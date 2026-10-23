"""Monte Carlo against baselines (FR-EVA-4, D-1, simulated data). No network: SAP-RPT is a
stub that replays the P(late) values the real SAP-RPT-1.6 returned on 20 Oct 2026."""
import re
from pathlib import Path

import numpy as np
import pytest

from adapters import CsvAdapter
from agents import llm
from eval import monte_carlo as mc
from integrations.sap.rpt_client import RptClient

ROOT = Path(__file__).resolve().parent.parent

# P(late) from the real SAP-RPT-1.6 run of `agents.run all` on 20 Oct 2026 (simulated data):
# open POs by EBELN, hypothetical candidate rows by (LIFNR, MENGE).
REAL_OPEN_PO = {"4500000430": 0.29, "4500000431": 0.33, "4500000432": 0.20,
                "4500000433": 0.22, "4500000434": 0.17}
REAL_CANDIDATE = {
    ("100109", 10000.0): 0.22, ("100108", 10000.0): 0.19, ("100106", 10000.0): 0.18,
    ("100102", 10000.0): 0.18, ("100105", 10000.0): 0.20, ("100107", 10000.0): 0.29,
    ("100108", 7200.0): 0.21, ("100109", 2500.0): 0.22, ("100102", 7200.0): 0.18,
    ("100106", 7200.0): 0.21, ("100105", 7200.0): 0.21, ("100107", 7200.0): 0.29,
    ("100108", 8000.0): 0.18, ("100102", 8000.0): 0.16, ("100106", 8000.0): 0.19,
    ("100105", 8000.0): 0.21, ("100107", 8000.0): 0.28,
}


class RecordedRpt(RptClient):
    """SAP-RPT stand-in that replays recorded values and counts its calls."""

    def __init__(self):
        self.config = None
        self.calls = 0

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        self.calls += 1
        out = {}
        for r in query_rows:
            key = (r["LIFNR"], float(r["MENGE"]))
            out[r["EBELN"]] = REAL_OPEN_PO.get(r["EBELN"], REAL_CANDIDATE.get(key))
        return out


class OnTimeWorld:
    """A world where every delivery arrives on its promised date (true P(late) = 0)."""

    def sample_delivery(self, lifnr, promised_date, qty, utilization=0.0, rng=None):
        return {"delay_days": 0, "received_qty": qty, "p_on_time": 1.0}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the Monte Carlo must not call an LLM")
    monkeypatch.setattr(llm, "complete", fail)


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


@pytest.fixture(scope="module")
def built(adapter):
    rpt = RecordedRpt()
    cases, meta = mc.build_cases(adapter, rpt)
    return {c.scenario: c for c in cases}, meta, rpt


def labels(case):
    return {name: choice.label for name, choice in case.choices.items()}


def test_strategies_match_the_real_recommendation(built):
    cases, meta, _ = built
    assert meta["DETECTION_MODEL"] == "RPT_ONLY sap-rpt-1.6, LEARNING sap-rpt-1.6 + posterior"
    assert meta["EXCLUDE_FLAGGED_SUPPLIERS"] is True
    assert labels(cases["S1"]) == {mc.GESIT_RPT_ONLY: "100109 10,000 kg",
                                   mc.GESIT_LEARNING: "100109 10,000 kg",
                                   mc.ALWAYS_CHEAPEST: "100105 10,000 kg",
                                   mc.ALWAYS_FASTEST: "100109 10,000 kg",
                                   mc.KEEP_CURRENT_PO: "keep PO 4500000431"}
    assert labels(cases["S2"]) == {mc.GESIT_RPT_ONLY: "keep PO 4500000432",
                                   mc.GESIT_LEARNING: "keep PO 4500000432",
                                   mc.ALWAYS_CHEAPEST: "100105 8,000 kg",
                                   mc.ALWAYS_FASTEST: "100102 8,000 kg",
                                   mc.KEEP_CURRENT_PO: "keep PO 4500000432"}
    assert labels(cases["S3"]) == {mc.GESIT_RPT_ONLY: "100108 7,200 kg",
                                   # MOQ (proposal item 12): 100102's MINBM 5,000 kg raises the
                                   # 100109 2,500 kg + 100102 4,700 kg split above 100102 alone.
                                   mc.GESIT_LEARNING: "100102 7,200 kg",
                                   mc.ALWAYS_CHEAPEST: "100105 7,200 kg",
                                   mc.ALWAYS_FASTEST: "100102 7,200 kg",
                                   mc.KEEP_CURRENT_PO: "keep PO 4500000433"}
    # The keep rows use the arrival from Detection: delay notice, PO date, remainder date.
    assert cases["S1"].choices[mc.KEEP_CURRENT_PO].parts == (mc.Part("100101", 10000.0, "2026-10-11"),)
    assert cases["S2"].choices[mc.KEEP_CURRENT_PO].parts == (mc.Part("100103", 8000.0, "2026-10-03"),)
    assert cases["S3"].choices[mc.KEEP_CURRENT_PO].parts == (mc.Part("100104", 7200.0, "2026-10-11"),)
    assert cases["S2"].choices[mc.KEEP_CURRENT_PO].purchase_cost == 149_600_000   # revised price
    assert {c.downtime_cost_per_day for c in cases.values()} == {150_000_000}


