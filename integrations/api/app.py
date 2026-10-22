"""GESIT HTTP API for the Joule capability (FR-SUP-1 to FR-SUP-3, FR-ACT-2, FR-INT-1, POL-1, POL-4).

    GESIT_API_KEY=... uvicorn --factory integrations.api.app:create_app --port 8000

A thin layer over the existing code: the supervisor (`agents.supervisor`), the
approval tools (`tools.action`, `agents.approvals`) and the Integrity Guard
(`agents.integrity` through the supervisor). Nothing here scores, checks policy or
approves on its own. Every endpoint except /health needs the header X-API-Key with
the value of GESIT_API_KEY; without that variable the app refuses to start (no
secret in any file: on Cloud Foundry it is set with cf set-env, NFR-4).

State lives where the CLIs keep it: the PO store (PO_STORE_PATH), the plan log
(PLAN_LOG_PATH) and the decision log (DECISION_LOG_PATH), on the app's local disk.
State-changing calls run one at a time (one lock), so two runs never draw the same
EBELN. Long runs (20 to 46 s with the model texts) can go through /run/start and
/run/{job_id}. Every response has SUMMARY; every amount in it says "(simulated)" (D-3),
text the user typed is quoted exactly as received, and SUMMARY ends with NEXT_STEPS (the
phrases the user can type next in Joule). POST /approvals/{ebeln}/approve takes "as":
"BUYER" for the draft's own buyer (ERNAM, the one-click of FR-ACT-3); the policy checks
in `release_po` decide as for anyone else.
"""
from __future__ import annotations

import hmac
import os
import threading
import uuid
from collections import OrderedDict
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapters import PoStore, default_adapter
from adapters.po_store import clear_po_store, po_store_path
from agents import approvals, supervisor as sup
from agents.integrity import is_override
from policy import Principal
from tools import get_integrity_flags, list_pending_approvals, reject_po
from tools.integrity_tools import (MIN_JUSTIFICATION_CHARS, check_override, clear_decision_log,
                                   read_overrides, recommended_label)

from integrations.api import responses as views
from integrations.api import warmup

API_KEY_ENV = "GESIT_API_KEY"
SCENARIOS = ("S1", "S2", "S3", "ALL", "SCAN")
MAX_JOBS = 20   # finished /run/start jobs kept in memory, oldest dropped first
BUYER_ALIAS = "BUYER"   # "as": "BUYER" = the draft's own buyer (ERNAM)
LATEST_OVERRIDES = 5


class RunBody(BaseModel):
    scenario: str = Field(description="S1, S2, S3, all (or scan)")
    llm: bool = True

    @field_validator("scenario")
    @classmethod
    def known(cls, value: str) -> str:
        if value.upper() not in SCENARIOS:
            raise ValueError("scenario must be S1, S2, S3, all or scan")
        return value.upper()


class ActorBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    actor: str = Field(alias="as", min_length=1,
                       description="who acts, e.g. MANAGER01, or BUYER for the draft's buyer")
    role: str = "manager"


class RejectBody(ActorBody):
    reason: str


class OverrideBody(BaseModel):
    scenario: Literal["S1", "S2", "S3"]
    choose: str = Field(min_length=1, description="a ranked LIFNR, or 'keep'")
    justification: str = ""
    llm: bool = False   # the Integrity Guard path uses no model text (as agents.integrity)


def _summary_error(status: int, summary: str, next_steps: str | None = None,
                   **extra: Any) -> HTTPException:
    body = views.with_next(summary, next_steps or views.say(views.PENDING_PHRASE))
    return HTTPException(status, {**body, "DATA_NOTE": views.DATA_NOTE, **extra})


