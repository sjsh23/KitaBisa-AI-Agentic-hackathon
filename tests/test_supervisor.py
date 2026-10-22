"""Supervisor: plan, fixed order, stop rules, plan log (FR-SUP-1 to FR-SUP-3, FR-ACT-2, POL-4).

Offline: SAP-RPT is a stub and no model text is asked for. Every test writes its
decision log, plan log and PO store under a temp folder.
"""
import copy
import json

import pytest
from strands.multiagent.base import MultiAgentBase
from strands.multiagent.graph import Graph

from adapters import CsvAdapter
from agents import run as run_cli
from agents import supervisor as sup
from agents.detection import DetectionAgent
from agents.supervisor import (ACTION, DETECTION, INTEGRITY_GUARD, SUPPLIER_EVALUATION,
                               StepNode, Supervisor, build_plan, read_plans)
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient

GOOD_REASON = "100109 has only 4 receipts, we prefer a long track record"


class StubRpt(RptClient):
    """SAP-RPT stand-in: the same P(late) for every row."""

    def __init__(self):
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        return {r["EBELN"]: 0.2 for r in query_rows}


class NoCandidates(SupplierEvaluationAgent):
    """Supplier Evaluation with every quote excluded for the listed POs (stop rule test)."""

    def __init__(self, ebelns, **kw):
        super().__init__(**kw)
        self.ebelns = set(ebelns)

    def evaluate(self, *args, **kwargs):
        report = copy.deepcopy(super().evaluate(*args, **kwargs))
        for case in report["CASES"]:
            if case["EBELN"] in self.ebelns:
                case["SCORING"]["CANDIDATES"] = []
        return report


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    return tmp_path


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def supervisor(adapter, scenario, evaluator_cls=SupplierEvaluationAgent, evaluator_kw=None,
               **kw):
    rpt = StubRpt()
    return Supervisor(scenario, adapter=adapter, rpt_client=rpt, use_llm=False,
                      detector=DetectionAgent(adapter=adapter, rpt_client=rpt,
                                              reasoner=lambda f: "ok"),
                      evaluator=evaluator_cls(adapter=adapter, rpt_client=rpt,
                                              explainer=lambda f: "ok", **(evaluator_kw or {})),
                      **kw)


def statuses(plan):
    return {s["STEP"]: s["STATUS"] for s in plan["STEPS"]}


# ---------------------------------------------------------------- FR-SUP-1
@pytest.mark.parametrize("scenario, trigger, shown", [
    ("S1", "supplier_notice", ["4500000431"]),
    ("S2", "supplier_notice", ["4500000432"]),
    ("S3", "supplier_notice", ["4500000433"]),
    ("all", "supplier_notice", None),
    ("scan", "scheduled_scan", None),
])
def test_plan_steps(adapter, scenario, trigger, shown):
    plan = build_plan(scenario, adapter.scenarios())
    assert [s["STEP"] for s in plan["STEPS"]] == [DETECTION, SUPPLIER_EVALUATION, ACTION]
    assert [s["AGENT"] for s in plan["STEPS"]] == ["Disruption Detection", "Supplier Evaluation",
                                                   "Action"]
    assert [s["NO"] for s in plan["STEPS"]] == [1, 2, 3]
    assert all(s["WHY"] and s["STATUS"] == "PLANNED" for s in plan["STEPS"])
    assert plan["TRIGGER"]["TYPE"] == trigger and plan["CASES_SHOWN"] == shown
    assert plan["NOT_IN_LIVE_FLOW"] == ["Evaluation and Learning: offline, see eval/monte_carlo.py"]
    text = sup.format_plan(plan)
    assert "Evaluation and Learning: offline, see eval/monte_carlo.py" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_plan_with_a_choice_checks_it_before_the_draft(adapter):
    plan = build_plan("S1", adapter.scenarios(), choose="100108")
    assert [s["STEP"] for s in plan["STEPS"]] == [DETECTION, SUPPLIER_EVALUATION,
                                                  INTEGRITY_GUARD, ACTION]


def test_unknown_scenario_is_refused(adapter):
    with pytest.raises(SystemExit):
        build_plan("S9", adapter.scenarios())


def test_graph_is_strands_with_python_nodes(adapter):
    s = supervisor(adapter, "S1")
    graph = s.build_graph()
    assert isinstance(graph, Graph)
    assert all(isinstance(node.executor, StepNode) and isinstance(node.executor, MultiAgentBase)
               for node in graph.nodes.values())
    assert sorted(e.from_node.node_id + ">" + e.to_node.node_id for e in graph.edges) == [
        "detection>supplier_evaluation", "supplier_evaluation>action"]


# ------------------------------------------------------- FR-SUP-2 stop rules
def test_nothing_flagged_stops_after_detection(adapter):
    s = supervisor(adapter, "scan")
    plan = s.run()
    assert statuses(plan) == {DETECTION: "DONE", SUPPLIER_EVALUATION: "NOT_RUN",
                              ACTION: "NOT_RUN"}
    assert plan["OUTCOME"]["CODE"] == "NOTHING_FLAGGED" and "no action needed" in \
        plan["OUTCOME"]["TEXT"]
    assert s.evaluation is None and s.store.all() == []


def test_keep_everywhere_stops_after_supplier_evaluation(adapter):
    s = supervisor(adapter, "S2")
    plan = s.run()
    assert statuses(plan)[ACTION] == "NOT_RUN"
    assert plan["OUTCOME"]["CODE"] == "KEEP_EVERYWHERE"
    assert plan["OUTCOME"]["HANDED_BACK_TO"] == ["buyer"]
    assert "accepts the revised price" in plan["CASES"][0]["TEXT"]
    assert s.store.all() == []


