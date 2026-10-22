"""Minimum order quantity (MINBM) in the quotes and in Supplier Evaluation scoring (FR-SEL-1,
FR-SEL-2, FR-ACT-1, POL-1, D-2, D-3; built, waiting for team agreement:
docs/prd_change_proposals.md item 12). All MINBM values simulated."""
import pytest

from adapters import CsvAdapter
from agents.action import ActionAgent
from agents.detection import DetectionAgent
from agents.run import cases_for, format_case, notices_on_file
from agents.supplier_evaluation import SupplierEvaluationAgent, facts_for_llm
from agents import supplier_evaluation
from integrations.sap.rpt_client import RptClient
from scoring import supplier_score as ss

TODAY, STOCKOUT, DOWNTIME = "2026-09-30", "2026-10-06", 150_000_000
MATNR = "RM-GULA-001"


def quote(lifnr, netpr, lead, available=50_000.0, minbm=None):
    q = {"MATNR": MATNR, "LIFNR": lifnr, "QUOTE_DATE": TODAY, "NETPR": netpr, "LEAD_DAYS": lead,
         "AVAILABLE_KG_NEXT_30D": available, "VALID_TO": "2026-10-07"}
    if minbm is not None:
        q["MINBM"] = minbm
    return q


def score(quotes, qty, p_late=None, d_late=None):
    p_late = p_late or {q["LIFNR"]: 0.1 for q in quotes}
    d_late = d_late or {q["LIFNR"]: 2.0 for q in quotes}
    return ss.score_candidates({q["LIFNR"]: q for q in quotes}, qty, p_late, d_late, TODAY,
                               STOCKOUT, DOWNTIME)


def by_lifnr(rows):
    return {r["LIFNR"]: r for r in rows}


# ------------------------------------------------------------- scoring rule
def test_split_partner_below_moq_is_rounded_up():
    # A has 2,500 kg spare; B takes the 4,700 kg rest but its MINBM is 5,000 kg.
    rows = by_lifnr(score([quote("A", 17550, 3, 2_500.0, 1_000.0),
                           quote("B", 18400, 3, 50_000.0, 5_000.0)], 7_200))
    split = rows["A"]["SPLIT_WITH"]
    assert (split["LIFNR"], split["MENGE"], split["MOQ_EXTRA_KG"]) == ("B", 5_000.0, 300.0)
    assert rows["A"]["MENGE"] == 2_500.0 and rows["A"]["MOQ_EXTRA_KG"] == 0.0
    assert rows["A"]["PURCHASE_COST"] == 2_500 * 17550 + 5_000 * 18400   # 300 kg extra paid
    assert split["PURCHASE_COST"] == 5_000 * 18400


def test_full_order_below_moq_is_rounded_up_with_a_mark():
    rows = score([quote("A", 17000, 3, 50_000.0, 5_000.0)], 4_700)
    [row] = rows
    assert (row["MENGE"], row["MOQ_EXTRA_KG"]) == (5_000.0, 300.0)
    assert row["PURCHASE_COST"] == 5_000 * 17000
    assert row["EXPECTED_COST"] == row["PURCHASE_COST"] + row["RISK_COST"]
    assert ss.moq_text(row["MOQ_EXTRA_KG"]) == "MOQ +300 kg"


def test_moq_does_not_change_the_risk_cost():
    plain = score([quote("A", 17000, 3, 50_000.0)], 4_700)[0]
    moq = score([quote("A", 17000, 3, 50_000.0, 5_000.0)], 4_700)[0]
    assert moq["RISK_COST"] == plain["RISK_COST"]
    assert moq["EXPECTED_STOCKOUT_DAYS"] == plain["EXPECTED_STOCKOUT_DAYS"]


def test_spare_below_moq_is_excluded_as_a_full_order():
    q = quote("A", 17000, 3, available=800.0, minbm=1_000.0)
    assert ss.exclusion_reason(q, {"ZZPANEL": "X"}, TODAY) == ss.EXCLUDED_BELOW_MOQ
    assert score([q], 500) == []   # never scored, even if it reached score_candidates