def create_app(load_env: bool = True, warm_up: bool | None = None) -> FastAPI:
    """Build the app; RuntimeError when GESIT_API_KEY is not set (the app does not start).

    `warm_up` (default: GESIT_WARMUP, on) starts `integrations.api.warmup` in a background
    thread; /health answers at once either way, GET /warmup shows its progress.
    """
    if load_env:
        try:   # local development reads .env; on Cloud Foundry the variables come from cf set-env
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is not set; the GESIT API does not start without it")

    app = FastAPI(title="GESIT API", version="1.0",
                  description="Procurement disruption assistant (simulated data).")
    @app.exception_handler(HTTPException)
    def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        """Errors carry SUMMARY at the top level, like every other response."""
        content = exc.detail if isinstance(exc.detail, dict) else {"SUMMARY": str(exc.detail)}
        return JSONResponse(content, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    def invalid_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}"
                             for e in exc.errors())
        return JSONResponse({"SUMMARY": f"Invalid request: {problems}."}, status_code=422)

    lock = threading.Lock()
    jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
    adapter = default_adapter()
    warm_state: dict[str, Any] = {"STATUS": "off", "STEPS": [], "SECONDS": None}
    if warmup.enabled() if warm_up is None else warm_up:
        warmup.start(adapter, warm_state)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not x_api_key or not hmac.compare_digest(x_api_key, api_key):
            raise HTTPException(401, {"SUMMARY": "Missing or wrong X-API-Key header."})

    def store() -> PoStore:
        return PoStore(adapter, path=po_store_path())

    def scenario_of() -> dict[str, str]:
        return {n["ebeln"]: n["id"] for n in adapter.scenarios()}

    def run_supervisor(scenario: str, use_llm: bool, choose: str | None = None,
                       justification: str = "") -> sup.Supervisor:
        """One supervisor run, plan logged as by `python -m agents.supervisor`."""
        with lock:
            run = sup.Supervisor(scenario, adapter=adapter, store=store(), use_llm=use_llm,
                                 choose=choose, justification=justification,
                                 plan_id=sup.next_plan_id())
            try:
                run.run()
            finally:
                sup.append_plan(run.plan)
        return run

    # ----------------------------------------------------------------- health
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/warmup", dependencies=[Depends(require_key)])
    def warmup_status() -> dict[str, Any]:
        steps = "; ".join(f"{s['STEP']} {s['SECONDS']} s" for s in warm_state["STEPS"])
        return {**warm_state, "SUMMARY": f"Warm-up {warm_state['STATUS']}"
                + (f": {steps}." if steps else ".")}

    # -------------------------------------------------------------------- run
    @app.post("/run", dependencies=[Depends(require_key)])
    def run(body: RunBody) -> dict[str, Any]:
        return views.run_response(run_supervisor(body.scenario, body.llm))

    @app.post("/run/start", dependencies=[Depends(require_key)])
    def run_start(body: RunBody) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        jobs[job_id] = {"status": "running", "SCENARIO": body.scenario}
        while len(jobs) > MAX_JOBS:
            jobs.popitem(last=False)

        def work() -> None:
            try:
                jobs[job_id] = {"status": "done",
                                **views.run_response(run_supervisor(body.scenario, body.llm))}
            except Exception as exc:   # reported to the caller, never raised in the thread
                jobs[job_id] = {"status": "failed", "SUMMARY": f"The run failed: "
                                f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=work, name=f"gesit-run-{job_id}", daemon=True).start()
        return {"job_id": job_id, "status": "running",
                "SUMMARY": f"Run {body.scenario} started. Ask GET /run/{job_id} for the result."}

    @app.get("/run/{job_id}", dependencies=[Depends(require_key)])
    def run_result(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise _summary_error(404, f"No run with job id {job_id}.")
        job = jobs[job_id]
        if job["status"] == "running":
            return {"status": "running", "SUMMARY": "The run is still going, ask again shortly."}
        return job

    # -------------------------------------------------------------- approvals
    @app.get("/approvals", dependencies=[Depends(require_key)])
    def approvals_list() -> dict[str, Any]:
        listing = list_pending_approvals(store=store(), principal=approvals.VIEWER)
        pending = listing["PENDING"]
        return {"DATA_NOTE": views.DATA_NOTE,
                **views.with_next(views.pending_summary(pending), views.pending_next_steps(pending)),
                "PENDING": pending}

    @app.post("/approvals/{ebeln}/approve", dependencies=[Depends(require_key)])
    def approve(ebeln: str, body: ActorBody) -> dict[str, Any]:
        with lock:
            pos = store()
            try:
                if body.actor.strip().upper() == BUYER_ALIAS:   # the draft's own buyer (B2)
                    principal = Principal(pos.get(ebeln)["ERNAM"], "buyer")
                else:
                    principal = Principal(body.actor, body.role)
                result = approvals.approve_po(pos, ebeln, principal)
            except KeyError:
                raise _summary_error(404, f"PO {ebeln} was not created by GESIT.")
            except ValueError as exc:
                raise _summary_error(409, str(exc))
        if result["RESULT"] != "RELEASED":
            decision = result["POLICY_DECISION"]
            raise _summary_error(403, views.deny_summary("Approve", principal, decision, ebeln,
                                                         result["STATUS"]),
                                 views.deny_next_steps(decision, ebeln), POLICY_DECISION=decision)
        po = result["PO"]
        return {"DATA_NOTE": views.DATA_NOTE,
                **views.with_next(views.approve_summary(po, principal.id, principal.role),
                                  views.say(views.PENDING_PHRASE, views.INTEGRITY_PHRASE)),
                "EBELN": ebeln, "STATUS": result["STATUS"], "RELEASED_BY": principal.id,
                "POLICY_DECISION": result["POLICY_DECISION"], "OLD_PO": po.get("OLD_PO_TEXT")}

    @app.post("/approvals/{ebeln}/reject", dependencies=[Depends(require_key)])
    def reject(ebeln: str, body: RejectBody) -> dict[str, Any]:
        principal = Principal(body.actor, body.role)
        with lock:
            pos = store()
            try:
                result = reject_po(ebeln, body.reason, store=pos, principal=principal)
            except KeyError:
                raise _summary_error(404, f"PO {ebeln} was not created by GESIT.")
            except ValueError as exc:
                raise _summary_error(409, str(exc))
        decision = result["POLICY_DECISION"]
        if result["RESULT"] == "DENIED":
            raise _summary_error(403, views.deny_summary("Reject", principal, decision, ebeln,
                                                         result["STATUS"]),
                                 views.deny_next_steps(decision, ebeln), POLICY_DECISION=decision)
        if result["RESULT"] == "REFUSED":   # A1: quote what was received, exactly
            raise _summary_error(422, views.short_reason_summary(
                "Reason", body.reason, result["REASON_CHARS"], MIN_JUSTIFICATION_CHARS,
                f"Nothing changed, PO {ebeln} stays {result['STATUS']}."),
                views.say(views.reject_phrase(ebeln)), REASON_CHARS=result["REASON_CHARS"])
        po = pos.get(ebeln)
        option = result["NEXT_OPTION"]
        return {"DATA_NOTE": views.DATA_NOTE,
                **views.with_next(views.reject_summary(po, option, body.reason),
                                  views.reject_next_steps(po, option, scenario_of())),
                "EBELN": ebeln, "STATUS": result["STATUS"], "REJECTED_BY": po["REJECTED_BY"],
                "REJECT_REASON": po["REJECT_REASON"], "REPLACES_EBELN": po["REPLACES_EBELN"],
                "REJECT_ID": result["REJECT_ID"],
                "NEXT_OPTION": ({**option, "LABEL": result["NEXT_OPTION_LABEL"],
                                 "TEXT": approvals.option_text(option)} if option else None)}

    # --------------------------------------------------------------- override
    @app.post("/override", dependencies=[Depends(require_key)])
    def override(body: OverrideBody) -> dict[str, Any]:
        run = run_supervisor(body.scenario, body.llm, choose=body.choose,
                             justification=body.justification)
        outcome = run.plan["OUTCOME"] or {}
        cases = (run.evaluation or {}).get("CASES", [])
        case = cases[0] if cases else None
        if outcome.get("CODE") == sup.STOP_OVERRIDE_REJECTED:
            reason = next((line.strip() for line in run.choice["LINES"]
                           if line.strip().startswith("Rejected")), outcome["TEXT"])
            check = check_override(recommended_label(case) or "", run.choice["CHOSEN"],
                                   body.justification)
            if check["OVERRIDE"] and check["JUSTIFICATION_CHARS"] is not None and \
                    check["JUSTIFICATION_CHARS"] < MIN_JUSTIFICATION_CHARS:   # A1
                reason = views.short_reason_summary(
                    "Justification", body.justification, check["JUSTIFICATION_CHARS"],
                    MIN_JUSTIFICATION_CHARS, "No draft PO was created.")
            raise _summary_error(422, reason,
                                 views.say(views.override_phrase(body.scenario, body.choose)),
                                 REASON=reason, CHOSEN=run.choice["CHOSEN"])
        overridden = case is not None and is_override(body.choose, case)
        out = views.run_response(run)
        flags = get_integrity_flags(adapter=adapter)
        record = (next((o for o in read_overrides()
                        if o["OVERRIDE_ID"] == run.choice["OVERRIDE_ID"]), None)
                  if overridden and run.choice else None)
        summary = views.override_summary(
            choice=run.choice, record=record,
            recommended=recommended_label(case) if case else None, chosen=body.choose,
            justification=body.justification,
            next_sentence=(views.next_step_sentence(case["EBELN"], run.actions) if case
                           else "Nothing was drafted."),
            supersede=views.supersede_sentences(run.actions), flags=flags)
        return {"DATA_NOTE": views.DATA_NOTE,
                **views.with_next(summary, out["NEXT_STEPS"]),
                "OVERRIDE_ID": run.choice["OVERRIDE_ID"] if run.choice else None,
                "CHOSEN": run.choice["CHOSEN"] if run.choice else body.choose,
                "RECOMMENDED": recommended_label(case) if case else None,
                "JUSTIFICATION": body.justification,
                "PLAN": out["PLAN"], "PLAN_MD": out["PLAN_MD"], "CASES": out["CASES"],
                "TABLE_MD": out["TABLE_MD"], "DRAFTS": out["DRAFTS"],
                "SUPERSEDED": out["SUPERSEDED"], "INTEGRITY_FLAGS": views.flags_view(flags)}

    # -------------------------------------------------------------- integrity
    @app.get("/integrity", dependencies=[Depends(require_key)])
    def integrity_flags() -> dict[str, Any]:
        """B4, FR-INT-4: flagged buyer and supplier pairs and the latest overrides."""
        flags = get_integrity_flags(adapter=adapter)
        latest = read_overrides()[-LATEST_OVERRIDES:]
        return {"DATA_NOTE": views.DATA_NOTE,
                **views.with_next(views.integrity_summary(flags, latest),
                                  views.say(views.PENDING_PHRASE, views.run_phrase("S1"))),
                "TABLE_MD": views.integrity_md(flags, latest),
                "INTEGRITY_FLAGS": views.flags_view(flags), "LATEST_OVERRIDES": latest}

    # ------------------------------------------------------------------ reset
    @app.post("/reset", dependencies=[Depends(require_key)])
    def reset() -> dict[str, Any]:
        with lock:
            clear_po_store()
            sup.clear_plan_log()
            clear_decision_log()
            jobs.clear()
        return {"status": "ok",
                **views.with_next("The PO store, the plan log and the decision log are empty. "
                                  "The next run starts from the scenario start state.",
                                  views.say(views.run_phrase("S1")))}

    return app
