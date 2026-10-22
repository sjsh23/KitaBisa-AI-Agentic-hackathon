"""Supervisor (FR-SUP-1 to FR-SUP-3): a plan, then Detection, Supplier Evaluation and Action.

    python -m agents.supervisor S1            a supplier notice (S1, S2, S3) as the trigger
    python -m agents.supervisor all           every notice on file
    python -m agents.supervisor scan          a scheduled scan of every open PO
    python -m agents.supervisor S1 --no-llm   skip the model texts (Gen AI Hub)
    python -m agents.supervisor S1 --choose 100108 --justification "text"
                                              the buyer picks another ranked option

FR-SUP-1: before anything runs, the plan (ordered steps, the agent that runs each,
and why) is built in code and printed. When the run ends it is appended as one
JSON line to the plan log (PLAN_LOG_PATH, default plan_log.jsonl next to the
decision log), every step with STATUS, SECONDS and RESULT (NFR-1).

FR-SUP-2: Detection, then Supplier Evaluation, then Action. Integrity Guard runs
only when the buyer passes --choose with an option other than #1; it runs before
Action because FR-INT-1 needs the justification before the draft exists
(docs/prd_change_proposals.md item 10). Evaluation and Learning is not part of the
live flow (offline, eval/monte_carlo.py). Stop rules are normal outcomes, printed
and logged: nothing flagged; keeping the current PO ranks #1 for every case; no
candidate supplier for a case (that case goes back to the buyer, the others go
on); policy denies release, so the draft is submitted for approval and the run
hands back to the manager without waiting.

FR-SUP-3: a Strands Graph (strands.multiagent.GraphBuilder). Each node is a plain
Python step (`StepNode`, a MultiAgentBase), each edge is fixed in code, and an
edge is followed only while no stop rule has fired. No LLM decides the route and
no Swarm is used; the model is called only for the texts inside Detection and
Supplier Evaluation, as in agents.run.

The report printed after the plan is the agents.run report (same print functions).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from strands.multiagent import GraphBuilder
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status

from adapters import CsvAdapter, PoStore, default_adapter
from adapters.po_store import po_store_path
from agents import action, integrity
from agents.action import ActionAgent
from agents.detection import DetectionAgent
from agents.run import (DEFAULT_MATNR, cases_for, format_action, format_evaluation,
                        format_report, llm_notes, notices_on_file, quota_text)
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient
from policy import APPROVAL_THRESHOLD_IDR
from scoring import supplier_score
from tools import get_integrity_flags
from tools.integrity_tools import JAKARTA, decision_log_path, recommended_label

DETECTION = "detection"
SUPPLIER_EVALUATION = "supplier_evaluation"
INTEGRITY_GUARD = "integrity_guard"
ACTION = "action"
AGENTS = {DETECTION: "Disruption Detection", SUPPLIER_EVALUATION: "Supplier Evaluation",
          INTEGRITY_GUARD: "Integrity Guard", ACTION: "Action"}

STATUS_PLANNED = "PLANNED"
STATUS_DONE = "DONE"
STATUS_SKIPPED = "SKIPPED"
STATUS_NOT_RUN = "NOT_RUN"
STATUS_FAILED = "FAILED"

# Stop rules (FR-SUP-2): each one a normal outcome.
STOP_NOTHING_FLAGGED = "NOTHING_FLAGGED"
STOP_KEEP_EVERYWHERE = "KEEP_EVERYWHERE"
STOP_NOTHING_TO_DRAFT = "NOTHING_TO_DRAFT"
STOP_OVERRIDE_REJECTED = "OVERRIDE_REJECTED"
STOP_HANDED_BACK = "HANDED_BACK"

# Case outcomes.
CASE_KEEP = "KEEP"
CASE_NO_CANDIDATE = "NO_CANDIDATE"
CASE_NEW_ORDER = "NEW_ORDER"
CASE_MANAGER = "MANAGER_APPROVAL"
CASE_BUYER = "BUYER_ONE_CLICK"
CASE_BLOCKED = "BLOCKED"

PLAN_LOG_ENV = "PLAN_LOG_PATH"
DEFAULT_PLAN_LOG_NAME = "plan_log.jsonl"
PLAN_PREFIX = "PLAN-"
ORCHESTRATION = ("Strands Graph, edges fixed in code, a step runs only while no stop rule has "
                 "fired; no LLM decides the order, no Swarm (FR-SUP-3)")
OFFLINE_NOTE = "Evaluation and Learning: offline, see eval/monte_carlo.py"


# ------------------------------------------------------------------- plan log
def plan_log_path(path: str | os.PathLike[str] | None = None) -> Path:
    """PLAN_LOG_PATH, else plan_log.jsonl in the decision log's folder."""
    return Path(path or os.environ.get(PLAN_LOG_ENV)
                or decision_log_path().with_name(DEFAULT_PLAN_LOG_NAME))


