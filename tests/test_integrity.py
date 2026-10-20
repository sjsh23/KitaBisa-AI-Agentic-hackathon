"""Integrity Guard: pattern flag, override rule and log, neutral wording, demo CLI
(FR-INT-1 to FR-INT-4, FR-SUP-2, D-1, D-3). Every test logs to a temp DECISION_LOG_PATH."""
import copy
import json
import re

import pytest
from scipy.stats import binom

from adapters import CsvAdapter
from agents import integrity as integrity_cli
from agents.action import ActionAgent
from agents.detection import DetectionAgent
from agents.evaluation import EvaluationAgent
from agents.run import cases_for, notices_on_file
from integrations.sap.rpt_client import RptClient
from scoring import integrity
from tools import check_override, get_integrity_flags, record_override
from tools.integrity_tools import (MIN_JUSTIFICATION_CHARS, find_option, keep_label,
                                   read_overrides, recommended_label)

MATNR = "RM-GULA-001"
FORBIDDEN = ("fraud", "suspicious", "violation", "guilty", "corrupt")
LOG_FIELDS = {"TIMESTAMP", "OVERRIDE_ID", "BUYER", "EBELN", "RECOMMENDED", "CHOSEN",
              "EXPECTED_COST_RECOMMENDED", "EXPECTED_COST_CHOSEN", "COST_DIFFERENCE",
              "JUSTIFICATION", "DATA_NOTE"}
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


@pytest.fixture(autouse=True)
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "decision_log.jsonl"
    monkeypatch.setenv("DECISION_LOG_PATH", str(path))
    return path


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


@pytest.fixture(scope="module")
def s1(adapter):
    rpt = StubRpt()
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt, reasoner=lambda f: "ok").detect(
        MATNR, notices_on_file("S1", adapter.scenarios()))
    return EvaluationAgent(adapter=adapter, rpt_client=rpt, explainer=lambda f: "ok").evaluate(
        detection, detection["FLAGGED_POS"], cases_for("S1", adapter.scenarios()))


@pytest.fixture
def case(s1):
    return copy.deepcopy(s1["CASES"][0])


def keep_first_case():
    """A minimal Evaluation case where keeping the current PO ranks #1."""
    return {"EBELN": "4500000432", "SCORING": {"CANDIDATES": [
        {"RANK": 1, "OPTION": "KEEP_CURRENT_PO", "KEEP_EBELN": "4500000432", "LIFNR": "100103",
         "EXPECTED_COST": 149_600_000},
        {"RANK": 2, "OPTION": "NEW_PO", "LIFNR": "100109", "EXPECTED_COST": 167_000_000},
    ]}}


# --------------------------------------------------------- Part A: FR-INT-3
def test_only_buyer03_with_100107_flagged_on_simulated_ekko(adapter):
    scan = integrity.scan_pairs(adapter.table("EKKO"))
    assert [(p.buyer, p.supplier) for p in scan.flagged] == [("BUYER03", "100107")]
    assert scan.pairs_tested == 40 and scan.pairs_flagged == 1   # 4 buyers x 10 suppliers
    pair = scan.flagged[0]
    assert (pair.k, pair.n_b) == (27, 113)
    assert pair.ratio >= 2 and pair.p_value < 0.01


def test_binomial_tail_matches_scipy():
    for k, n, p in ((27, 113, 0.031), (5, 20, 0.125), (0, 10, 0.3), (19, 100, 0.1)):
        assert integrity.binomial_tail(k, n, p) == pytest.approx(binom.sf(k - 1, n, p), rel=1e-9)


def test_k_4_not_flagged():
    pair = integrity.pair_stats("B", "S", 4, 10, 1, 100)
    assert pair.ratio >= 2 and pair.p_value < 0.01 and not pair.flagged


def test_ratio_1_9_not_flagged():
    pair = integrity.pair_stats("B", "S", 19, 100, 10, 100)
    assert pair.ratio == pytest.approx(1.9) and pair.p_value < 0.01 and not pair.flagged


def test_p_at_least_0_01_not_flagged():
    pair = integrity.pair_stats("B", "S", 5, 20, 25, 200)
    assert pair.k >= 5 and pair.ratio >= 2 and pair.p_value >= 0.01 and not pair.flagged