def test_spare_below_moq_is_skipped_as_a_split_partner():
    # B could take the 4,700 kg rest by capacity (4,800 kg) but not its 5,000 kg MINBM, so C does.
    rows = by_lifnr(score([quote("A", 17550, 3, 2_500.0),
                           quote("B", 17000, 3, 4_800.0, 5_000.0),
                           quote("C", 18400, 3, 50_000.0)], 7_200))
    assert rows["A"]["SPLIT_WITH"]["LIFNR"] == "C"
    assert ss.best_partner(4_700, "A", {"B": quote("B", 17000, 3, 4_800.0, 5_000.0)},
                           {"B": 0.1}, {"B": 2.0}, TODAY, STOCKOUT, DOWNTIME) is None


def test_moq_equal_to_the_need_changes_nothing():
    plain = score([quote("A", 17000, 3, 50_000.0)], 5_000)[0]
    moq = score([quote("A", 17000, 3, 50_000.0, 5_000.0)], 5_000)[0]
    assert moq["MOQ_EXTRA_KG"] == 0.0
    assert {k: v for k, v in moq.items() if k != "MOQ_EXTRA_KG"} == \
        {k: v for k, v in plain.items() if k != "MOQ_EXTRA_KG"}


def test_missing_minbm_behaves_as_before():
    # Old quote files have no MINBM: treated as 0, so nothing is raised or excluded.
    q = quote("A", 17000, 3, 300.0)
    assert ss.minbm(q) == 0.0 and ss.exclusion_reason(q, {"ZZPANEL": "X"}, TODAY) is None
    [row] = score([q, quote("B", 18000, 3, 50_000.0)], 200)[:1]
    assert row["MENGE"] == 200.0 and row["MOQ_EXTRA_KG"] == 0.0


def test_keep_row_is_not_affected():
    current = {"EBELN": "4500000433", "LIFNR": "100103", "NETPR": 17450, "ARRIVAL": "2026-10-11",
               "P_LATE": 0.3}
    keep = ss.keep_row(current, 300.0, 2.0, TODAY, STOCKOUT, DOWNTIME)
    assert keep["MENGE"] == 300.0 and keep["MOQ_EXTRA_KG"] == 0.0
    assert keep["PURCHASE_COST"] == 300 * 17450


def test_rule_text():
    assert ss.MOQ_RULE_TEXT == (
        "MOQ rule: an order below a supplier's MINBM is raised to MINBM and the extra kg is "
        "counted as purchase cost; a supplier whose spare capacity is below MINBM is skipped. "
        "MINBM values simulated.")
    assert chr(0x2014) not in ss.MOQ_RULE_TEXT and chr(0x2013) not in ss.MOQ_RULE_TEXT


# ------------------------------------------------- data and agents (tracked)
class StubRpt(RptClient):
    """SAP-RPT stand-in: 0.14 for supplier 100102 (close to the real run), 0.2 for every other
    row, so 100102 is the S3 split partner as with the real SAP-RPT."""

    def __init__(self):
        self.config = None

    @property
    def model_id(self):
        return "sap-rpt-1.6"

    def predict_late(self, context_rows, query_rows):
        return {r["EBELN"]: 0.14 if r["EBELN"].endswith("100102") else 0.2 for r in query_rows}


@pytest.fixture(scope="module")
def adapter():
    return CsvAdapter()


def evaluation(adapter, scenario):
    rpt = StubRpt()
    detection = DetectionAgent(adapter=adapter, rpt_client=rpt, reasoner=lambda f: "ok").detect(
        MATNR, notices_on_file(scenario, adapter.scenarios()))
    return SupplierEvaluationAgent(adapter=adapter, rpt_client=rpt, explainer=lambda f: "ok").evaluate(
        detection, detection["FLAGGED_POS"], cases_for(scenario, adapter.scenarios()))


def test_quotes_carry_minbm(adapter):
    quotes = {q["LIFNR"]: q for q in adapter.quotes(MATNR)}
    assert quotes["100102"]["MINBM"] == 5_000.0
    assert {q["MINBM"] for l, q in quotes.items() if l != "100102"} == {1_000.0}
    assert all(q["AVAILABLE_KG_NEXT_30D"] >= q["MINBM"] or q["AVAILABLE_KG_NEXT_30D"] == 0
               for q in quotes.values())


