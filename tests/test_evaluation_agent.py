"""Supplier Evaluation agent on the simulated scenarios (FR-SEL-1 to FR-SEL-4)."""
import json

import pytest

from adapters import CsvAdapter
from agents import llm
from agents.detection import DetectionAgent
from agents import evaluation, grounding
from agents.evaluation import (EvaluationAgent, facts_for_llm, replacement_qty,
                               required_mentions)
from agents.run import format_evaluation, main, notices_for
from integrations.sap.rpt_client import RptClient, RptUnavailable
from tools import get_market_price, get_quotes, score_suppliers

MATNR = "RM-GULA-001"
NAMES = ("PT Sumber Rasa Baru", "PT Kristal Prima Jaya", "PT Sinar Manis Lampung")


class StubRpt(RptClient):
    """SAP-RPT stand-in: P(late) per supplier (LIFNR), or unavailable."""

    def __init__(self, p_late=None):
        self._p_late = p_late
        self.config = None
        self.calls = 0

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        if self._p_late is None:
            raise RptUnavailable("SAP-RPT credentials are not configured")
        self.calls += 1
        return {r["EBELN"]: self._p_late.get(r["LIFNR"], 0.2) for r in query_rows}


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def detection(adapter, scenario):
    agent = DetectionAgent(adapter=adapter, rpt_client=StubRpt({}), reasoner=lambda f: "ok")
    return agent.detect(MATNR, notices_for(scenario, adapter.scenarios()))


def evaluate(adapter, scenario, p_late=None, explainer=lambda facts: "ok"):
    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt(p_late), explainer=explainer)
    return agent.evaluate(detection(adapter, scenario))


def test_tools_quotes_and_market_price(adapter):
    quotes = get_quotes(MATNR, adapter=adapter)
    assert len(quotes) == 11 and {q["LIFNR"] for q in quotes} >= {"100101", "100111"}
    assert get_market_price(MATNR, "2026-09-30", adapter=adapter)["PRICE_IDR_KG"] == 17550.0
    assert get_market_price(MATNR, "2026-12-31", adapter=adapter)["DATE"] == "2026-09-30"
    with pytest.raises(KeyError):
        get_market_price(MATNR, "2020-01-01", adapter=adapter)


def test_s1_candidates_exclusions_and_ranking(adapter):
    report = evaluate(adapter, "S1", {})
    assert len(report["CASES"]) == 1
    case = report["CASES"][0]
    table = case["SCORING"]
    assert (case["EBELN"], case["QTY"], case["NEED_BY"]) == ("4500000431", 10_000.0, "2026-10-06")
    # FR-SEL-1: panel suppliers with a valid quote; the disrupted and empty ones are listed.
    assert {e["LIFNR"]: e["REASON"] for e in table["EXCLUDED"]} == {
        "100101": "DISRUPTED_SUPPLIER", "100104": "NO_SPARE_CAPACITY",
        "100110": "NO_SPARE_CAPACITY", "100111": "OFF_PANEL"}
    rows = table["CANDIDATES"]
    assert {r["LIFNR"] for r in rows} == {"100102", "100103", "100105", "100106", "100107",
                                          "100108", "100109"}
    # FR-SEL-2 and FR-SEL-3: sorted by expected cost, every component present.
    assert [r["EXPECTED_COST"] for r in rows] == sorted(r["EXPECTED_COST"] for r in rows)
    assert all(r["EXPECTED_COST"] == r["PURCHASE_COST"] + r["RISK_COST"] for r in rows)
    assert table["RECOMMENDED"] == rows[0]["LIFNR"] and rows[0]["SCORE"] == 100.0
    assert table["CHEAPEST"] == min(rows, key=lambda r: r["NETPR"])["LIFNR"]
    assert min(r["LEAD_DAYS"] for r in rows) == next(
        r["LEAD_DAYS"] for r in rows if r["LIFNR"] == table["FASTEST"])
    assert table["PREDICTION"]["FALLBACK"] is False and table["PREDICTION"]["ROWS"] == 7


def test_rpt_p_late_drives_the_ranking(adapter):
    # 100105 is the cheapest candidate but arrives only 1 day before the stock-out date.
    sure = evaluate(adapter, "S1", {"100105": 0.0})["CASES"][0]["SCORING"]
    risky = evaluate(adapter, "S1", {"100105": 0.95})["CASES"][0]["SCORING"]
    assert sure["RECOMMENDED"] == "100105" and sure["CHEAPEST"] == "100105"
    row = next(r for r in risky["CANDIDATES"] if r["LIFNR"] == "100105")
    assert risky["RECOMMENDED"] != "100105" and row["RISK_COST"] > 0


