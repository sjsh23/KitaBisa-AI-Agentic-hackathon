"""Supplier Evaluation agent on the simulated scenarios (FR-SEL-1 to FR-SEL-4)."""
import json

import pytest

from adapters import CsvAdapter
from agents import llm
from agents.detection import DetectionAgent
from agents import grounding, supplier_evaluation
from agents.supplier_evaluation import (SupplierEvaluationAgent, facts_for_llm, replacement_qty,
                               required_mentions)
from agents.run import cases_for, format_case, format_evaluation, main, notices_on_file
from integrations.sap.rpt_client import RptClient, RptUnavailable
from scoring import supplier_score
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
    """Detection as the CLI runs it: every notice on file (one world)."""
    agent = DetectionAgent(adapter=adapter, rpt_client=StubRpt({}), reasoner=lambda f: "ok")
    return agent.detect(MATNR, notices_on_file(scenario, adapter.scenarios()))


def evaluate(adapter, scenario, p_late=None, explainer=lambda facts: "ok", **agent_args):
    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt(p_late), explainer=explainer,
                            **agent_args)
    det = detection(adapter, scenario)
    return agent.evaluate(det, det["FLAGGED_POS"], cases_for(scenario, adapter.scenarios()))


def run_s1(agent, adapter):
    """Score and explain the S1 case, with the full flagged list as the CLI passes it."""
    det = detection(adapter, "S1")
    return agent.run(det, det["FLAGGED_POS"], cases_for("S1", adapter.scenarios()))


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
    # Every notice on file counts, so the suppliers of the S2 and S3 POs are disrupted too.
    assert {e["LIFNR"]: e["REASON"] for e in table["EXCLUDED"]} == {
        "100101": "DISRUPTED_SUPPLIER", "100103": "DISRUPTED_SUPPLIER",
        "100104": "DISRUPTED_SUPPLIER", "100110": "NO_SPARE_CAPACITY", "100111": "OFF_PANEL"}
    rows = table["CANDIDATES"]
    new = [r for r in rows if r["OPTION"] == "NEW_PO"]
    assert {r["LIFNR"] for r in new} == {"100102", "100105", "100106", "100107",
                                         "100108", "100109"}
    # Keeping the delayed PO is ranked too: it arrives 5 days after stock runs out, so last.
    assert rows[-1]["OPTION"] == "KEEP_CURRENT_PO" and rows[-1]["LIFNR"] == "100101"
    assert table["KEEP"] == rows[-1] and table["RECOMMENDED_OPTION"] == "NEW_PO"
    # FR-SEL-2 and FR-SEL-3: sorted by expected cost, every component present.
    assert [r["EXPECTED_COST"] for r in rows] == sorted(r["EXPECTED_COST"] for r in rows)
    assert all(r["EXPECTED_COST"] == r["PURCHASE_COST"] + r["RISK_COST"] for r in rows)
    assert table["RECOMMENDED"] == rows[0]["LIFNR"] and rows[0]["SCORE"] == 100.0
    # Cheapest and fastest are the baselines among new orders (the kept PO never counts).
    assert table["CHEAPEST"] == min(new, key=lambda r: r["NETPR"])["LIFNR"]
    assert min(r["LEAD_DAYS"] for r in new) == next(
        r["LEAD_DAYS"] for r in new if r["LIFNR"] == table["FASTEST"])
    assert table["PREDICTION"]["FALLBACK"] is False and table["PREDICTION"]["ROWS"] == 6


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
    flagged = {po["EBELN"]: po for po in detection(adapter, "all")["FLAGGED_POS"]}
    assert replacement_qty(flagged["4500000431"]) == 10_000.0   # S1
    assert replacement_qty(flagged["4500000432"]) == 8_000.0    # S2
    assert replacement_qty(flagged["4500000433"]) == 7_200.0    # S3, only the late remainder


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
    assert facts["RECOMMENDED"]["EXPECTED_COST"].startswith("expected cost Rp")
    # The first sentence is built in code (the model wrote "expected cost is expected cost").
    assert facts["OPENING"] == ("Recommend supplier 100109, also the fastest option, at an expected cost of "
                                + facts["RECOMMENDED"]["EXPECTED_COST"].removeprefix("expected cost ")
                                + ".")
    assert not grounding.NEGATIVE_AMOUNT.search(text)
    cheapest = facts["CHEAPEST"]["VS_RECOMMENDED"]
    assert cheapest["PURCHASE"] == "purchase saving Rp1,000,000"   # 17,450 vs 17,550 x 10,000 kg
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
    assert why.startswith(f"mainly from extra risk cost Rp{cheap['RISK_COST'] - best['RISK_COST']:,} (")
    # In S2 keeping the PO can be #1 (S1 takes 100109's spare capacity first).
    label = (f"the current order {best['KEEP_EBELN']}" if best["OPTION"] == "KEEP_CURRENT_PO"
             else f"supplier {best['LIFNR']}")
    assert (f"{cheap['EXPECTED_STOCKOUT_DAYS']} expected days without stock against "
            f"{best['EXPECTED_STOCKOUT_DAYS']} for {label}") in why
    labels = [label for label, _ in required_mentions(facts)]
    assert any(label.startswith("reason vs cheapest: mainly from extra risk cost") for label in labels)
    assert "days without stock" in labels