def s3_split_row(ev):
    case = next(c for c in ev["CASES"] if c["EBELN"] == "4500000433")
    return case, next(r for r in case["SCORING"]["CANDIDATES"]
                      if r.get("SPLIT_WITH") and r["SPLIT_WITH"]["LIFNR"] == "100102")


def test_s3_split_with_100102_is_raised_to_its_moq(adapter):
    case, row = s3_split_row(evaluation(adapter, "S3"))
    split = row["SPLIT_WITH"]
    assert row["MENGE"] + split["MENGE"] - split["MOQ_EXTRA_KG"] == case["QTY"]
    assert split["MENGE"] == 5_000.0 and split["MOQ_EXTRA_KG"] > 0
    text = "\n".join(format_case(case))
    assert ss.moq_text(split["MOQ_EXTRA_KG"]) in text and ss.MOQ_RULE_TEXT in text


def test_capacity_reservation_uses_the_ordered_kg(adapter):
    ev = evaluation(adapter, "all")
    reserved = {}
    for case in ev["CASES"]:
        for t in case["RESERVED_FOR"]:
            reserved[(t["EBELN"], t["LIFNR"])] = t["KG"]
    first = next(c for c in ev["CASES"] if c["EBELN"] == ev["FLAGGED_EBELN"][0])
    best = first["SCORING"]["CANDIDATES"][0]
    later = [c for c in ev["CASES"] if c["EBELN"] != first["EBELN"]]
    taken = next(t for c in later for t in c["RESERVED_FOR"] if t["EBELN"] == first["EBELN"])
    assert taken["KG"] == best["MENGE"]   # ordered kg, MOQ round-up included


def test_reserve_counts_the_moq_round_up():
    reserved, reserved_for = {}, []
    table = {"CANDIDATES": [{"OPTION": ss.OPTION_NEW_PO, "LIFNR": "100109", "MENGE": 2_500.0,
                             "SPLIT_WITH": {"LIFNR": "100102", "MENGE": 5_000.0,
                                            "MOQ_EXTRA_KG": 300.0}}]}
    supplier_evaluation.reserve(reserved, table, "4500000433", reserved_for)
    assert reserved == {"100109": 2_500.0, "100102": 5_000.0}


def test_draft_menge_and_netwr_include_the_round_up(adapter):
    ev = evaluation(adapter, "S3")
    case, row = s3_split_row(ev)
    # Make the 100102 split the #1 for this test, then draft it.
    case["SCORING"]["CANDIDATES"] = [row]
    report = ActionAgent(adapter=adapter).act({**ev, "CASES": [case]})
    orders = {o["LIFNR"]: o for o in report["CASES"][0]["ORDERS"]}
    assert orders["100102"]["MENGE"] == 5_000.0
    assert orders["100102"]["NETWR"] == 5_000 * row["SPLIT_WITH"]["NETPR"]
    assert orders["100102"]["RELEASE_CHECK"]["ALLOWED"] is True   # POL-1 on that NETWR


def test_explanation_facts_name_the_round_up_and_pass_the_check(adapter):
    from agents import grounding
    ev = evaluation(adapter, "S3")
    case, row = s3_split_row(ev)
    case["SCORING"]["CANDIDATES"] = [row] + [r for r in case["SCORING"]["CANDIDATES"]
                                             if r is not row]
    facts = facts_for_llm(case, ev)
    moq = facts["RECOMMENDED"]["MINIMUM_ORDER"]
    assert "5,000 kg" in moq and "300 kg more than needed" in moq
    text = (f"{facts['OPENING']} The order to supplier 100102 is raised to its minimum order "
            f"quantity of 5,000 kg, 300 kg more than needed. All figures are simulated.")
    check = grounding.check_explanation(text, facts, 120, lifnrs=["100102", "100109"])
    assert not check["UNGROUNDED_NUMBERS"]


def test_no_moq_phrase_without_a_round_up(adapter):
    ev = evaluation(adapter, "S1")
    facts = facts_for_llm(ev["CASES"][0], ev)
    assert "MINIMUM_ORDER" not in facts["RECOMMENDED"]