def test_predictor_is_called_once_per_case_and_never_in_the_loop(built):
    cases, _, rpt = built
    # Per mode (RPT_ONLY, LEARNING): Detection once, then one ranked table per case.
    assert rpt.calls == len(mc.MODES) * (1 + len(cases))
    mc.simulate(list(cases.values()), mc.load_world(), runs=50, seed=42)
    assert rpt.calls == len(mc.MODES) * (1 + len(cases))


def test_same_seed_same_results_other_seed_differs(adapter):
    a = mc.run("all", 200, 42, adapter, RecordedRpt())
    b = mc.run("all", 200, 42, adapter, RecordedRpt())
    c = mc.run("all", 200, 7, adapter, RecordedRpt())
    assert a["RESULTS"] == b["RESULTS"]
    assert a["RESULTS"] != c["RESULTS"]
    assert set(a["RESULTS"]) == {"S1", "S2", "S3", "TOTAL"}


def test_on_time_world_costs_only_the_purchase():
    # Every part is promised before the stock-out date and nothing is late.
    part = mc.Part("100109", 5000.0, "2026-10-03")
    choices = {name: mc.Choice(name, "x", (part, mc.Part("100102", 2000.0, "2026-10-05")),
                               123_000_000, 0) for name in mc.STRATEGIES}
    case = mc.Case("S1", "4500000431", 7000.0, "2026-10-06", 150_000_000, choices, "stub")
    out = mc.simulate([case], OnTimeWorld(), runs=20, seed=1)["S1"]
    for name in mc.STRATEGIES:
        assert not out[name]["OUT_DAYS"].any()
        assert (out[name]["COST"] == 123_000_000).all()


def test_on_time_world_on_the_real_cases(built):
    cases, _, _ = built
    out = mc.simulate(list(cases.values()), OnTimeWorld(), runs=5, seed=1)
    assert (out["S1"][mc.GESIT_RPT_ONLY]["COST"] == cases["S1"].choices[mc.GESIT_RPT_ONLY].purchase_cost).all()
    # Keeping PO 4500000431 is 5 days late even when nothing slips further (2026-10-11).
    assert (out["S1"][mc.KEEP_CURRENT_PO]["OUT_DAYS"] == 5).all()


def test_common_random_numbers(built):
    cases, _, _ = built
    out = mc.simulate(list(cases.values()), mc.load_world(), runs=300, seed=42)
    s1 = out["S1"]   # GESIT and ALWAYS_FASTEST both order 100109 in S1
    assert np.array_equal(s1[mc.GESIT_RPT_ONLY]["COST"], s1[mc.ALWAYS_FASTEST]["COST"])
    assert np.array_equal(out["S2"][mc.GESIT_RPT_ONLY]["COST"], out["S2"][mc.KEEP_CURRENT_PO]["COST"])
    assert s1[mc.GESIT_RPT_ONLY]["OUT_DAYS"].any()   # 100109 is late in some runs, the same for both


def test_split_stockout_is_quantity_weighted():
    parts = (mc.Part("100109", 2500.0, "2026-10-03"), mc.Part("100108", 4700.0, "2026-10-04"))
    # 100109 arrives 2026-10-08 (2 days after the stock-out date), 100108 on 2026-10-04.
    delays = {("100109", "2026-10-03"): 5, ("100108", "2026-10-04"): 0}
    days = mc.realised_stockout_days(parts, delays, "2026-10-06")
    assert days == pytest.approx(2500 * 2 / 7200)
    delays[("100108", "2026-10-04")] = -1   # a day early never gives negative days
    assert mc.realised_stockout_days(parts, delays, "2026-10-06") == pytest.approx(2500 * 2 / 7200)


def test_case_without_a_fastest_row_stops():
    table = {"CANDIDATES": [{"OPTION": "NEW_PO", "LIFNR": "A", "MENGE": 1.0, "ARRIVAL": "2026-10-03",
                             "SPLIT_WITH": {"LIFNR": "B", "MENGE": 1.0, "ARRIVAL": "2026-10-04"},
                             "PURCHASE_COST": 1, "EXPECTED_COST": 1, "IS_CHEAPEST": True,
                             "IS_FASTEST": False}],
             "KEEP": {"OPTION": "KEEP_CURRENT_PO", "KEEP_EBELN": "1", "LIFNR": "K", "MENGE": 2.0,
                      "ARRIVAL": "2026-10-05", "PURCHASE_COST": 2, "EXPECTED_COST": 2}}
    with pytest.raises(ValueError, match="ALWAYS_FASTEST"):
        mc.choices_for(table)