def test_explanation_without_reason_fails_the_check(adapter):
    report = evaluate(adapter, "S1", {})   # a new order wins and rests on 4 past deliveries
    facts = facts_for_llm(report["CASES"][0], report)
    versus = facts["CHEAPEST"]["VS_RECOMMENDED"]
    base = (f"Order from {facts['RECOMMENDED']['SUPPLIER']}. The cheapest has "
            f"{versus['EXPECTED_COST']}. It rests on only "
            f"{facts['RECOMMENDED']['PAST_DELIVERIES']}. All figures are simulated.")
    check = grounding.check_explanation(base, facts, 120, must_mention=required_mentions(facts))
    assert check["PASSED"] is False
    assert any(m.startswith("reason vs cheapest") for m in check["MISSING_MENTIONS"])
    with_reason = base.replace(f"{versus['EXPECTED_COST']}.", f"{versus['EXPECTED_COST']}, "
                               f"{versus['WHY']}. Keeping the current order costs more.")
    check = grounding.check_explanation(with_reason, facts, 120, must_mention=required_mentions(facts))
    assert check["PASSED"] is True, check

def test_run_adds_explanation_and_check(adapter):
    seen = {}

    def explainer(facts):
        seen.update(facts)
        best = facts["RECOMMENDED"]
        versus = facts["CHEAPEST"]["VS_RECOMMENDED"]
        return (f" Order from {best['SUPPLIER']}, {best['EXPECTED_COST']}. The cheapest has "
                f"{versus['EXPECTED_COST']}, {versus['WHY']}. Keeping the current order "
                f"costs more. It rests on only {best['PAST_DELIVERIES']}. All figures are "
                "simulated. ", "model-x")

    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = run_s1(agent, adapter)
    case = report["CASES"][0]
    assert case["EXPLANATION"].startswith("Order from supplier 100109")
    assert case["EXPLANATION_MODEL"] == "model-x" and case["EXPLANATION_ERROR"] is None
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
                f"{versus['EXPECTED_COST']}, {why}. Keeping the current order costs more. "
                f"It rests on only {best['PAST_DELIVERIES']}. All figures are simulated.")

    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = run_s1(agent, adapter)
    case = report["CASES"][0]
    assert "againts" not in case["EXPLANATION"] and " against " in case["EXPLANATION"]
    assert ". The cheapest" in case["EXPLANATION"]
    assert "spelling againts -> against" in case["EXPLANATION_FIXES"]
    assert case["EXPLANATION_CHECK"]["PASSED"] is True, case["EXPLANATION_CHECK"]
    assert "Text tidied in code: " in format_evaluation(report)


