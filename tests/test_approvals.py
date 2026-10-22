"""Manager approval and the reject path across processes (FR-ACT-2, FR-ACT-3, POL-1, POL-4,
FR-INT-1 reason rule; docs/prd_change_proposals.md item 11).

"Two processes" is two store objects on the same JSON file: the supervisor's store
drafts and submits, a new PoStore loads the file and the manager decides on it.
Offline: SAP-RPT is a stub, no model text. Logs and the store live in a temp folder.
"""
import json

import pytest

from adapters import CsvAdapter, PoStore
from adapters.po_store import clear_po_store, po_store_path
from agents import approvals
from agents.detection import DetectionAgent
from agents.supervisor import Supervisor
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient
from policy import AGENT, Principal, buyer, manager
from tools import list_pending_approvals, reject_po, release_po
from tools.action import next_option
from tools.integrity_tools import read_overrides, read_rejections

REASON = "Price too high for this quarter, ask buyer for option 2"
OVERRIDE_REASON = "100109 has only 4 receipts, we prefer a long track record"
MANAGER = manager("MGR07")   # any manager id: nothing in the tools expects MANAGER01


class StubRpt(RptClient):
    """SAP-RPT stand-in: the same P(late) for every row."""

    def __init__(self):
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        return {r["EBELN"]: 0.2 for r in query_rows}


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "decision_log.jsonl"))
    monkeypatch.delenv("PLAN_LOG_PATH", raising=False)
    monkeypatch.delenv("PO_STORE_PATH", raising=False)
    return tmp_path


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def supervise(adapter, scenario="S1", **kw):
    """Process 1: the supervisor drafts and submits into the saved store."""
    rpt = StubRpt()
    s = Supervisor(scenario, adapter=adapter, rpt_client=rpt, use_llm=False,
                   store=PoStore(adapter, path=po_store_path()),
                   detector=DetectionAgent(adapter=adapter, rpt_client=rpt,
                                           reasoner=lambda f: "ok"),
                   evaluator=SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt,
                                                     explainer=lambda f: "ok"), **kw)
    s.run()
    return s


def reopen(adapter):
    """Process 2: a fresh store object on the same file."""
    return PoStore(adapter, path=po_store_path())


def pending_s1(adapter):
    s = supervise(adapter)
    [po] = [p for p in s.store.all() if p["STATUS"] == "PENDING_APPROVAL"]
    assert po["REPLACES_EBELN"] == "4500000431"
    return s, po["EBELN"]


# ---------------------------------------------------------------- the store
def test_store_saves_and_loads(adapter, logs):
    s, ebeln = pending_s1(adapter)
    assert po_store_path() == logs / "po_store.json"
    assert json.loads(po_store_path().read_text(encoding="utf-8"))["POS"][0]["EBELN"] == ebeln
    store = reopen(adapter)
    assert store.get(ebeln) == s.store.get(ebeln)
    assert store.get(ebeln)["RANKING"] and store.get(ebeln)["DRAFT_RANK"] == 1


def test_numbers_continue_from_the_file(adapter):
    _, first = pending_s1(adapter)
    second = supervise(adapter).store.all()[-1]["EBELN"]
    assert (first, second) == ("4500000435", "4500000436")


def test_memory_mode_writes_nothing(adapter, logs):
    store = PoStore(adapter)
    assert store.path is None
    Supervisor("S1", adapter=adapter, rpt_client=StubRpt(), use_llm=False, store=store).run()
    assert store.all() and not (logs / "po_store.json").exists()


def test_clear_po_store(adapter):
    pending_s1(adapter)
    clear_po_store()
    assert not po_store_path().exists() and reopen(adapter).all() == []
    clear_po_store()   # clearing twice is fine


def test_po_store_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PO_STORE_PATH", str(tmp_path / "x" / "pos.json"))
    assert po_store_path() == tmp_path / "x" / "pos.json"


# ------------------------------------------------------------ approve path
def test_s1_across_two_processes_approved(adapter):
    _, ebeln = pending_s1(adapter)
    store = reopen(adapter)
    listing = list_pending_approvals(store=store, principal=MANAGER)
    [row] = listing["PENDING"]
    assert (row["EBELN"], row["APPROVAL_REQUEST_ID"], row["LIFNR"], row["NETWR"],
            row["REPLACES_EBELN"]) == (ebeln, f"APR-{ebeln}", "100109", 175_500_000, "4500000431")
    assert "GESIT rank 1" in row["REASON"] and row["SUBMITTED_AT"]
    code, lines = approvals.approve(store, ebeln, MANAGER)
    assert code == 0 and "RELEASED by MGR07" in lines[0]
    assert "Old PO 4500000431: cancel" in lines[1]
    after = reopen(adapter).get(ebeln)
    assert (after["STATUS"], after["RELEASED_BY"]) == ("RELEASED", "MGR07")
    assert list_pending_approvals(store=reopen(adapter), principal=MANAGER)["PENDING"] == []