def read_plans(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Every logged plan, oldest first; an empty list when the log does not exist."""
    log = plan_log_path(path)
    if not log.exists():
        return []
    with open(log, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def clear_plan_log(path: str | os.PathLike[str] | None = None) -> None:
    """Empty the plan log (for scripts/reset_demo, NFR-2)."""
    log = plan_log_path(path)
    if log.exists():
        log.unlink()


def append_plan(plan: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    log = plan_log_path(path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(plan, ensure_ascii=False) + "\n")
    return log


def _now() -> str:
    return datetime.now(JAKARTA).isoformat(timespec="seconds")


# ----------------------------------------------------------------------- plan
def _step(step: str, why: str) -> dict[str, Any]:
    return {"STEP": step, "AGENT": AGENTS[step], "WHY": why, "STATUS": STATUS_PLANNED,
            "SECONDS": None, "RESULT": None}


def build_plan(scenario: str, scenarios: list[dict[str, Any]], matnr: str = DEFAULT_MATNR,
               choose: str | None = None, plan_id: str | None = None) -> dict[str, Any]:
    """FR-SUP-1: the ordered steps for a trigger, built in code before anything runs.

    A scenario id stands in for a supplier notice, `scan` for a scheduled scan; both
    take the same path. Raises SystemExit for an unknown scenario id.
    """
    notices = notices_on_file(scenario, scenarios)
    shown = cases_for(scenario, scenarios)
    scan = scenario.upper() == "SCAN"
    target = "every PO Detection flags" if shown is None else "PO " + ", ".join(shown)
    steps = [
        _step(DETECTION,
              f"score every open PO of {matnr} with SAP-RPT and check it against "
              + ("no notice (scheduled scan)" if scan
                 else f"the {len(notices)} supplier notices on file")
              + "; stop if nothing is flagged"),
        _step(SUPPLIER_EVALUATION,
              f"rank backup suppliers and keeping the current PO for {target}, against every "
              "flagged PO; stop if keeping ranks #1 for every case"),
    ]
    if choose is not None:
        steps.append(_step(INTEGRITY_GUARD,
                           f"the buyer chose {choose}: if that is not #1, check the justification "
                           "(20 characters or more) and log the override before any draft"))
    steps.append(_step(ACTION,
                       "draft the #1 (or the buyer's logged choice) per case and route it by "
                       f"policy: within policy it waits for the buyer's one click, above "
                       f"Rp{APPROVAL_THRESHOLD_IDR:,} it is submitted to the manager (POL-1); "
                       "the agent never releases a PO (POL-4)"))
    for no, step in enumerate(steps, 1):
        step["NO"] = no
    return {
        "PLAN_ID": plan_id,
        "PLANNED_AT": _now(),
        "TRIGGER": {"TYPE": "scheduled_scan" if scan else "supplier_notice",
                    "SCENARIO": scenario.upper()},
        "MATNR": matnr,
        "CASES_SHOWN": shown,
        "ORCHESTRATION": ORCHESTRATION,
        "STEPS": steps,
        "NOT_IN_LIVE_FLOW": [OFFLINE_NOTE],
        "OUTCOME": None,
        "CASES": [],
        "DATA_NOTE": "simulated data",
    }


def format_plan(plan: dict[str, Any]) -> str:
    trigger = plan["TRIGGER"]
    what = ("scheduled scan of every open PO" if trigger["TYPE"] == "scheduled_scan"
            else f"supplier notice {trigger['SCENARIO']} (scenario id standing in for the notice)")
    plan_id = f" {plan['PLAN_ID']}" if plan["PLAN_ID"] else ""
    lines = [f"GESIT Supervisor plan{plan_id} ({plan['DATA_NOTE']})",
             f"Trigger: {what}, material {plan['MATNR']}",
             f"Orchestration: {plan['ORCHESTRATION']}"]
    lines += [f"  {s['NO']}. {s['AGENT']}: {s['WHY']}" for s in plan["STEPS"]]
    lines += [f"  Not in the live flow: {note}" for note in plan["NOT_IN_LIVE_FLOW"]]
    return "\n".join(lines)


def format_outcome(plan: dict[str, Any], log: Path | None = None) -> str:
    """Per step status and seconds, the stop rule that ended the run, and every case."""
    lines = ["GESIT Supervisor outcome"]
    for s in plan["STEPS"]:
        took = "" if s["SECONDS"] is None else f" in {s['SECONDS']:.1f} s"
        lines.append(f"  {s['NO']}. {s['AGENT']}: {s['STATUS']}{took}"
                     + (f", {s['RESULT']}" if s["RESULT"] else ""))
    outcome = plan["OUTCOME"] or {}
    if outcome:
        lines.append(f"Stop ({outcome['CODE']}, after {AGENTS[outcome['AFTER']]}): {outcome['TEXT']}")
    lines += [f"  PO {c['EBELN']}: {c['TEXT']}" for c in plan["CASES"]]
    if log is not None:
        lines.append(f"Plan log: {log} ({plan['PLAN_ID']}), total {plan['TOTAL_SECONDS']:.1f} s")
    return "\n".join(lines)


# ---------------------------------------------------------------- graph nodes
class StepNode(MultiAgentBase):
    """A deterministic Graph node: runs one Python step, times it, and fills its plan entry.

    `fn` returns (STATUS, RESULT text). An exception marks the step FAILED and is raised.
    """

    def __init__(self, step: dict[str, Any], fn: Callable[[], tuple[str, str]]) -> None:
        super().__init__()
        self.id = step["STEP"]
        self.step = step
        self.fn = fn

    async def invoke_async(self, task: Any, invocation_state: dict[str, Any] | None = None,
                           **kwargs: Any) -> MultiAgentResult:
        start = time.perf_counter()
        try:
            status, result = self.fn()
        except Exception as exc:
            self.step.update(STATUS=STATUS_FAILED, RESULT=f"{type(exc).__name__}: {exc}",
                             SECONDS=round(time.perf_counter() - start, 2))
            raise
        seconds = round(time.perf_counter() - start, 2)
        self.step.update(STATUS=status, RESULT=result, SECONDS=seconds)
        ms = round(seconds * 1000)
        node = NodeResult(result=MultiAgentResult(status=Status.COMPLETED), execution_time=ms,
                          status=Status.COMPLETED, execution_count=1)
        return MultiAgentResult(status=Status.COMPLETED, results={self.id: node},
                                execution_time=ms, execution_count=1)


# --------------------------------------------------------------------- the run
def case_kind(case: dict[str, Any]) -> str:
    """What the ranked table says for one case: keep the PO, no candidate, or a new order."""
    rows = case["SCORING"]["CANDIDATES"]
    if not rows:
        return CASE_NO_CANDIDATE
    return CASE_KEEP if rows[0].get("OPTION") == supplier_score.OPTION_KEEP else CASE_NEW_ORDER


def _kind_text(case: dict[str, Any]) -> str:
    kind = case_kind(case)
    if kind == CASE_NO_CANDIDATE:
        return "no candidate supplier (every quote excluded); handed back to the buyer"
    if kind == CASE_KEEP:   # Action does not run, so its old PO line is given here
        return ("keeping the current PO ranks #1, nothing drafted; handed back to the buyer. "
                + action.old_po_action(case)["TEXT"])
    return f"#1 is {recommended_label(case)}, a new order"


class Supervisor:
    """One run of the plan on one trigger. The PO store is the Action side's state."""

    def __init__(self, scenario: str, *, adapter: CsvAdapter | None = None,
                 rpt_client: RptClient | None = None, store: PoStore | None = None,
                 matnr: str = DEFAULT_MATNR, use_llm: bool = True, choose: str | None = None,
                 justification: str = "", plan_id: str | None = None,
                 detector: DetectionAgent | None = None,
                 evaluator: SupplierEvaluationAgent | None = None) -> None:
        self.adapter = adapter or default_adapter()
        self.rpt_client = rpt_client or RptClient()   # one client, one AI Core token
        self.store = store or PoStore(self.adapter)
        self.scenario, self.matnr, self.use_llm = scenario, matnr, use_llm
        self.choose, self.justification = choose, justification
        self.detector = detector or DetectionAgent(adapter=self.adapter, rpt_client=self.rpt_client)
        self.evaluator = evaluator or SupplierEvaluationAgent(adapter=self.adapter,
                                                              rpt_client=self.rpt_client)
        self.plan = build_plan(scenario, self.adapter.scenarios(), matnr, choose, plan_id)
        self.report: dict[str, Any] | None = None
        self.evaluation: dict[str, Any] | None = None
        self.choice: dict[str, Any] | None = None
        self.actions: dict[str, Any] | None = None
        self.flags: dict[str, Any] | None = None

    # ---- stop rules
    def _stop(self, after: str, code: str, text: str, cases: list[dict[str, Any]]) -> None:
        self.plan["OUTCOME"] = {"AFTER": after, "CODE": code, "TEXT": text,
                                "HANDED_BACK_TO": sorted({c["TO"] for c in cases if c.get("TO")})}
        self.plan["CASES"] = cases

    def stopped(self) -> bool:
        return self.plan["OUTCOME"] is not None

    def _ranking_cases(self) -> list[dict[str, Any]]:
        return [{"EBELN": c["EBELN"], "OUTCOME": case_kind(c), "TEXT": _kind_text(c),
                 "TO": "buyer" if case_kind(c) != CASE_NEW_ORDER else None}
                for c in self.evaluation["CASES"]]

    def _stop_if_nothing_to_draft(self, after: str) -> None:
        cases = self._ranking_cases()
        if any(c["OUTCOME"] == CASE_NEW_ORDER for c in cases):
            return
        if cases and all(c["OUTCOME"] == CASE_KEEP for c in cases):
            self._stop(after, STOP_KEEP_EVERYWHERE, "keeping the current PO ranks #1 for every "
                       "case, nothing drafted; handed back to the buyer.", cases)
        else:
            self._stop(after, STOP_NOTHING_TO_DRAFT, "no case has a new order to draft (keeping "
                       "ranks #1 or no candidate supplier); handed back to the buyer.", cases)

    # ---- steps (each returns STATUS and a one-line RESULT)
    def detect(self) -> tuple[str, str]:
        notices = notices_on_file(self.scenario, self.adapter.scenarios())
        self.report = (self.detector.run(self.matnr, notices) if self.use_llm
                       else self.detector.detect(self.matnr, notices))
        pred, flagged = self.report["PREDICTION"], self.report["FLAGGED_EBELN"]
        model = pred["MODEL_ID"] + (", FALLBACK" if pred["FALLBACK"] else "")
        result = (f"{len(self.report['OPEN_POS'])} open POs scored ({model}), "
                  f"{len(flagged)} flagged" + (f": {', '.join(flagged)}" if flagged else ""))
        if not flagged:
            self._stop(DETECTION, STOP_NOTHING_FLAGGED, "Detection flagged nothing: no action "
                       "needed.", [])
        return STATUS_DONE, result

    def evaluate(self) -> tuple[str, str]:
        shown = cases_for(self.scenario, self.adapter.scenarios())
        flagged = self.report["FLAGGED_POS"]
        self.evaluation = (self.evaluator.run(self.report, flagged, shown) if self.use_llm
                           else self.evaluator.evaluate(self.report, flagged, shown))
        cases = self.evaluation["CASES"]
        result = f"{len(cases)} case{'s' if len(cases) != 1 else ''} ranked" + (
            ": " + "; ".join(f"PO {c['EBELN']} #1 {recommended_label(c) or 'none'}"
                             for c in cases) if cases else "")
        # With a buyer's choice the Integrity Guard decides next (a new order may still be chosen).
        if self.choose is None or not cases or case_kind(cases[0]) == CASE_NO_CANDIDATE:
            self._stop_if_nothing_to_draft(SUPPLIER_EVALUATION)
        return STATUS_DONE, result

    def guard(self) -> tuple[str, str]:
        case = self.evaluation["CASES"][0]          # the buyer's choice is for the most urgent case
        if not integrity.is_override(self.choose, case):
            self._stop_if_nothing_to_draft(INTEGRITY_GUARD)
            return STATUS_SKIPPED, (f"the buyer's choice {recommended_label(case)} is #1 for PO "
                                    f"{case['EBELN']}; no justification needed")
        buyer_id = self._buyer_of(case["EBELN"])
        self.choice = integrity.buyer_choice(case, self.choose, self.justification, buyer_id)
        self.choice.update(EBELN=case["EBELN"], BUYER=buyer_id)
        if not self.choice["ALLOWED"]:
            self._stop(INTEGRITY_GUARD, STOP_OVERRIDE_REJECTED,
                       f"the buyer's choice {self.choice['CHOSEN']} for PO {case['EBELN']} was "
                       "rejected (see Buyer's choice), nothing drafted; handed back to the buyer.",
                       [{"EBELN": case["EBELN"], "OUTCOME": STOP_OVERRIDE_REJECTED,
                         "TEXT": "choice rejected, nothing drafted", "TO": "buyer"}])
            return STATUS_DONE, f"choice {self.choice['CHOSEN']} rejected, nothing drafted"
        self.flags = get_integrity_flags(adapter=self.adapter)
        return STATUS_DONE, (f"override {self.choice['OVERRIDE_ID']} logged: "
                             f"{self.choice['CHOSEN']} instead of {recommended_label(case)}")

    def act(self) -> tuple[str, str]:
        choices = None
        if self.choice and self.choice["ALLOWED"]:
            choices = {self.choice["EBELN"]: {"CHOSEN": self.choice["CHOSEN"],
                                              "OVERRIDE_ID": self.choice["OVERRIDE_ID"]}}
        self.actions = ActionAgent(adapter=self.adapter, store=self.store).act(
            self.evaluation, choices=choices)
        cases = [self._action_case(c) for c in self.actions["CASES"]]
        orders = [o for c in self.actions["CASES"] for o in c["ORDERS"]]
        result = (f"{len(orders)} draft{'s' if len(orders) != 1 else ''}"
                  + (": " + "; ".join(f"{o['EBELN'] or '-'} {o['STATUS']}" for o in orders)
                     if orders else ", nothing drafted"))
        to = sorted({c["TO"] for c in cases if c["TO"]})
        self._stop(ACTION, STOP_HANDED_BACK,
                   "handed back to the " + " and the ".join(to)
                   + "; the supervisor does not wait for an approval." if to else
                   "nothing waits for a human.", cases)
        return STATUS_DONE, result

    def _buyer_of(self, ebeln: str) -> str:
        return next(line["ERNAM"] for line in self.adapter.po_lines() if line["EBELN"] == ebeln)

    @staticmethod
    def _action_case(case: dict[str, Any]) -> dict[str, Any]:
        """Who decides next for one case of the Action report (FR-ACT-2, FR-ACT-3)."""
        out = {"EBELN": case["EBELN"]}
        if case["NOTE"]:
            kind = CASE_NO_CANDIDATE if "no candidate" in case["NOTE"] else CASE_KEEP
            return {**out, "OUTCOME": kind, "TEXT": case["NOTE"], "TO": "buyer"}
        nexts = {o["NEXT"] for o in case["ORDERS"]}
        if action.NEXT_MANAGER in nexts:
            ids = ", ".join(o["APPROVAL_REQUEST_ID"] for o in case["ORDERS"]
                            if o["APPROVAL_REQUEST_ID"])
            return {**out, "OUTCOME": CASE_MANAGER, "TO": "manager",
                    "TEXT": f"release denied by POL-1, submitted for approval {ids}; "
                            "handed back to the manager"}
        if action.NEXT_BLOCKED in nexts:
            policies = ", ".join(sorted({o["RELEASE_CHECK"]["POLICY"] for o in case["ORDERS"]
                                         if o["NEXT"] == action.NEXT_BLOCKED}))
            return {**out, "OUTCOME": CASE_BLOCKED, "TO": "manager",
                    "TEXT": f"blocked by {policies}, nothing can be released; handed back to the "
                            "manager"}
        drafts = ", ".join(o["EBELN"] for o in case["ORDERS"])
        return {**out, "OUTCOME": CASE_BUYER, "TO": "buyer",
                "TEXT": f"draft {drafts} within policy, waiting for the buyer's one click"}

    # ---- the graph
    def build_graph(self):
        """FR-SUP-3: one node per plan step, edges fixed in code, followed while no stop fired."""
        steps = {DETECTION: self.detect, SUPPLIER_EVALUATION: self.evaluate,
                 INTEGRITY_GUARD: self.guard, ACTION: self.act}
        order = [s["STEP"] for s in self.plan["STEPS"]]
        builder = GraphBuilder()
        for step in self.plan["STEPS"]:
            builder.add_node(StepNode(step, steps[step["STEP"]]), step["STEP"])
        for before, after in zip(order, order[1:]):
            builder.add_edge(before, after, condition=lambda _state: not self.stopped())
        builder.set_entry_point(order[0])
        builder.set_max_node_executions(len(order))
        builder.set_graph_id("gesit-supervisor")
        return builder.build()

    def run(self) -> dict[str, Any]:
        """Run the plan; steps after a stop rule are NOT_RUN. Returns the finished plan."""
        start = time.perf_counter()
        try:
            self.build_graph()(f"GESIT trigger {self.plan['TRIGGER']['SCENARIO']}")
        finally:
            for step in self.plan["STEPS"]:
                if step["STATUS"] == STATUS_PLANNED:
                    step["STATUS"] = STATUS_NOT_RUN
            self.plan["TOTAL_SECONDS"] = round(time.perf_counter() - start, 2)
            self.plan["FINISHED_AT"] = _now()
            self.plan["LLM"] = self.use_llm
            self.plan["PO_STORE"] = str(self.store.path) if self.store.path else None
        return self.plan

    def report_text(self) -> str:
        """The agents.run report for the steps that ran, plus the Integrity Guard parts."""
        parts = [format_report(self.report)] if self.report else []
        if self.evaluation:
            parts.append(format_evaluation(self.evaluation))
        if self.choice:
            parts.append(f"GESIT Integrity Guard (simulated data)\nPO {self.choice['EBELN']}, "
                         f"buyer {self.choice['BUYER']}, decision log {decision_log_path()}\n"
                         + "\n".join(self.choice["LINES"]))
        if self.actions:
            parts.append(format_action(self.actions))
        text = "\n\n".join(parts) + quota_text(llm_notes())
        if self.flags:
            text += "\n\n" + integrity.format_flags(self.flags)
        return text


def next_plan_id(path: str | os.PathLike[str] | None = None) -> str:
    return f"{PLAN_PREFIX}{len(read_plans(path)) + 1:03d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GESIT supervisor: plan, then the agents.")
    parser.add_argument("scenario", nargs="?", default="scan", help="S1, S2, S3, scan or all")
    parser.add_argument("--matnr", default=DEFAULT_MATNR)
    parser.add_argument("--no-llm", action="store_true", help="skip the model texts (Gen AI Hub)")
    parser.add_argument("--choose", help="LIFNR of a ranked option, or 'keep', for the most urgent "
                                         "case shown (default: #1)")
    parser.add_argument("--justification", default="", help="why the buyer overrides #1")
    args = parser.parse_args(argv)

    try:   # local development reads credentials from .env (NFR-4)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    adapter = default_adapter()
    store = PoStore(adapter, path=po_store_path())   # survives until the manager decides
    supervisor = Supervisor(args.scenario, adapter=adapter, store=store, matnr=args.matnr,
                            use_llm=not args.no_llm, choose=args.choose,
                            justification=args.justification, plan_id=next_plan_id())
    print(format_plan(supervisor.plan) + "\n", flush=True)
    try:
        plan = supervisor.run()
    finally:
        log = append_plan(supervisor.plan)
    print(supervisor.report_text())
    print("\n" + format_outcome(plan, log))
    return 1 if plan["OUTCOME"] and plan["OUTCOME"]["CODE"] == STOP_OVERRIDE_REJECTED else 0


if __name__ == "__main__":
    raise SystemExit(main())