def test_invented_number_is_flagged(adapter):
    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}),
                            explainer=lambda facts: "It saves about Rp12.3 million.")
    check = run_s1(agent, adapter)["CASES"][0]["EXPLANATION_CHECK"]
    assert check["UNGROUNDED_NUMBERS"] == ["12.3"] and check["PASSED"] is False


def test_run_survives_missing_llm(adapter):
    def explainer(facts):
        raise llm.LlmUnavailable("SAP AI Core credentials (AICORE_*) are not set")

    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    report = run_s1(agent, adapter)
    case = report["CASES"][0]
    assert case["EXPLANATION"] is None and "AICORE_*" in case["EXPLANATION_ERROR"]
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
    assert set(data) == {"DETECTION", "SUPPLIER_EVALUATION", "ACTION", "POS", "LLM_NOTES"}
    main(["S1", "--no-llm", "--detect-only"])
    assert "Supplier Evaluation" not in capsys.readouterr().out


def test_short_track_record_is_flagged_for_the_model(adapter):
    report = evaluate(adapter, "S1", {})
    facts = facts_for_llm(report["CASES"][0], report)
    assert facts["RECOMMENDED"]["PAST_DELIVERIES"] == "4 past deliveries"
    assert "only 4 past deliveries" in facts["TRACK_RECORD_WARNING"]
    labels = [label for label, _ in required_mentions(facts)]
    assert labels[:3] == ["simulated", "keeping the current order",
                          "4 past deliveries"]   # then the reason checks

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
            if row["OPTION"] == "KEEP_CURRENT_PO":
                assert row["AVAILABLE_KG"] is None and row["MENGE"] == case["QTY"]
                continue   # already ordered, no spare capacity involved
            assert row["MENGE"] <= row["AVAILABLE_KG"]
            if row["COVERS_FULL_QTY"]:
                assert row["MENGE"] == case["QTY"] and row["CAPACITY_NOTE"] is None
            else:
                assert row["CAPACITY_NOTE"] == "cannot cover full quantity"
                assert row["MENGE"] + row["SPLIT_WITH"]["MENGE"] == case["QTY"]
    row = next(r for r in s3["SCORING"]["CANDIDATES"] if r["LIFNR"] == "100109")
    assert (row["AVAILABLE_KG"], row["MENGE"]) == (2_500.0, 2_500.0)


def test_exclude_flagged_suppliers_flag(adapter, monkeypatch):
    on_report = evaluate(adapter, "S2", {}, exclude_flagged=True)
    off_report = evaluate(adapter, "S2", {}, exclude_flagged=False)
    excluded_on = {e["LIFNR"]: e["REASON"] for e in on_report["CASES"][0]["SCORING"]["EXCLUDED"]}
    assert excluded_on["100103"] == "DISRUPTED_SUPPLIER"
    assert off_report["EXCLUDE_FLAGGED_SUPPLIERS"] is False
    assert "100103" in {r["LIFNR"] for r in off_report["CASES"][0]["SCORING"]["CANDIDATES"]}
    assert supplier_evaluation.exclude_flagged_suppliers({}) is True                       # default
    assert supplier_evaluation.exclude_flagged_suppliers({"GESIT_EXCLUDE_FLAGGED_SUPPLIERS": "false"}) is False


def test_masking_check_blocks_the_call(adapter, monkeypatch):
    called = []
    monkeypatch.setattr(supplier_evaluation, "facts_for_llm",
                        lambda case, report: {"NOTE": "PT Sumber Rasa Baru is best"})
    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}),
                            explainer=lambda facts: called.append(facts) or "x")
    case = run_s1(agent, adapter)["CASES"][0]
    assert called == [] and case["EXPLANATION"] is None
    assert "masking check failed" in case["EXPLANATION_ERROR"]


def test_near_tie_must_be_mentioned(adapter):
    facts = {"CHEAPEST": {"VS_RECOMMENDED": {"NEAR_TIE": True}},
             "RECOMMENDED": {"PAST_DELIVERIES": "40 past deliveries"}}
    assert [label for label, _ in required_mentions(facts)] == ["simulated", "near tie"]


