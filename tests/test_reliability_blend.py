"""Reliability blend of the delay risk (FR-DET-4, FR-EVA-2; team decision, proposed rule):
P_USED = 0.5 x SAP-RPT + 0.5 x Beta posterior from receipts before today. No network."""
import pytest

from adapters import CsvAdapter
from agents.detection import DetectionAgent
from agents.run import cases_for, format_case, format_report, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent
from integrations.sap.rpt_client import RptClient, RptUnavailable
from scoring import reliability
from tools import predict
from tools.predict import predict_delay_risk

MATNR = "RM-GULA-001"
TODAY = "2026-09-30"


class StubRpt(RptClient):
    """SAP-RPT stand-in: the same P(late) for every row, or unavailable."""

    def __init__(self, p=0.2):
        self.p = p
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        if self.p is None:
            raise RptUnavailable("SAP-RPT credentials are not configured")
        return {r["EBELN"]: self.p for r in query_rows}


class ExtraReceipts(CsvAdapter):
    """The real adapter plus extra goods receipts, to test the date cut-off."""

    def __init__(self, extra):
        super().__init__()
        self.extra = extra

    def receipts(self, lifnr=None):
        rows = super().receipts(lifnr) + [r for r in self.extra if lifnr in (None, r["LIFNR"])]
        return sorted(rows, key=lambda r: (r["BUDAT"], r["EBELN"]))


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def row(lifnr, ebeln="CAND-X"):
    return {"EBELN": ebeln, "EBELP": 10, "LIFNR": lifnr, "MATNR": MATNR}


def blend_on(monkeypatch):
    monkeypatch.setenv("GESIT_RELIABILITY_BLEND", "1")


# ------------------------------------------------------------------ arithmetic and rule
def test_blend_arithmetic_and_rule_text():
    assert reliability.BLEND_WEIGHT_RPT == 0.5
    assert reliability.blend(0.2, 0.4) == pytest.approx(0.3)
    assert reliability.blend(None, 0.4) == 0.4            # SAP-RPT unavailable: posterior alone
    assert reliability.BLEND_RULE_TEXT == (
        "P(late) = 0.5 x SAP-RPT + 0.5 x reliability posterior (Beta, forgetting 0.97 per "
        "receipt) (proposed rule)")
    # The PRD parameters are unchanged: prior Beta(2, 1), forgetting 0.97 per receipt.
    assert (reliability.PRIOR_ALPHA, reliability.PRIOR_BETA, reliability.FORGETTING_FACTOR) == (
        2.0, 1.0, 0.97)


def test_switch(monkeypatch):
    assert predict.blend_enabled({}) is True                                  # default on
    for off in ("0", "false", "off", "no"):
        assert predict.blend_enabled({"GESIT_RELIABILITY_BLEND": off}) is False
    assert predict.blend_enabled({"GESIT_RELIABILITY_BLEND": "1"}) is True


# ------------------------------------------------------------------ predict_delay_risk
def test_blend_of_rpt_and_posterior(adapter):
    out = predict_delay_risk([row("100108")], adapter, StubRpt(0.2), today=TODAY, use_blend=True)
    p = out["predictions"][0]
    post = reliability.posterior_before(adapter.receipts("100108"), TODAY).p_late
    assert p["P_RPT"] == 0.2 and p["P_POST"] == round(post, 4)
    assert p["P_USED"] == round(0.5 * 0.2 + 0.5 * post, 4) == p["P_LATE"]
    assert p["SOURCE"] == predict.SOURCE_BLEND and out["blend"] is True
    assert out["model_id"] == "sap-rpt-1.6" and out["fallback"] is False


def test_fallback_uses_the_posterior_alone(adapter):
    out = predict_delay_risk([row("100108")], adapter, StubRpt(None), today=TODAY, use_blend=True)
    p = out["predictions"][0]
    assert out["fallback"] is True and out["model_id"] == predict.FALLBACK_MODEL_ID
    assert p["P_RPT"] is None and p["P_USED"] == p["P_POST"] == p["P_LATE"]
    assert p["SOURCE"] == predict.SOURCE_POSTERIOR


def test_supplier_without_receipts_gets_the_prior_mean(adapter):
    assert adapter.receipts("100111") == []   # off-panel, quote only
    p = predict_delay_risk([row("100111")], adapter, StubRpt(0.3), today=TODAY,
                           use_blend=True)["predictions"][0]
    assert p["P_POST"] == round(1 / 3, 4)                 # Beta(2, 1): P(late) = 1/3
    assert p["P_USED"] == round(0.5 * 0.3 + 0.5 / 3, 4)


