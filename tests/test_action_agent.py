"""Action tools and agent on the simulated scenarios (FR-ACT-1 to FR-ACT-3, POL-1 to POL-4)."""
import copy
import json

import pytest

from adapters import CsvAdapter, PoStore
from agents.action import (NEXT_BUYER, NEXT_DONE, NEXT_MANAGER, ActionAgent, human_approvals,
                           order_parts)
from agents.detection import DetectionAgent
from agents.evaluation import EvaluationAgent
from agents.run import cases_for, format_action, main, notices_on_file
from integrations.sap.rpt_client import RptClient
from policy import AGENT, buyer, manager
from tools import draft_po, release_po, submit_for_approval

MATNR = "RM-GULA-001"


class StubRpt(RptClient):
    """SAP-RPT stand-in: the same P(late) for every row."""

    def __init__(self):
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        return {r["EBELN"]: 0.2 for r in query_rows}


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def evaluation(adapter, scenario):
    rpt = StubRpt()
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt, reasoner=lambda f: "ok").detect(
        MATNR, notices_on_file(scenario, adapter.scenarios()))
    return EvaluationAgent(adapter=adapter, rpt_client=rpt, explainer=lambda f: "ok").evaluate(
        detection, detection["FLAGGED_POS"], cases_for(scenario, adapter.scenarios()))


def draft(store, lifnr="100109", qty=7_200.0, netpr=17_550, principal=AGENT):
    return draft_po(lifnr, MATNR, qty, netpr, "2026-10-03", "test", store=store,
                    ernam="BUYER01", bedat="2026-09-30", replaces="4500000431",
                    principal=principal)


# ------------------------------------------------------------------- tools
def test_draft_po_fills_s4_fields(adapter):
    store = PoStore(adapter)
    result = draft(store)
    po = result["PO"]
    assert result["RESULT"] == "DRAFTED" and po["STATUS"] == "DRAFT"
    assert po["EBELN"] == "4500000435"             # continues after the last PO in EKKO
    assert (po["LIFNR"], po["MATNR"], po["MENGE"], po["NETPR"]) == ("100109", MATNR, 7200.0, 17550)
    assert po["NETWR"] == 126_360_000 and isinstance(po["NETWR"], int)
    assert (po["BEDAT"], po["EINDT"], po["ERNAM"]) == ("2026-09-30", "2026-10-03", "BUYER01")
    assert (po["BUKRS"], po["EKORG"], po["WERKS"], po["MEINS"], po["WAERS"]) == (
        "1000", "1000", "1100", "KG", "IDR")       # copied from the replaced PO (D-2)
    assert po["REPLACES_EBELN"] == "4500000431"
    assert draft(store)["EBELN"] == "4500000436"


def test_draft_po_denied_off_panel(adapter):
    store = PoStore(adapter)
    result = draft(store, lifnr="100111")
    assert result["RESULT"] == "DENIED" and result["POLICY_DECISION"]["POLICY"] == "POL-2"
    assert store.all() == []


def test_release_below_threshold_by_buyer(adapter):
    store = PoStore(adapter)
    ebeln = draft(store)["EBELN"]
    assert release_po(ebeln, store=store, principal=AGENT)["POLICY_DECISION"]["POLICY"] == "POL-4"
    result = release_po(ebeln, store=store, principal=buyer("BUYER01"))
    assert result["RESULT"] == "RELEASED"
    po = store.get(ebeln)
    assert (po["STATUS"], po["RELEASED_BY"]) == ("RELEASED", "BUYER01")
    assert [e["RESULT"] for e in po["HISTORY"]] == ["DRAFT", "DENIED", "RELEASED"]
    with pytest.raises(ValueError):
        release_po(ebeln, store=store, principal=buyer("BUYER01"))