# ------------------------------------------------- keep the current PO (proposal item 4)
def test_keep_row_uses_the_po_terms(adapter):
    # S2: PO 4500000432 keeps arriving 3 days before stock runs out, at the revised price.
    case = evaluate(adapter, "S2", {})["CASES"][0]
    keep = case["SCORING"]["KEEP"]
    assert (keep["LIFNR"], keep["KEEP_EBELN"], keep["NETPR"]) == ("100103", "4500000432", 18_700)
    assert (keep["ARRIVAL"], keep["MENGE"]) == ("2026-10-03", 8_000.0)
    assert keep["PURCHASE_COST"] == 149_600_000   # 8,000 kg x Rp18,700, simulated
    assert keep["P_LATE"] == case["CURRENT_PO"]["P_LATE"]   # the PO's own prediction
    assert not keep["IS_CHEAPEST"] and not keep["IS_FASTEST"]
    assert case["CURRENT_PO"]["NETPR"] == 18_700 and case["CURRENT_PO"]["PO_NETPR"] == 17_650


def test_keep_wins_in_all_when_the_best_backup_is_taken(adapter):
    # In `all` 100109's capacity goes to S1 first, so for S2 keeping PO 4500000432
    # (Rp149,600,000, simulated) beats every new order: no reservation, keep facts.
    report = evaluate(adapter, "all", {})
    s2 = next(c for c in report["CASES"] if c["EBELN"] == "4500000432")
    table = s2["SCORING"]
    assert table["RECOMMENDED_OPTION"] == "KEEP_CURRENT_PO" and table["RECOMMENDED"] == "100103"
    assert table["CANDIDATES"][0]["EXPECTED_COST"] == 149_600_000
    assert "#1 keep PO 4500000432" in report["STEPS"][2]["RESULT"]
    facts = facts_for_llm(s2, report)
    assert facts["RECOMMENDED_IS_KEEP"] is True and "KEEP_CURRENT_ORDER" not in facts
    assert facts["RECOMMENDED"]["SUPPLIER"].startswith("keep order 4500000432")
    assert facts["OPENING"] == ("Recommend keeping order 4500000432 with supplier 100103 and "
                                "placing no new order, at an expected cost of Rp149,600,000.")
    assert "for the current order 4500000432" in facts["CHEAPEST"]["VS_RECOMMENDED"]["WHY"]
    assert "keeping the current order" in [label for label, _ in required_mentions(facts)]
    assert "Recommended: keep PO 4500000432 (100103), no new order" in format_evaluation(report)


def test_keep_comparison_is_given_when_a_new_order_wins(adapter):
    report = evaluate(adapter, "S1", {})
    facts = facts_for_llm(report["CASES"][0], report)
    versus = facts["KEEP_CURRENT_ORDER"]["VS_RECOMMENDED"]
    assert facts["RECOMMENDED_IS_KEEP"] is False
    assert versus["WHY"].startswith("mainly from extra risk cost Rp")   # 5 days after stock-out
    assert facts["CANDIDATES"] == "6 suppliers ranked, plus keeping the current order"


def test_comparison_sentences_are_built_in_code(adapter):
    # The model wrapped its own words around the cost phrases ("with expected cost Rp ...
    # more expected cost"), so every comparison sentence is now ready-made.
    report = evaluate(adapter, "all", {})
    cases = {c["EBELN"]: facts_for_llm(c, report) for c in report["CASES"]}
    keep = cases["4500000431"]["KEEP_CURRENT_ORDER"]["VS_RECOMMENDED"]["SENTENCE"]
    assert keep.startswith("Keeping the current order 4500000431 would cost Rp")
    assert "expected cost" not in keep.split(", mainly")[0] and keep.endswith(").")
    # FR-SEL-3: the 100109 split is no longer the fastest in S3; 100102 covers all 7,200 kg.
    fastest = cases["4500000433"]["FASTEST"]["VS_RECOMMENDED"]["SENTENCE"]
    assert fastest.startswith("The fastest option, supplier 100102, would cost Rp")
    # The near-tie wording, on the 100109 split row that is Rp125,000 above #1.
    s3 = next(c for c in report["CASES"] if c["EBELN"] == "4500000433")
    rows = {r["LIFNR"]: r for r in s3["SCORING"]["CANDIDATES"]}
    tie = supplier_evaluation._versus_facts(rows["100109"], s3["SCORING"]["CANDIDATES"][0],
                                            s3["QTY"], "Supplier 100109,")["VS_RECOMMENDED"]
    assert tie["SENTENCE"].startswith("Supplier 100109, is a near tie: it would cost Rp125,000 more")
    for facts in cases.values():
        for key in ("KEEP_CURRENT_ORDER", "CHEAPEST", "FASTEST"):
            if key in facts:
                assert not grounding.repeated_phrases(facts[key]["VS_RECOMMENDED"]["SENTENCE"])