def test_share_others_zero_does_not_crash():
    pair = integrity.pair_stats("B", "S", 6, 20, 0, 100)
    assert pair.share_others == 0 and pair.ratio is None and pair.p_value == 0.0
    assert pair.flagged
    alone = integrity.pair_stats("B", "S", 6, 20, 0, 0)   # no other buyer at all
    assert alone.ratio is None
    assert "no other buyer" in integrity.review_text(pair)


# ----------------------------------------------------- Part B: FR-INT-1, 2
def test_justification_length_rule():
    assert not check_override("100109", "100108", "x" * 19)["ALLOWED"]
    assert check_override("100109", "100108", "x" * 20)["ALLOWED"]
    assert not check_override("100109", "100108", " " * 25)["ALLOWED"]
    assert not check_override("100109", "100108", None)["ALLOWED"]
    assert MIN_JUSTIFICATION_CHARS == 20


def test_same_choice_needs_nothing_and_writes_nothing(case, log_path):
    check = check_override("100109", "100109", "")
    assert check["ALLOWED"] and not check["OVERRIDE"]
    with pytest.raises(ValueError):
        record_override(case["EBELN"], "100109", "100109", GOOD_REASON, case=case, buyer="BUYER01")
    assert not log_path.exists()


def test_override_writes_one_line_with_all_fields(case, log_path):
    assert recommended_label(case) == "100109"
    override_id = record_override(case["EBELN"], "100109", "100108", f"  {GOOD_REASON}  ",
                                  case=case, buyer="BUYER01")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == LOG_FIELDS
    assert record["OVERRIDE_ID"] == override_id == "OVR-4500000431-001"
    assert record["TIMESTAMP"].endswith("+07:00")                 # Asia/Jakarta
    assert (record["BUYER"], record["EBELN"]) == ("BUYER01", "4500000431")
    assert record["JUSTIFICATION"] == GOOD_REASON                 # stripped
    # costs copied from the ranked rows, never recomputed
    assert record["EXPECTED_COST_RECOMMENDED"] == find_option(case, "100109")["EXPECTED_COST"]
    assert record["EXPECTED_COST_CHOSEN"] == find_option(case, "100108")["EXPECTED_COST"]


def test_cost_difference_is_chosen_minus_recommended(case):
    record_override(case["EBELN"], "100109", "100108", GOOD_REASON, case=case, buyer="BUYER01")
    record = read_overrides()[0]
    assert record["COST_DIFFERENCE"] > 0                          # 100108 costs more
    assert record["COST_DIFFERENCE"] == (record["EXPECTED_COST_CHOSEN"]
                                         - record["EXPECTED_COST_RECOMMENDED"])


def test_choosing_keep_when_new_order_recommended(case):
    chosen = keep_label(case["EBELN"])
    assert not check_override("100109", chosen, "short")["ALLOWED"]
    record_override(case["EBELN"], "100109", chosen, GOOD_REASON, case=case, buyer="BUYER01")
    assert read_overrides()[0]["CHOSEN"] == "keep PO 4500000431"


def test_overriding_a_keep_recommendation():
    case = keep_first_case()
    recommended = recommended_label(case)
    assert recommended == "keep PO 4500000432"
    assert not check_override(recommended, "100109", "too short")["ALLOWED"]
    record_override("4500000432", recommended, "100109", GOOD_REASON, case=case, buyer="BUYER02")
    record = read_overrides()[0]
    assert (record["RECOMMENDED"], record["CHOSEN"]) == ("keep PO 4500000432", "100109")
    assert record["COST_DIFFERENCE"] == 167_000_000 - 149_600_000


def test_record_override_refuses_bad_input(case, log_path):
    with pytest.raises(ValueError):   # short justification
        record_override(case["EBELN"], "100109", "100108", "too short", case=case, buyer="B")
    with pytest.raises(ValueError):   # recommended is not the case's #1
        record_override(case["EBELN"], "100103", "100108", GOOD_REASON, case=case, buyer="B")
    with pytest.raises(ValueError):   # not a ranked option (off panel)
        record_override(case["EBELN"], "100109", "100111", GOOD_REASON, case=case, buyer="B")
    assert not log_path.exists()