def test_above_threshold_goes_to_the_manager(adapter):
    store = PoStore(adapter)
    ebeln = draft(store, qty=10_000.0)["EBELN"]     # Rp175,500,000
    denied = release_po(ebeln, store=store, principal=buyer("BUYER01"))
    assert denied["RESULT"] == "DENIED" and denied["POLICY_DECISION"]["POLICY"] == "POL-1"
    submitted = submit_for_approval(ebeln, store=store)
    assert submitted["APPROVAL_REQUEST_ID"] == f"APR-{ebeln}"
    assert submit_for_approval(ebeln, store=store)["APPROVAL_REQUEST_ID"] == f"APR-{ebeln}"
    assert store.get(ebeln)["STATUS"] == "PENDING_APPROVAL"
    assert release_po(ebeln, store=store, principal=manager())["RESULT"] == "RELEASED"


def test_store_returns_copies(adapter):
    store = PoStore(adapter)
    ebeln = draft(store)["EBELN"]
    store.get(ebeln)["STATUS"] = "RELEASED"
    assert store.get(ebeln)["STATUS"] == "DRAFT"
    with pytest.raises(KeyError):
        store.get("4500000001")   # a PO from history, not created by GESIT


# ------------------------------------------------------------------- agent
def test_s1_draft_is_routed_to_the_manager(adapter):
    agent = ActionAgent(adapter=adapter)
    report = agent.act(evaluation(adapter, "S1"))
    case = report["CASES"][0]
    order = case["ORDERS"][0]
    assert (case["EBELN"], case["BUYER"]) == ("4500000431", "BUYER01")
    assert order["NETWR"] > report["APPROVAL_THRESHOLD_IDR"]
    assert (order["STATUS"], order["NEXT"]) == ("PENDING_APPROVAL", NEXT_MANAGER)
    assert order["RELEASE_CHECK"]["POLICY"] == "POL-1"
    assert order["APPROVAL_REQUEST_ID"] == f"APR-{order['EBELN']}"
    assert report["HANDED_BACK"] is True
    assert [s["TOOL"] for s in report["STEPS"]] == ["draft_po", "policy_check",
                                                    "submit_for_approval"]
    # The agent itself never released anything (POL-4).
    assert all(e["TOOL"] != "release_po" for e in agent.store.get(order["EBELN"])["HISTORY"])


def test_s3_draft_waits_for_one_click(adapter):
    agent = ActionAgent(adapter=adapter)
    report = agent.act(evaluation(adapter, "S3"))
    order = report["CASES"][0]["ORDERS"][0]
    assert order["MENGE"] == 7_200.0 and order["NETWR"] <= report["APPROVAL_THRESHOLD_IDR"]
    assert (order["STATUS"], order["NEXT"]) == ("DRAFT", NEXT_BUYER)
    assert order["RELEASE_CHECK"]["ALLOWED"] is True
    assert "one-click" in format_action(report)


def test_drafts_follow_the_recommendation(adapter):
    ev = evaluation(adapter, "all")
    report = ActionAgent(adapter=adapter).act(ev)
    for case, acted in zip(ev["CASES"], report["CASES"]):
        best = case["SCORING"]["CANDIDATES"][0]
        if best["OPTION"] == "KEEP_CURRENT_PO":   # S2 in `all`: keeping PO 4500000432 wins
            assert acted["ORDERS"] == [] and "nothing drafted" in acted["NOTE"]
            continue
        assert [o["LIFNR"] for o in acted["ORDERS"]] == [p["LIFNR"] for p in order_parts(best)]
        assert sum(o["MENGE"] for o in acted["ORDERS"]) == case["QTY"]
    assert len({o["EBELN"] for c in report["CASES"] for o in c["ORDERS"]}) == 2


def test_split_recommendation_gets_two_drafts(adapter):
    ev = copy.deepcopy(evaluation(adapter, "S3"))
    best = ev["CASES"][0]["SCORING"]["CANDIDATES"][0]
    best.update(MENGE=2_500.0, SPLIT_WITH={"LIFNR": "100105", "MENGE": 4_700.0, "NETPR": 17_300,
                                           "ARRIVAL": "2026-10-05"})
    orders = ActionAgent(adapter=adapter).act(ev)["CASES"][0]["ORDERS"]
    assert [(o["LIFNR"], o["MENGE"]) for o in orders] == [(best["LIFNR"], 2_500.0),
                                                          ("100105", 4_700.0)]
    assert orders[1]["EINDT"] == "2026-10-05"