def test_receipt_on_or_after_today_is_not_used(adapter):
    base = predict_delay_risk([row("100108")], adapter, StubRpt(0.2), today=TODAY,
                              use_blend=True)["predictions"][0]["P_POST"]
    late = {"EBELN": "9", "EBELP": 10, "LIFNR": "100108", "EINDT": "2026-09-25",
            "MENGE": 1.0, "DELAY_DAYS": 7, "LATE": 1}
    for budat in (TODAY, "2026-10-02"):
        extra = ExtraReceipts([{**late, "BUDAT": budat}])
        after = predict_delay_risk([row("100108")], extra, StubRpt(0.2), today=TODAY,
                                   use_blend=True)["predictions"][0]["P_POST"]
        assert after == base
    # The same late receipt one day before today does count.
    extra = ExtraReceipts([{**late, "BUDAT": "2026-09-29"}])
    before = predict_delay_risk([row("100108")], extra, StubRpt(0.2), today=TODAY,
                                use_blend=True)["predictions"][0]["P_POST"]
    assert before > base


def test_switch_off_is_the_old_tool(adapter):
    out = predict_delay_risk([row("100108")], adapter, StubRpt(0.2), use_blend=False)
    assert out["predictions"] == [{"EBELN": "CAND-X", "EBELP": 10, "LIFNR": "100108",
                                   "P_LATE": 0.2}]
    fallback = predict_delay_risk([row("100108")], adapter, StubRpt(None), use_blend=False)
    every = reliability.posterior(r["LATE"] for r in adapter.receipts("100108")).p_late
    assert fallback["predictions"][0]["P_LATE"] == round(every, 4)


# ------------------------------------------------------------------ downstream
def run_all(adapter, p=0.2):
    rpt = StubRpt(p)
    det = DetectionAgent(adapter=adapter, rpt_client=rpt, reasoner=lambda f: "ok").detect(
        MATNR, notices_on_file("all", adapter.scenarios()))
    ev = SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt).evaluate(det, det["FLAGGED_POS"])
    return det, ev


def test_detection_flags_and_order_do_not_change(adapter, monkeypatch):
    off_det, _ = run_all(adapter)
    blend_on(monkeypatch)
    on_det, _ = run_all(adapter)
    assert on_det["FLAGGED_EBELN"] == off_det["FLAGGED_EBELN"] == [
        "4500000431", "4500000433", "4500000432"]
    for a, b in zip(off_det["FLAGGED_POS"], on_det["FLAGGED_POS"]):
        assert [r["CODE"] for r in a["REASONS"]] == [r["CODE"] for r in b["REASONS"]]
    po = next(p for p in on_det["OPEN_POS"] if p["EBELN"] == "4500000431")
    assert po["P_LATE"] == po["P_LATE_SOURCES"]["P_USED"] != 0.2
    text = format_report(on_det)
    assert reliability.BLEND_RULE_TEXT in text and reliability.BLEND_RULE_TEXT not in format_report(off_det)


def test_p_used_reaches_the_supplier_evaluation_tables(adapter, monkeypatch):
    blend_on(monkeypatch)
    det, ev = run_all(adapter)
    for case in ev["CASES"]:
        table = case["SCORING"]
        for r in table["CANDIDATES"]:
            if r["OPTION"] == "NEW_PO":
                assert r["P_LATE"] == round(table["P_LATE_SOURCES"][r["LIFNR"]]["P_USED"], 4)
            else:   # the keep row uses Detection's P_USED for the PO
                assert r["P_LATE"] == case["CURRENT_PO"]["P_LATE_SOURCES"]["P_USED"]
        lines = format_case(case)
        assert reliability.BLEND_RULE_TEXT in lines
        sources = next(line for line in lines if line.startswith("P(late) sources: "))
        assert f"keep PO {case['EBELN']}" in sources and "posterior" in sources


def test_sources_line_names_the_three_numbers(adapter, monkeypatch):
    blend_on(monkeypatch)
    _, ev = run_all(adapter)
    s1 = next(c for c in ev["CASES"] if c["EBELN"] == "4500000431")
    src = s1["SCORING"]["P_LATE_SOURCES"]["100109"]
    line = next(l for l in format_case(s1) if l.startswith("P(late) sources: "))
    assert line.startswith(f"P(late) sources: 100109 SAP-RPT 0.20, posterior "
                           f"{src['P_POST']:.2f}, used {src['P_USED']:.2f}")