# ------------------------------------------------------------- reject path
def test_s1_across_two_processes_rejected(adapter):
    _, ebeln = pending_s1(adapter)
    code, lines = approvals.reject(reopen(adapter), ebeln, REASON, MANAGER)
    assert code == 0
    text = "\n".join(lines)
    assert f"PO {ebeln}" in text and "REJECTED by MGR07" in text
    assert "never released" in text and "Old PO 4500000431: untouched" in text
    assert "Next option for the buyer" in text and "rank 2: " in text
    assert "Not drafted and not scored again" in text
    after = reopen(adapter).get(ebeln)
    assert (after["STATUS"], after["REJECTED_BY"], after["REJECT_REASON"]) == (
        "REJECTED", "MGR07", REASON)
    event = after["HISTORY"][-1]
    assert (event["TOOL"], event["RESULT"], event["BY"]) == ("reject_po", "REJECTED", "MGR07")
    assert event["AT"] and event["REJECT_REASON"] == REASON
    assert not any(e["RESULT"] == "RELEASED" for e in after["HISTORY"])
    assert len(reopen(adapter).all()) == 1   # the next option was not drafted


def test_next_option_is_the_next_row_of_the_saved_table(adapter):
    _, ebeln = pending_s1(adapter)
    po = reopen(adapter).get(ebeln)
    row = next_option(po)
    assert row == sorted(po["RANKING"], key=lambda r: r["RANK"])[1]
    result = reject_po(ebeln, REASON, store=reopen(adapter), principal=MANAGER)
    assert result["NEXT_OPTION"] == row and result["NEXT_OPTION_LABEL"] == row["LIFNR"]


def test_next_option_after_a_rejected_override_can_be_number_one(adapter):
    s = supervise(adapter, choose="100108", justification=OVERRIDE_REASON)
    [po] = s.store.all()
    assert po["LIFNR"] == "100108" and po["STATUS"] == "PENDING_APPROVAL"
    assert (next_option(po)["RANK"], next_option(po)["LIFNR"]) == (1, "100109")


def test_next_option_keep_when_it_ranked_higher():
    po = {"DRAFT_RANK": 1, "RANKING": [
        {"RANK": 2, "OPTION": "KEEP_CURRENT_PO", "LIFNR": "100103", "KEEP_EBELN": "4500000432"},
        {"RANK": 3, "OPTION": "NEW_PO", "LIFNR": "100102"},
        {"RANK": 1, "OPTION": "NEW_PO", "LIFNR": "100109"}]}
    assert next_option(po)["OPTION"] == "KEEP_CURRENT_PO"
    assert next_option({"DRAFT_RANK": 1, "RANKING": []}) is None


def test_reject_goes_to_the_decision_log_as_its_own_type(adapter):
    _, ebeln = pending_s1(adapter)
    result = reject_po(ebeln, REASON, store=reopen(adapter), principal=MANAGER)
    [entry] = read_rejections()
    assert entry["ENTRY_TYPE"] == "REJECT" and entry["REJECT_ID"] == result["REJECT_ID"]
    assert (entry["EBELN"], entry["REPLACES_EBELN"], entry["REJECTED_BY"], entry["BUYER"]) == (
        ebeln, "4500000431", "MGR07", "BUYER01")
    assert entry["NEXT_OPTION"] == result["NEXT_OPTION_LABEL"]
    assert entry["DATA_NOTE"] == "simulated data"
    assert read_overrides() == []   # a reject is not an override


def test_override_entries_unchanged_next_to_rejects(adapter):
    s = supervise(adapter, choose="100108", justification=OVERRIDE_REASON)
    [po] = s.store.all()
    reject_po(po["EBELN"], REASON, store=reopen(adapter), principal=MANAGER)
    [override] = read_overrides()
    assert "ENTRY_TYPE" not in override and override["OVERRIDE_ID"] == "OVR-4500000431-001"
    assert len(read_rejections()) == 1


