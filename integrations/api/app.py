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
/run/{job_id}. Every response has SUMMARY; every amount in it says "(simulated)" (D-3).
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
from tools.integrity_tools import clear_decision_log, recommended_label

from integrations.api import responses as views

API_KEY_ENV = "GESIT_API_KEY"
SCENARIOS = ("S1", "S2", "S3", "ALL", "SCAN")
MAX_JOBS = 20   # finished /run/start jobs kept in memory, oldest dropped first


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
    actor: str = Field(alias="as", min_length=1, description="who acts, e.g. MANAGER01")
    role: str = "manager"


class RejectBody(ActorBody):
    reason: str


class OverrideBody(BaseModel):
    scenario: Literal["S1", "S2", "S3"]
    choose: str = Field(min_length=1, description="a ranked LIFNR, or 'keep'")
    justification: str = ""
    llm: bool = False   # the Integrity Guard path uses no model text (as agents.integrity)


def _summary_error(status: int, summary: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"SUMMARY": summary, "DATA_NOTE": views.DATA_NOTE, **extra})


def create_app(load_env: bool = True) -> FastAPI:
    """Build the app; RuntimeError when GESIT_API_KEY is not set (the app does not start)."""
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

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not x_api_key or not hmac.compare_digest(x_api_key, api_key):
            raise HTTPException(401, {"SUMMARY": "Missing or wrong X-API-Key header."})

    def store() -> PoStore:
        return PoStore(adapter, path=po_store_path())

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
        return {"DATA_NOTE": views.DATA_NOTE, "SUMMARY": views.pending_summary(listing["PENDING"]),
                "PENDING": listing["PENDING"]}

    @app.post("/approvals/{ebeln}/approve", dependencies=[Depends(require_key)])
    def approve(ebeln: str, body: ActorBody) -> dict[str, Any]:
        principal = Principal(body.actor, body.role)
        with lock:
            try:
                result = approvals.approve_po(store(), ebeln, principal)
            except KeyError:
                raise _summary_error(404, f"PO {ebeln} was not created by GESIT.")
            except ValueError as exc:
                raise _summary_error(409, str(exc))
        if result["RESULT"] != "RELEASED":
            decision = result["POLICY_DECISION"]
            raise _summary_error(403, views.deny_summary("Approve", principal, decision, ebeln,
                                                         result["STATUS"]),
                                 POLICY_DECISION=decision)
        po = result["PO"]
        return {"DATA_NOTE": views.DATA_NOTE, "SUMMARY": views.approve_summary(po, principal.id),
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
                                 POLICY_DECISION=decision)
        if result["RESULT"] == "REFUSED":
            raise _summary_error(422, f"Refused: {result['MESSAGE']}.")
        po = pos.get(ebeln)
        option = result["NEXT_OPTION"]
        return {"DATA_NOTE": views.DATA_NOTE, "SUMMARY": views.reject_summary(po, option),
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
        if outcome.get("CODE") == sup.STOP_OVERRIDE_REJECTED:
            reason = next((line.strip() for line in run.choice["LINES"]
                           if line.strip().startswith("Rejected")), outcome["TEXT"])
            raise _summary_error(422, reason, REASON=reason, CHOSEN=run.choice["CHOSEN"])
        cases = (run.evaluation or {}).get("CASES", [])
        case = cases[0] if cases else None
        overridden = case is not None and is_override(body.choose, case)
        out = views.run_response(run)
        flags = get_integrity_flags(adapter=adapter)
        drafts = out["DRAFTS"]
        if overridden:
            summary = (f"Override {run.choice['OVERRIDE_ID']} logged: {run.choice['CHOSEN']} "
                       f"instead of the recommended {recommended_label(case)}, with the buyer's "
                       "justification.")
        else:
            summary = (f"{body.choose} is the recommended option, so no justification was "
                       "needed and nothing was logged.")
        summary += " " + (views.next_step_sentence(case["EBELN"], run.actions) if case
                          else "Nothing was drafted.")
        summary += (f" Integrity flags: {flags['PAIRS_FLAGGED']} of {flags['PAIRS_TESTED']} "
                    "buyer and supplier pairs flagged for review.")
        return {"DATA_NOTE": views.DATA_NOTE, "SUMMARY": summary,
                "OVERRIDE_ID": run.choice["OVERRIDE_ID"] if run.choice else None,
                "CHOSEN": run.choice["CHOSEN"] if run.choice else body.choose,
                "RECOMMENDED": recommended_label(case) if case else None,
                "PLAN": out["PLAN"], "CASES": out["CASES"], "DRAFTS": drafts,
                "INTEGRITY_FLAGS": views.flags_view(flags)}

    # ------------------------------------------------------------------ reset
    @app.post("/reset", dependencies=[Depends(require_key)])
    def reset() -> dict[str, Any]:
        with lock:
            clear_po_store()
            sup.clear_plan_log()
            clear_decision_log()
            jobs.clear()
        return {"status": "ok", "SUMMARY": "The PO store, the plan log and the decision log are "
                                           "empty. The next run starts from the scenario start "
                                           "state."}

    return app