# --------------------------------- one world: a case's table does not depend on the run
@pytest.mark.parametrize("scenario,ebeln", [("S1", "4500000431"), ("S2", "4500000432"),
                                            ("S3", "4500000433")])
def test_case_table_is_the_same_alone_and_in_all(adapter, scenario, ebeln):
    # Ranks, recommended, cheapest and fastest labels, every number, and the printed table.
    alone = evaluate(adapter, scenario, {})
    together = evaluate(adapter, "all", {})
    assert [c["EBELN"] for c in alone["CASES"]] == [ebeln]
    case = alone["CASES"][0]
    same = next(c for c in together["CASES"] if c["EBELN"] == ebeln)
    assert json.dumps(case, sort_keys=True) == json.dumps(same, sort_keys=True)
    assert format_case(case) == format_case(same)
    assert alone["FLAGGED_EBELN"] == together["FLAGGED_EBELN"] == [
        "4500000431", "4500000433", "4500000432"]


def test_s1_excludes_the_suppliers_detection_flagged(adapter):
    table = evaluate(adapter, "S1", {})["CASES"][0]["SCORING"]
    excluded = {e["LIFNR"]: e["REASON"] for e in table["EXCLUDED"]}
    assert excluded["100103"] == excluded["100104"] == "DISRUPTED_SUPPLIER"   # PO 432 and 433
    assert table["CHEAPEST"] == "100105"


def test_s3_alone_reserves_what_s1_takes_first(adapter):
    report = evaluate(adapter, "S3", {})
    case = report["CASES"][0]
    assert case["RESERVED_FOR"] == [{"EBELN": "4500000431", "LIFNR": "100109", "KG": 10_000.0}]
    assert case["SCORING"]["RESERVED_KG"] == {"100109": 10_000.0}
    assert "scored only to reserve its capacity" in report["STEPS"][0]["RESULT"]
    text = "\n".join(format_case(case))
    assert ("Capacity rule: spare capacity goes to the flagged POs in Detection's urgency order "
            "(4500000431, 4500000433, 4500000432)") in text
    assert "Reserved for more urgent POs: 100109 10,000 kg (PO 4500000431)" in text
    assert "Cheapest tie-break: on an equal NETPR, fewer OUT d first" in text


def test_evaluation_without_the_flagged_list_fails_loudly(adapter):
    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}))
    det = detection(adapter, "all")
    with pytest.raises(supplier_evaluation.FlaggedListMissing, match="full list of flagged POs"):
        agent.evaluate(det)
    with pytest.raises(supplier_evaluation.FlaggedListMissing, match="missing POs .*4500000433"):
        agent.evaluate(det, det["FLAGGED_POS"][:1])   # only the case to show, not the list
    with pytest.raises(supplier_evaluation.FlaggedListMissing):
        agent.run(det)


def test_cheapest_tie_break_is_stable(adapter):
    # 100103 and 100105 both quote Rp17,450, the lowest price once the S1 supplier is out.
    # 100103 arrives a day earlier, so fewer expected stock-out days.
    table = score_suppliers(MATNR, 10_000, "2026-10-06", exclude_lifnr=["100101"],
                            adapter=adapter, client=StubRpt({}))
    rows = {r["LIFNR"]: r for r in table["CANDIDATES"]}
    assert rows["100103"]["NETPR"] == rows["100105"]["NETPR"] == 17_450
    assert rows["100103"]["EXPECTED_STOCKOUT_DAYS"] < rows["100105"]["EXPECTED_STOCKOUT_DAYS"]
    assert table["CHEAPEST"] == "100103"