def test_action_drafts_choice_only_with_override(adapter, s1, case):
    report = {**s1, "CASES": [case]}
    with pytest.raises(ValueError):
        ActionAgent(adapter=adapter).act(report, choices={case["EBELN"]: {"CHOSEN": "100108"}})
    override_id = record_override(case["EBELN"], "100109", "100108", GOOD_REASON, case=case,
                                  buyer="BUYER01")
    out = ActionAgent(adapter=adapter).act(
        report, choices={case["EBELN"]: {"CHOSEN": "100108", "OVERRIDE_ID": override_id}})
    order = out["CASES"][0]["ORDERS"][0]
    assert order["LIFNR"] == "100108" and order["NEXT"] == "MANAGER_APPROVAL"   # POL-1


def test_action_keep_choice_drafts_nothing(adapter, s1, case):
    chosen = keep_label(case["EBELN"])
    override_id = record_override(case["EBELN"], "100109", chosen, GOOD_REASON, case=case,
                                  buyer="BUYER01")
    out = ActionAgent(adapter=adapter).act(
        {**s1, "CASES": [case]},
        choices={case["EBELN"]: {"CHOSEN": chosen, "OVERRIDE_ID": override_id}})
    assert out["CASES"][0]["ORDERS"] == [] and out["CASES"][0]["OLD_PO"]["ACTION"] == "KEEP"


def test_get_integrity_flags_counts_overrides(adapter, case):
    record_override(case["EBELN"], "100109", "100108", GOOD_REASON, case=case, buyer="BUYER03")
    record_override(case["EBELN"], "100109", "100105", GOOD_REASON, case=case, buyer="BUYER01")
    flags = get_integrity_flags(adapter=adapter)
    assert (flags["PAIRS_TESTED"], flags["PAIRS_FLAGGED"]) == (40, 1)
    assert flags["FLAGS"][0]["BUYER"] == "BUYER03" and flags["FLAGS"][0]["OVERRIDE_COUNT"] == 1
    assert flags["OVERRIDES_BY_BUYER"] == {"BUYER01": 1, "BUYER03": 1}
    assert flags["OVERRIDES_BY_CHOSEN"] == {"100105": 1, "100108": 1}
    assert read_overrides()[1]["OVERRIDE_ID"] == "OVR-4500000431-002"


# ------------------------------------------------------- Part C: FR-INT-4
def _neutral(text):
    lowered = text.lower()
    assert not [w for w in FORBIDDEN if w in lowered], text
    assert chr(0x2013) not in text and chr(0x2014) not in text   # en and em dash


def test_review_text_is_neutral(adapter):
    flags = get_integrity_flags(adapter=adapter)
    text = flags["FLAGS"][0]["REVIEW_TEXT"]
    assert text.startswith("Pattern for review: BUYER03 placed 27 of 113 POs (23.9%) with 100107")
    assert "simulated data" in text and text.endswith("This is a statistic, not a finding.")
    _neutral(text)
    _neutral(integrity_cli.format_flags(flags))
    _neutral(integrity.review_text(integrity.pair_stats("B", "S", 6, 20, 0, 100)))


# ------------------------------------------------------------ Part D: CLI
@pytest.fixture
def cli(monkeypatch, capsys):
    # Never call the network and never load the real .env into the test process (NFR-4).
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(integrity_cli, "RptClient", StubRpt)

    def run(*argv):
        code = integrity_cli.main(["S1", *argv])
        return code, capsys.readouterr().out
    return run


def test_cli_recommended_option(cli, log_path):
    code, out = cli()
    assert code == 0 and "No override, nothing logged." in out
    assert re.search(r"4500000435\s+100109", out) and "BUYER03  100107" in out
    assert not log_path.exists()
    _neutral(out)


def test_cli_short_justification_rejected(cli, log_path):
    code, out = cli("--choose", "100108", "--justification", "cheaper")
    assert code == 1 and "Rejected" in out and "No draft PO was created." in out
    assert "GESIT Action" not in out and not log_path.exists()


def test_cli_valid_override(cli, log_path):
    code, out = cli("--buyer", "BUYER01", "--choose", "100108", "--justification", GOOD_REASON)
    assert code == 0 and "OVERRIDE_ID: OVR-4500000431-001" in out
    assert re.search(r"4500000435\s+100108", out) and "APR-4500000435" in out
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1