# ------------------------------------------------- who may approve or reject
def test_agent_is_denied_approve_and_reject(adapter):
    _, ebeln = pending_s1(adapter)
    store = reopen(adapter)
    assert release_po(ebeln, store=store, principal=AGENT)["POLICY_DECISION"]["POLICY"] == "POL-4"
    denied = reject_po(ebeln, REASON, store=store, principal=Principal("other-agent", "agent"))
    assert (denied["RESULT"], denied["POLICY_DECISION"]["POLICY"]) == ("DENIED", "POL-4")
    code, _ = approvals.approve(store, ebeln, AGENT)
    assert code == 1
    assert reopen(adapter).get(ebeln)["STATUS"] == "PENDING_APPROVAL"
    assert read_rejections() == []


def test_buyer_cannot_reject(adapter):
    _, ebeln = pending_s1(adapter)
    result = reject_po(ebeln, REASON, store=reopen(adapter), principal=buyer("BUYER01"))
    assert (result["RESULT"], result["POLICY_DECISION"]["POLICY"]) == ("DENIED", "POL-1")
    assert reopen(adapter).get(ebeln)["STATUS"] == "PENDING_APPROVAL"


def test_buyer_cannot_approve_above_the_threshold(adapter):
    _, ebeln = pending_s1(adapter)
    code, lines = approvals.approve(reopen(adapter), ebeln, buyer("BUYER01"))
    assert code == 1 and "POL-1" in lines[0]


def test_short_reason_is_refused(adapter):
    _, ebeln = pending_s1(adapter)
    before = reopen(adapter).get(ebeln)
    result = reject_po(ebeln, "  too expensive      ", store=reopen(adapter), principal=MANAGER)
    assert result["RESULT"] == "REFUSED" and "at least 20 characters" in result["MESSAGE"]
    assert reopen(adapter).get(ebeln) == before and read_rejections() == []


# ----------------------------------------------------------- double actions
@pytest.mark.parametrize("first, second", [
    ("approve", "approve"), ("reject", "reject"), ("reject", "approve"), ("approve", "reject")])
def test_double_actions_change_nothing(adapter, first, second):
    _, ebeln = pending_s1(adapter)
    do = {"approve": lambda st: approvals.approve(st, ebeln, MANAGER),
          "reject": lambda st: reject_po(ebeln, REASON, store=st, principal=MANAGER)}
    do[first](reopen(adapter))
    before = reopen(adapter).get(ebeln)
    log_before = read_rejections()
    if second == "approve":
        code, lines = approvals.approve(reopen(adapter), ebeln, MANAGER)
        assert code == 1 and "only a PENDING_APPROVAL PO can be approved" in lines[0]
        with pytest.raises(ValueError):   # the tool itself refuses too
            release_po(ebeln, store=reopen(adapter), principal=MANAGER)
    else:
        with pytest.raises(ValueError, match="only a PENDING_APPROVAL PO can be rejected"):
            reject_po(ebeln, REASON, store=reopen(adapter), principal=MANAGER)
    assert reopen(adapter).get(ebeln) == before and read_rejections() == log_before


# ---------------------------------------------------------------------- CLI
@pytest.fixture
def offline(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("SAP_RPT_MODE", "none")


def test_cli_list_reject_then_approve_a_new_draft(offline, capsys):
    from agents import supervisor
    supervisor.main(["S1", "--no-llm"])
    capsys.readouterr()
    assert approvals.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "APR-4500000435" in out and "175,500,000" in out
    assert approvals.main(["reject", "4500000435", "--as", "MANAGER01", "--reason", REASON]) == 0
    out = capsys.readouterr().out
    assert "REJECTED by MANAGER01" in out and "Next option for the buyer" in out
    assert approvals.main(["list"]) == 0
    assert "No PO is waiting for approval." in capsys.readouterr().out
    supervisor.main(["S1", "--no-llm"])
    capsys.readouterr()
    assert approvals.main(["approve", "4500000436", "--as", "MANAGER01"]) == 0
    assert "approved and RELEASED by MANAGER01" in capsys.readouterr().out
    assert approvals.main(["approve", "4500000436", "--as", "MANAGER01"]) == 1
    assert "Error: PO 4500000436 is RELEASED" in capsys.readouterr().out
    assert approvals.main(["approve", "4500009999", "--as", "MANAGER01"]) == 1
    assert "was not created by GESIT" in capsys.readouterr().out


def test_cli_needs_an_approver(offline):
    with pytest.raises(SystemExit):
        approvals.main(["approve", "4500000435"])
    with pytest.raises(SystemExit):
        approvals.main(["reject", "4500000435", "--as", "MANAGER01"])   # no reason