@pytest.mark.parametrize("scenario", ["S1", "S2", "S3"])
def test_fastest_rule_is_printed_under_every_table(adapter, scenario):
    case = evaluate(adapter, scenario, {})["CASES"][0]
    lines = format_case(case)
    rule = next(line for line in lines if line.startswith("Fastest rule: "))
    assert rule == supplier_score.fastest_rule_text()
    assert lines.index(rule) == next(i for i, line in enumerate(lines)
                                     if line.startswith("Cheapest tie-break: ")) + 1
    # The printed rule picks the row the table marks fastest, and that row needs no split.
    scope = [r for r in case["SCORING"]["CANDIDATES"] if supplier_score.can_be_fastest(r)]
    fastest = min(scope, key=supplier_score.fastest_key)
    assert fastest["LIFNR"] == case["SCORING"]["FASTEST"] and fastest["COVERS_FULL_QTY"]


def test_s1_fastest_is_unchanged(adapter):
    # 100109 has 12,500 kg spare for 10,000 kg, so it is still the fastest in S1.
    table = evaluate(adapter, "S1", {})["CASES"][0]["SCORING"]
    assert table["FASTEST"] == "100109"
    row = next(r for r in table["CANDIDATES"] if r["IS_FASTEST"])
    assert (row["LEAD_DAYS"], row["COVERS_FULL_QTY"]) == (3, True)


@pytest.mark.parametrize("scenario", ["S2", "S3"])
def test_split_100109_is_not_fastest_in_s2_and_s3(adapter, scenario):
    # 100109 has 2,500 kg left after S1, so it is a split and 100102 (3 days, 50,000 kg) is fastest.
    table = evaluate(adapter, scenario, {})["CASES"][0]["SCORING"]
    rows = {r["LIFNR"]: r for r in table["CANDIDATES"] if r["OPTION"] == "NEW_PO"}
    assert rows["100109"]["SPLIT_WITH"] and not rows["100109"]["IS_FASTEST"]
    assert table["FASTEST"] == "100102" and rows["100102"]["LEAD_DAYS"] == 3
    assert rows["100102"]["COVERS_FULL_QTY"]


def test_no_fastest_option_is_explained_without_error(adapter):
    # 60,000 kg: no backup has that much spare, so every new order is a split.
    report = evaluate(adapter, "S1", {})
    case = report["CASES"][0]
    table = score_suppliers(MATNR, 60_000, case["NEED_BY"],
                            exclude_lifnr=["100101", "100103", "100104"], adapter=adapter,
                            client=StubRpt({}))
    case = {**case, "QTY": 60_000.0, "SCORING": table}
    assert table["CANDIDATES"] and table["FASTEST"] is None
    assert all(r["SPLIT_WITH"] for r in table["CANDIDATES"])
    assert "fastest new order: none can cover the full quantity" in "\n".join(format_case(case))
    facts = facts_for_llm(case, report)
    assert "FASTEST" not in facts and facts["RECOMMENDED_IS_FASTEST"] is False
    assert facts["FASTEST_NOTE"] == supplier_evaluation.FASTEST_NOTE
    assert ("no fastest option", "no fastest option") in required_mentions(facts)

    def explainer(f):
        parts = [f["OPENING"]] + [f[k]["VS_RECOMMENDED"]["SENTENCE"] for k in ("CHEAPEST",)
                                  if k in f]
        parts += [f.get("TRACK_RECORD_WARNING", ""), f["FASTEST_NOTE"], "All figures are simulated."]
        return " ".join(p for p in parts if p)

    agent = SupplierEvaluationAgent(adapter=adapter, rpt_client=StubRpt({}), explainer=explainer)
    out = agent.explain({**report, "CASES": [case]})["CASES"][0]
    assert out["EXPLANATION_ERROR"] is None
    assert out["EXPLANATION_CHECK"]["PASSED"] is True, out["EXPLANATION_CHECK"]
    # Without the note the check fails: the buyer must be told there is no fastest option.
    without_note = out["EXPLANATION"].replace(supplier_evaluation.FASTEST_NOTE, "")
    bad = grounding.check_explanation(without_note, facts, 120,
                                      must_mention=required_mentions(facts))
    assert "no fastest option" in bad["MISSING_MENTIONS"]