def test_fallback_is_reported(adapter):
    table = evaluate(adapter, "S1", None)["CASES"][0]["SCORING"]
    assert table["PREDICTION"]["FALLBACK"] is True
    assert table["PREDICTION"]["MODEL_ID"] == "beta-posterior-fallback"
    assert all(0 < r["P_LATE"] < 1 for r in table["CANDIDATES"])


def test_replacement_quantity_per_scenario(adapter):
    flagged = {s: detection(adapter, s)["FLAGGED_POS"][0] for s in ("S1", "S2", "S3")}
    assert replacement_qty(flagged["S1"]) == 10_000.0
    assert replacement_qty(flagged["S2"]) == 8_000.0
    assert replacement_qty(flagged["S3"]) == 7_200.0   # only the late remainder


def test_all_reserves_capacity_across_cases(adapter):
    report = evaluate(adapter, "all", {})
    assert [c["EBELN"] for c in report["CASES"]] == ["4500000431", "4500000433", "4500000432"]
    booked: dict[str, float] = {}
    for case in report["CASES"]:
        best = case["SCORING"]["CANDIDATES"][0]
        for part in (best, best["SPLIT_WITH"]):
            if part:
                booked[part["LIFNR"]] = booked.get(part["LIFNR"], 0.0) + part["MENGE"]
        excluded = {e["LIFNR"] for e in case["SCORING"]["EXCLUDED"]}
        assert {"100101", "100103", "100104"} <= excluded
    spare = {q["LIFNR"]: q["AVAILABLE_KG_NEXT_30D"] for q in adapter.quotes(MATNR)}
    assert all(kg <= spare[lifnr] for lifnr, kg in booked.items())
    assert report["STEPS"][1]["INPUT"]["reserved_kg"] != {}


def test_scan_without_flags_has_no_cases(adapter):
    report = evaluate(adapter, "scan", {})
    assert report["CASES"] == []
    assert "nothing to replace" in format_evaluation(report)


def test_evaluate_is_deterministic(adapter):
    assert json.dumps(evaluate(adapter, "all", {})) == json.dumps(evaluate(adapter, "all", {}))


def test_score_suppliers_tool_directly(adapter):
    table = score_suppliers(MATNR, 5_000, "2026-10-06", adapter=adapter, client=StubRpt({}))
    assert "100101" in {r["LIFNR"] for r in table["CANDIDATES"]}   # nothing excluded by request
    assert table["MARKET_PRICE"] == {"DATE": "2026-09-30", "PRICE_IDR_KG": 17550}


def test_llm_facts_are_masked_and_precomputed(adapter):
    report = evaluate(adapter, "S1", {})
    facts = facts_for_llm(report["CASES"][0], report)
    text = json.dumps(facts)
    assert not any(name in text for name in NAMES)
    assert "BUYER" not in text and "ERNAM" not in text
    # Ready-made phrases: a space around every number, no negative amount (FR-SEL-4).
    assert facts["RECOMMENDED"]["SUPPLIER"] == "supplier 100109"
    assert facts["RECOMMENDED"]["EXPECTED_COST"].startswith("expected cost Rp ")
    assert not grounding.NEGATIVE_AMOUNT.search(text)
    cheapest = facts["CHEAPEST"]["VS_RECOMMENDED"]
    assert cheapest["PURCHASE"] == "purchase saving Rp 1,000,000"   # 17,450 vs 17,550 x 10,000 kg
    assert cheapest["EXPECTED_COST"].endswith("more)")


@pytest.mark.parametrize("scenario", ["S1", "S2", "S3"])
def test_reason_vs_cheapest_is_given(adapter, scenario):
    # FR-SEL-4: why #1 beats the cheapest, not only by how much. The cheapest saves on
    # purchase, so the extra risk cost is what makes it lose, with its stock-out days.
    report = evaluate(adapter, scenario, {"100101": 0.6, "100103": 0.5})
    case = report["CASES"][0]
    facts = facts_for_llm(case, report)
    if facts["RECOMMENDED_IS_CHEAPEST"]:
        pytest.skip("recommended supplier is the cheapest")
    rows = {r["LIFNR"]: r for r in case["SCORING"]["CANDIDATES"]}
    cheap, best = rows[case["SCORING"]["CHEAPEST"]], rows[case["SCORING"]["RECOMMENDED"]]
    why = facts["CHEAPEST"]["VS_RECOMMENDED"]["WHY"]
    assert why.startswith(f"mainly from extra risk cost Rp {cheap['RISK_COST'] - best['RISK_COST']:,} (")
    assert (f"{cheap['EXPECTED_STOCKOUT_DAYS']} expected days without stock against "
            f"{best['EXPECTED_STOCKOUT_DAYS']} for supplier {best['LIFNR']}") in why
    labels = [label for label, _ in required_mentions(facts)]
    assert any(label.startswith("reason vs cheapest: mainly from extra risk cost") for label in labels)
    assert "days without stock" in labels