def test_no_candidate_drafts_nothing(adapter):
    ev = copy.deepcopy(evaluation(adapter, "S1"))
    ev["CASES"][0]["SCORING"]["CANDIDATES"] = []
    report = ActionAgent(adapter=adapter).act(ev)
    assert report["CASES"][0]["ORDERS"] == [] and report["HANDED_BACK"] is True
    assert "nothing drafted" in format_action(report)


def test_human_approvals_release_everything(adapter):
    agent = ActionAgent(adapter=adapter)
    report = human_approvals(agent.act(evaluation(adapter, "all")), agent.store)
    orders = [o for c in report["CASES"] for o in c["ORDERS"]]
    assert all(o["NEXT"] == NEXT_DONE and o["STATUS"] == "RELEASED" for o in orders)
    by = {o["EBELN"]: o["RELEASED_BY"] for o in orders}
    assert by[report["CASES"][0]["ORDERS"][0]["EBELN"]] == "MANAGER01"   # S1, above threshold
    assert {by[c["ORDERS"][0]["EBELN"]] for c in report["CASES"][1:] if c["ORDERS"]} == {"BUYER04"}
    # S2 drafted nothing (keeping PO 4500000432 ranks #1), so the buyer still decides.
    assert report["HANDED_BACK"] is True


def test_act_is_deterministic(adapter):
    ev = evaluation(adapter, "all")
    assert ActionAgent(adapter=adapter).act(ev) == ActionAgent(adapter=adapter).act(ev)


def test_scan_has_nothing_to_draft(adapter):
    report = ActionAgent(adapter=adapter).act(evaluation(adapter, "scan"))
    assert report["CASES"] == [] and "nothing to draft" in format_action(report)


def test_cli_prints_action_and_approvals(monkeypatch, capsys):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    main(["S1", "--no-llm"])
    out = capsys.readouterr().out
    assert "GESIT Action" in out and "denied by POL-1" in out and "APR-4500000435" in out
    main(["S1", "--no-llm", "--approve"])
    assert "Released by MANAGER01" in capsys.readouterr().out
    main(["S1", "--no-llm", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["POS"][0]["STATUS"] == "PENDING_APPROVAL"


# ------------------------------------------------- the old PO (proposal item 5)
@pytest.mark.parametrize("scenario, action, new_menge, words", [
    ("S1", "CANCEL", 0.0, "it would arrive 2026-10-11, 5 days after stock runs out"),
    # One world: S1 takes 100109's spare capacity first, so keeping PO 432 wins (as in `all`).
    ("S2", "KEEP", 8_000.0, "accepts the revised price Rp18,700 per kg (was Rp17,650)"),
    ("S3", "REDUCE", 4_800.0, "reduce from 12,000 to 4,800 kg (the part arriving 2026-10-01)"),
])
def test_every_case_says_what_happens_to_the_old_po(adapter, scenario, action, new_menge, words):
    report = ActionAgent(adapter=adapter).act(evaluation(adapter, scenario))
    old = report["CASES"][0]["OLD_PO"]
    assert (old["ACTION"], old["NEW_MENGE"]) == (action, new_menge)
    assert words in old["TEXT"] and "GESIT does not change the old PO" in old["TEXT"]
    assert old["TEXT"] in format_action(report)


def test_keep_recommendation_drafts_nothing(adapter):
    agent = ActionAgent(adapter=adapter)
    report = agent.act(evaluation(adapter, "all"))
    s2 = next(c for c in report["CASES"] if c["EBELN"] == "4500000432")
    assert s2["ORDERS"] == [] and s2["OLD_PO"]["ACTION"] == "KEEP"
    assert "accepts the revised price Rp18,700 per kg (was Rp17,650)" in s2["OLD_PO"]["TEXT"]
    assert all(po["REPLACES_EBELN"] != "4500000432" for po in agent.store.all())