@pytest.mark.parametrize("scenario,expected", [
    ("S1", "Keep row = PO 4500000431 as it stands: its price, its expected arrival and its "
           "own P(late)."),
    ("S2", "Keep row = PO 4500000432 as it stands: its price (the revised one after a price "
           "increase), its expected arrival and its own P(late)."),
    ("S3", "Keep row = the 7,200 kg remainder on PO 4500000433 as it stands: its price, its "
           "expected arrival and its own P(late)."),
])
def test_keep_row_sentence_depends_on_the_case(adapter, scenario, expected):
    case = evaluate(adapter, scenario, {})["CASES"][0]
    assert expected in format_case(case)


def test_amounts_are_written_as_in_the_tables(adapter):
    # "Rp178,446,429" in the facts and the explanation, as in the ranked table (no space).
    report = evaluate(adapter, "S1", {})
    facts = facts_for_llm(report["CASES"][0], report)
    text = json.dumps(facts)
    assert "Rp " not in text and "Rp17,550 per kg" in text
    best = report["CASES"][0]["SCORING"]["CANDIDATES"][0]
    assert facts["OPENING"] == f"Recommend supplier 100109, also the fastest option, at an expected cost of Rp{best['EXPECTED_COST']:,}."
    sentence = facts["CHEAPEST"]["VS_RECOMMENDED"]["SENTENCE"]
    check = grounding.check_explanation(
        f"{facts['OPENING']} {sentence} {facts['TRACK_RECORD_WARNING']} Keeping it costs more. "
        "All figures are simulated.", facts, 120, must_mention=required_mentions(facts))
    assert check["PASSED"] is True, check


def test_exclude_flagged_label_has_one_source(adapter):
    text = supplier_evaluation.exclude_flagged_text(True)
    assert text == ("EXCLUDE_FLAGGED_SUPPLIERS: True (proposed rule, see "
                    "docs/prd_change_proposals.md)")
    assert text in format_evaluation(evaluate(adapter, "S1", {}))


def test_s3_keep_wording_names_the_remainder(adapter):
    report = evaluate(adapter, "S3", {})
    case = report["CASES"][0]
    facts = facts_for_llm(case, report)
    kg = f"{case['SCORING']['KEEP']['MENGE']:,.0f}"
    assert kg == "7,200"
    assert facts["KEEP_CURRENT_ORDER"]["VS_RECOMMENDED"]["SENTENCE"].startswith(
        f"Keeping the {kg} kg remainder on the current order 4500000433 would cost Rp")
    assert facts["CANDIDATES"].endswith(f"plus keeping the {kg} kg remainder on the current order")
    assert facts["KEEP_CURRENT_ORDER"]["SUPPLIER"].startswith(f"keep the {kg} kg remainder on order")
    keep = case["SCORING"]["KEEP"]
    assert supplier_evaluation.opening_sentence(keep, remainder=True).startswith(
        f"Recommend keeping the {kg} kg remainder on order 4500000433 with supplier 100104")
    assert f"Keep row = the {kg} kg remainder on PO 4500000433" in "\n".join(format_case(case))
    # S1 and S2 replace the whole PO, so their wording is unchanged.
    s1 = evaluate(adapter, "S1", {})
    assert facts_for_llm(s1["CASES"][0], s1)["KEEP_CURRENT_ORDER"]["VS_RECOMMENDED"][
        "SENTENCE"].startswith("Keeping the current order 4500000431 would cost")