def test_explanation_without_reason_fails_the_check(adapter):
    report = evaluate(adapter, "S2", {})
    facts = facts_for_llm(report["CASES"][0], report)
    versus = facts["CHEAPEST"]["VS_RECOMMENDED"]
    base = (f"Order from {facts['RECOMMENDED']['SUPPLIER']}. The cheapest has "
            f"{versus['EXPECTED_COST']}. It rests on only "
            f"{facts['RECOMMENDED']['PAST_DELIVERIES']}. All figures are simulated.")
    check = grounding.check_explanation(base, facts, 120, must_mention=required_mentions(facts))
    assert check["PASSED"] is False
    assert any(m.startswith("reason vs cheapest") for m in check["MISSING_MENTIONS"])
    with_reason = base.replace(f"{versus['EXPECTED_COST']}.", f"{versus['EXPECTED_COST']}, {versus['WHY']}.")
    check = grounding.check_explanation(with_reason, facts, 120, must_mention=required_mentions(facts))
    assert check["PASSED"] is True, check

def test_run_adds_explanation_and_check(adapter):
    seen = {}

    def explainer(facts):
        seen.update(facts)
        best = facts["RECOMMENDED"]
        versus = facts["CHEAPEST"]["VS_RECOMMENDED"]
        return (f" Order from {best['SUPPLIER']}, {best['EXPECTED_COST']}. The cheapest has "
                f"{versus['EXPECTED_COST']}, {versus['WHY']}. It rests on only "
                f"{best['PAST_DELIVERIES']}. All figures are simulated. ", "gemini-x")

    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = agent.run(detection(adapter, "S1"))
    case = report["CASES"][0]
    assert case["EXPLANATION"].startswith("Order from supplier 100109")
    assert case["EXPLANATION_MODEL"] == "gemini-x" and case["EXPLANATION_ERROR"] is None
    assert case["EXPLANATION_CHECK"]["PASSED"] is True, case["EXPLANATION_CHECK"]
    assert seen["REPLACES"].startswith("order 4500000431")
    settings = report["EXPLANATION_SETTINGS"]
    assert settings["MODEL"] == llm.model_id() and settings["TEMPERATURE"] <= 0.2
    assert settings["MASKING"].startswith("on")

def test_explanation_is_tidied_before_the_check(adapter):
    def explainer(facts):
        best = facts["RECOMMENDED"]
        versus = facts["CHEAPEST"]["VS_RECOMMENDED"]
        why = versus["WHY"].replace("against ", "againts")      # the typo seen in a real run
        return (f"Order from {best['SUPPLIER']}, {best['EXPECTED_COST']}.The cheapest has "
                f"{versus['EXPECTED_COST']}, {why}. It rests on only "
                f"{best['PAST_DELIVERIES']}. All figures are simulated.")

    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = agent.run(detection(adapter, "S1"))
    case = report["CASES"][0]
    assert "againts" not in case["EXPLANATION"] and " against " in case["EXPLANATION"]
    assert ". The cheapest" in case["EXPLANATION"]
    assert "spelling againts -> against" in case["EXPLANATION_FIXES"]
    assert case["EXPLANATION_CHECK"]["PASSED"] is True, case["EXPLANATION_CHECK"]
    assert "Text tidied in code: " in format_evaluation(report)


def test_invented_number_is_flagged(adapter):
    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}),
                            explainer=lambda facts: "It saves about Rp12.3 million.")
    check = agent.run(detection(adapter, "S1"))["CASES"][0]["EXPLANATION_CHECK"]
    assert check["UNGROUNDED_NUMBERS"] == ["12.3"] and check["PASSED"] is False


def test_run_survives_missing_llm(adapter):
    def explainer(facts):
        raise llm.LlmUnavailable("GEMINI_API_KEY is not set")

    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = agent.run(detection(adapter, "S1"))
    case = report["CASES"][0]
    assert case["EXPLANATION"] is None and "GEMINI_API_KEY" in case["EXPLANATION_ERROR"]
    assert case["SCORING"]["CANDIDATES"]                  # the table survives
    assert "not available" in format_evaluation(report)