def test_no_candidate_case_goes_back_to_the_buyer_others_go_on(adapter):
    s = supervisor(adapter, "all", NoCandidates, {"ebelns": ["4500000433"]})
    plan = s.run()
    assert statuses(plan)[ACTION] == "DONE"
    by_po = {c["EBELN"]: c for c in plan["CASES"]}
    assert by_po["4500000433"]["OUTCOME"] == "NO_CANDIDATE"
    assert "no candidate supplier" in by_po["4500000433"]["TEXT"]
    assert by_po["4500000431"]["OUTCOME"] == "MANAGER_APPROVAL"
    assert all(po["REPLACES_EBELN"] != "4500000433" for po in s.store.all())


def test_no_candidate_anywhere_stops_after_supplier_evaluation(adapter):
    s = supervisor(adapter, "S1", NoCandidates, {"ebelns": ["4500000431"]})
    plan = s.run()
    assert plan["OUTCOME"]["CODE"] == "NOTHING_TO_DRAFT"
    assert plan["CASES"][0]["OUTCOME"] == "NO_CANDIDATE" and statuses(plan)[ACTION] == "NOT_RUN"


def test_policy_deny_submits_and_hands_back_to_the_manager(adapter):
    s = supervisor(adapter, "S1")
    plan = s.run()
    assert plan["OUTCOME"]["CODE"] == "HANDED_BACK"
    assert plan["OUTCOME"]["HANDED_BACK_TO"] == ["manager"]
    assert plan["CASES"][0]["OUTCOME"] == "MANAGER_APPROVAL"
    [po] = s.store.all()
    assert po["STATUS"] == "PENDING_APPROVAL" and po["APPROVAL_REQUEST_ID"] == f"APR-{po['EBELN']}"
    assert "does not wait" in plan["OUTCOME"]["TEXT"]


def test_short_justification_stops_before_action(adapter):
    s = supervisor(adapter, "S1", choose="100108", justification="cheaper")
    plan = s.run()
    assert statuses(plan) == {DETECTION: "DONE", SUPPLIER_EVALUATION: "DONE",
                              INTEGRITY_GUARD: "DONE", ACTION: "NOT_RUN"}
    assert plan["OUTCOME"]["CODE"] == "OVERRIDE_REJECTED" and s.store.all() == []


def test_valid_override_is_logged_then_drafted(adapter):
    s = supervisor(adapter, "S1", choose="100108", justification=GOOD_REASON)
    plan = s.run()
    assert statuses(plan)[INTEGRITY_GUARD] == "DONE"
    [po] = s.store.all()
    assert po["LIFNR"] == "100108" and s.choice["OVERRIDE_ID"] in po["REASON"]
    assert "GESIT Integrity flags" in s.report_text()


def test_choosing_number_one_skips_the_integrity_guard(adapter):
    s = supervisor(adapter, "S1", choose="100109")
    plan = s.run()
    assert statuses(plan)[INTEGRITY_GUARD] == "SKIPPED" and s.choice is None
    assert s.store.all()[0]["LIFNR"] == "100109"


def test_supervisor_never_releases(adapter, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the supervisor called release_po")

    import agents.action
    import tools
    import tools.action
    for module in (agents.action, tools, tools.action):
        monkeypatch.setattr(module, "release_po", refuse)
    s = supervisor(adapter, "all")
    s.run()
    assert all(po["STATUS"] in ("DRAFT", "PENDING_APPROVAL") for po in s.store.all())
    assert not any(e["TOOL"] == "release_po" for po in s.store.all() for e in po["HISTORY"])


def test_failed_step_is_marked_and_raised(adapter):
    s = supervisor(adapter, "S1")
    s.evaluator.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        s.run()
    assert statuses(s.plan) == {DETECTION: "DONE", SUPPLIER_EVALUATION: "FAILED",
                                ACTION: "NOT_RUN"}


# ---------------------------------------------------------- CLI and plan log
@pytest.fixture
def offline(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("SAP_RPT_MODE", "none")


def test_plan_log_line_is_json_with_every_step(offline, logs, capsys):
    assert sup.main(["S1", "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("GESIT Supervisor plan PLAN-001")
    lines = (logs / "plan_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    plan = json.loads(lines[0])
    assert plan == read_plans()[0]
    assert [s["STEP"] for s in plan["STEPS"]] == [DETECTION, SUPPLIER_EVALUATION, ACTION]
    assert all(s["STATUS"] == "DONE" and isinstance(s["SECONDS"], float) and s["RESULT"]
               for s in plan["STEPS"])
    assert plan["OUTCOME"]["CODE"] == "HANDED_BACK" and plan["TOTAL_SECONDS"] >= 0
    assert plan["PO_STORE"] == str(logs / "po_store.json")
    sup.main(["scan", "--no-llm"])
    assert [p["PLAN_ID"] for p in read_plans()] == ["PLAN-001", "PLAN-002"]


def test_cli_prints_the_agents_run_report(offline, capsys):
    run_cli.main(["S1", "--no-llm"])
    expected = capsys.readouterr().out
    sup.main(["S1", "--no-llm"])
    out = capsys.readouterr().out
    assert expected.rstrip("\n") in out
    assert out.index("GESIT Supervisor plan") < out.index("GESIT Disruption Detection") \
        < out.index("GESIT Supervisor outcome")


def test_cli_rejected_override_returns_1(offline, capsys):
    assert sup.main(["S1", "--no-llm", "--choose", "100108", "--justification", "short"]) == 1
    assert "OVERRIDE_REJECTED" in capsys.readouterr().out