def test_report_text(adapter):
    text = mc.format_report(mc.run("S1", 50, 42, adapter, RecordedRpt()))
    assert "simulated data" in text and "Seed 42, 50 runs" in text
    assert "quantity-weighted average" in text and "Rp150,000,000 per day" in text
    assert "No SAP-RPT or LLM call inside the loop" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text   # em and en dash


def test_rpt_only_numbers_do_not_depend_on_the_learning_strategy(built):
    # GESIT_RPT_ONLY is the GESIT of before the blend: adding GESIT_LEARNING to the run must
    # not change its draws (common random numbers keyed per supplier and date).
    cases, _, _ = built
    old = [mc.Case(c.scenario, c.ebeln, c.qty, c.stockout, c.downtime_cost_per_day,
                   {n: ch for n, ch in c.choices.items() if n != mc.GESIT_LEARNING},
                   c.prediction_model) for c in cases.values()]
    world = mc.load_world()
    with_learning = mc.simulate(list(cases.values()), world, runs=300, seed=42)
    without = mc.simulate(old, world, runs=300, seed=42)
    for scenario in without:
        for name in without[scenario]:
            assert np.array_equal(with_learning[scenario][name]["COST"], without[scenario][name]["COST"])


def test_calibration_table_and_drift_history(adapter):
    report = mc.run("all", 20, 42, adapter, RecordedRpt())
    rows = {(r["SCENARIO"], r["LIFNR"]): r for r in report["CALIBRATION"]}
    s3 = rows[("S3", "100108")]
    assert (s3["P_RPT"], s3["P_TRUE"]) == (0.21, pytest.approx(0.45, abs=0.005))
    assert s3["P_USED"] == round(0.5 * s3["P_RPT"] + 0.5 * s3["P_POST"], 4)
    assert rows[("S1", "100101")]["KEEP"] is True
    # Visible goods receipts of 100108, not the world's truth (D-1).
    assert report["DRIFT_HISTORY"] == {"LIFNR": "100108", "SPLIT_DATE": "2026-03-01",
                                       "BEFORE": {"LATE": 4, "N": 36}, "AFTER": {"LATE": 5, "N": 15}}
    text = mc.format_report(report)
    assert "before 2026-03-01 4 late of 36 (11%), from 2026-03-01 5 late of 15 (33%)" in text
    assert "built from the RPT_ONLY ranked table" in text
    assert "GESIT_LEARNING vs" in text and "  GESIT_RPT_ONLY" in text


def test_modes_leave_the_switch_as_it_was(adapter):
    import os
    before = os.environ.get("GESIT_RELIABILITY_BLEND")
    mc.build_cases(adapter, RecordedRpt())
    assert os.environ.get("GESIT_RELIABILITY_BLEND") == before


def test_only_eval_uses_the_world_and_the_truth():
    # D-1: agent code, tools, scoring, adapters, integrations, policy never read _truth,
    # never import data_gen/world.py and never import eval.
    imports = re.compile(r"^\s*(from|import)\s+(world|eval)\b|data_gen\.world")
    guard = re.compile(r"""["']_truth["']\s+in\b|\bnever\b""")   # the adapter's D-1 refusal
    hits = []
    for folder in ("agents", "tools", "scoring", "adapters", "integrations", "policy"):
        for path in (ROOT / folder).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if imports.search(line) or ("_truth" in line and not guard.search(line)):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert hits == []


def test_moq_extra_kg_is_not_stock_out_weight():
    """The kg a MOQ round-up adds (MOQ_EXTRA_KG) are purchase cost only: each part is weighted
    by the kg it covers of the need (FR-EVA-4, proposal item 12)."""
    row = {"LIFNR": "100109", "MENGE": 2_500.0, "MOQ_EXTRA_KG": 0.0, "ARRIVAL": "2026-10-03",
           "SPLIT_WITH": {"LIFNR": "100102", "MENGE": 5_000.0, "MOQ_EXTRA_KG": 300.0,
                          "ARRIVAL": "2026-10-03"}}
    parts = mc.parts_of(row)
    assert [p.menge for p in parts] == [2_500.0, 4_700.0]   # sums to the 7,200 kg needed
    delays = {("100109", "2026-10-03"): 0, ("100102", "2026-10-03"): 6}   # 100102 3 days late
    assert mc.realised_stockout_days(parts, delays, "2026-10-06") == 3 * 4_700 / 7_200