def test_cli_prints_both_agents(monkeypatch, capsys):
    # Never call the network and never load the real .env into the test process.
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("SAP_RPT_MODE", "none")
    main(["S1", "--no-llm"])
    out = capsys.readouterr().out
    assert "GESIT Disruption Detection" in out and "GESIT Supplier Evaluation" in out
    assert "Replacement for PO 4500000431" in out
    main(["S1", "--no-llm", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"DETECTION", "EVALUATION", "ACTION", "POS", "LLM_NOTES"}
    main(["S1", "--no-llm", "--detect-only"])
    assert "Supplier Evaluation" not in capsys.readouterr().out


def test_short_track_record_is_flagged_for_the_model(adapter):
    report = evaluate(adapter, "S1", {})
    facts = facts_for_llm(report["CASES"][0], report)
    assert facts["RECOMMENDED"]["PAST_DELIVERIES"] == "4 past deliveries"
    assert "only 4 past deliveries" in facts["TRACK_RECORD_WARNING"]
    labels = [label for label, _ in required_mentions(facts)]
    assert labels[:2] == ["simulated", "4 past deliveries"]   # then the reason checks

def test_s1_recommended_covers_the_full_quantity(adapter):
    # FR-SEL-2: 100109 has 12,500 kg spare, so in S1 it covers all 10,000 kg without a split.
    table = evaluate(adapter, "S1", {})["CASES"][0]["SCORING"]
    best = table["CANDIDATES"][0]
    assert best["LIFNR"] == "100109" and best["AVAILABLE_KG"] == 12_500.0
    assert best["COVERS_FULL_QTY"] is True and best["CAPACITY_NOTE"] is None
    assert table["RESERVED_KG"] == {}


def test_no_row_is_ranked_for_more_than_its_spare_capacity(adapter):
    # In `all` the first case takes 10,000 of 100109's 12,500 kg, so 2,500 kg are left for
    # the next case: that row must say "cannot cover full quantity" and add up to 7,200 kg.
    report = evaluate(adapter, "all", {})
    s3 = report["CASES"][1]
    assert s3["EBELN"] == "4500000433" and s3["SCORING"]["RESERVED_KG"] == {"100109": 10_000.0}
    for case in report["CASES"]:
        for row in case["SCORING"]["CANDIDATES"]:
            assert row["MENGE"] <= row["AVAILABLE_KG"]
            if row["COVERS_FULL_QTY"]:
                assert row["MENGE"] == case["QTY"] and row["CAPACITY_NOTE"] is None
            else:
                assert row["CAPACITY_NOTE"] == "cannot cover full quantity"
                assert row["MENGE"] + row["SPLIT_WITH"]["MENGE"] == case["QTY"]
    row = next(r for r in s3["SCORING"]["CANDIDATES"] if r["LIFNR"] == "100109")
    assert (row["AVAILABLE_KG"], row["MENGE"]) == (2_500.0, 2_500.0)


def test_exclude_flagged_suppliers_flag(adapter, monkeypatch):
    det = detection(adapter, "S2")
    on = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), exclude_flagged=True)
    off = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), exclude_flagged=False)
    excluded_on = {e["LIFNR"]: e["REASON"] for e in on.evaluate(det)["CASES"][0]["SCORING"]["EXCLUDED"]}
    off_report = off.evaluate(det)
    assert excluded_on["100103"] == "DISRUPTED_SUPPLIER"
    assert off_report["EXCLUDE_FLAGGED_SUPPLIERS"] is False
    assert "100103" in {r["LIFNR"] for r in off_report["CASES"][0]["SCORING"]["CANDIDATES"]}
    assert evaluation.exclude_flagged_suppliers({}) is True                       # default
    assert evaluation.exclude_flagged_suppliers({"GESIT_EXCLUDE_FLAGGED_SUPPLIERS": "false"}) is False


def test_masking_check_blocks_the_call(adapter, monkeypatch):
    called = []
    monkeypatch.setattr(evaluation, "facts_for_llm",
                        lambda case, report: {"NOTE": "PT Sumber Rasa Baru is best"})
    agent = EvaluationAgent(adapter=adapter, rpt_client=StubRpt({}),
                            explainer=lambda facts: called.append(facts) or "x")
    case = agent.run(detection(adapter, "S1"))["CASES"][0]
    assert called == [] and case["EXPLANATION"] is None
    assert "masking check failed" in case["EXPLANATION_ERROR"]


def test_near_tie_must_be_mentioned(adapter):
    facts = {"CHEAPEST": {"VS_RECOMMENDED": {"NEAR_TIE": True}},
             "RECOMMENDED": {"PAST_DELIVERIES": "40 past deliveries"}}
    assert [label for label, _ in required_mentions(facts)] == ["simulated", "near tie"]
