"""Expected-cost scoring (FR-SEL-1 to FR-SEL-3)."""
import pytest

from scoring import supplier_score as ss

TODAY, STOCKOUT, DOWNTIME = "2026-09-30", "2026-10-06", 150_000_000


def quote(lifnr, netpr, lead, available=50_000.0, valid_to="2026-10-07"):
    return {"MATNR": "RM-GULA-001", "LIFNR": lifnr, "QUOTE_DATE": TODAY, "NETPR": netpr,
            "LEAD_DAYS": lead, "AVAILABLE_KG_NEXT_30D": available, "VALID_TO": valid_to}


def test_arrival_is_today_plus_lead_days():
    assert ss.arrival_date(TODAY, 6) == "2026-10-06"


def test_expected_stockout_days_formula():
    # Arrives 2 days early: only the late branch (p 0.3, 4.5 days late) runs out, by 2.5 days.
    assert ss.expected_stockout_days("2026-10-04", STOCKOUT, 0.3, 4.5) == pytest.approx(0.75)
    # Arrives 1 day after stock-out: 1 day when on time, 1 + 2 days when late.
    assert ss.expected_stockout_days("2026-10-07", STOCKOUT, 0.5, 2.0) == pytest.approx(2.0)
    # Early enough that even a late delivery is in time.
    assert ss.expected_stockout_days("2026-10-01", STOCKOUT, 0.9, 3.0) == 0.0


def test_delay_when_late_is_shrunk_to_the_pooled_average():
    assert ss.mean_delay_when_late([0, -1, 2, 4]) == 3.0
    assert ss.mean_delay_when_late([0, -1]) is None
    # One late receipt of 1 day, pooled average 3.5, weight 3: (1 + 10.5) / 4.
    assert ss.delay_when_late([1, 0, 0], pooled=3.5) == pytest.approx(2.875)
    assert ss.delay_when_late([0, 0], pooled=3.5) == pytest.approx(3.5)


@pytest.mark.parametrize("q, panel, disrupted, reason", [
    (quote("100102", 18400, 3), "X", (), None),
    (quote("100111", 16600, 2), "", (), ss.EXCLUDED_OFF_PANEL),
    (quote("100102", 18400, 3, valid_to="2026-09-29"), "X", (), ss.EXCLUDED_QUOTE_NOT_VALID),
    (quote("100101", 16500, 6), "X", ("100101",), ss.EXCLUDED_DISRUPTED),
    (quote("100104", 17950, 2, available=0.0), "X", (), ss.EXCLUDED_NO_SPARE_CAPACITY),
])
def test_exclusion_reasons(q, panel, disrupted, reason):
    supplier = {"LIFNR": q["LIFNR"], "ZZPANEL": panel}
    assert ss.exclusion_reason(q, supplier, TODAY, disrupted) == reason


def test_unknown_supplier_is_treated_as_off_panel():
    assert ss.exclusion_reason(quote("199999", 1, 1), None, TODAY) == ss.EXCLUDED_OFF_PANEL


def score(quotes, qty, p_late, d_late):
    return ss.score_candidates({q["LIFNR"]: q for q in quotes}, qty, p_late, d_late,
                               TODAY, STOCKOUT, DOWNTIME)


def test_ranking_prices_in_the_stockout_risk():
    quotes = [quote("A", 16500, 6), quote("B", 17550, 3), quote("C", 18400, 2)]
    p_late = {"A": 0.4, "B": 0.2, "C": 0.1}
    d_late = {"A": 4.0, "B": 2.0, "C": 2.0}
    rows = score(quotes, 10_000, p_late, d_late)

    assert [r["LIFNR"] for r in rows] == ["B", "C", "A"]
    a = rows[2]
    assert a["PURCHASE_COST"] == 165_000_000
    assert a["EXPECTED_STOCKOUT_DAYS"] == pytest.approx(1.6)      # 0.4 x (0 + 4)
    assert a["RISK_COST"] == 240_000_000
    assert a["EXPECTED_COST"] == 405_000_000
    assert rows[0]["EXPECTED_COST"] == 175_500_000 and rows[0]["RISK_COST"] == 0
    assert [r["RANK"] for r in rows] == [1, 2, 3]
    assert rows[0]["SCORE"] == 100.0
    assert a["SCORE"] == round(100 * 175_500_000 / 405_000_000, 1)
    assert [r["LIFNR"] for r in rows if r["IS_CHEAPEST"]] == ["A"]
    assert [r["LIFNR"] for r in rows if r["IS_FASTEST"]] == ["C"]
    assert all(r["COVERS_FULL_QTY"] and r["SPLIT_WITH"] is None for r in rows)


def test_short_supplier_is_evaluated_as_a_split_order():
    quotes = [quote("A", 17000, 3, available=4_000.0), quote("B", 17500, 4), quote("C", 18000, 4)]
    p_late = {"A": 0.0, "B": 0.5, "C": 0.0}
    d_late = {"A": 2.0, "B": 4.0, "C": 2.0}
    rows = {r["LIFNR"]: r for r in score(quotes, 10_000, p_late, d_late)}

    a = rows["A"]
    assert a["COVERS_FULL_QTY"] is False and a["MENGE"] == 4_000.0
    # The rest goes to C: B is cheaper per kg but its 1-day expected stock-out costs more.
    assert a["SPLIT_WITH"]["LIFNR"] == "C" and a["SPLIT_WITH"]["MENGE"] == 6_000.0
    assert a["PURCHASE_COST"] == 4_000 * 17000 + 6_000 * 18000
    assert a["EXPECTED_STOCKOUT_DAYS"] == 0.0


def test_split_takes_the_larger_stockout_of_its_parts():
    quotes = [quote("A", 17000, 3, available=4_000.0), quote("B", 17500, 4)]
    rows = {r["LIFNR"]: r for r in score(quotes, 10_000, {"A": 0.0, "B": 0.5},
                                         {"A": 2.0, "B": 4.0})}
    assert rows["A"]["SPLIT_WITH"]["LIFNR"] == "B"
    assert rows["A"]["EXPECTED_STOCKOUT_DAYS"] == rows["B"]["EXPECTED_STOCKOUT_DAYS"] == 1.0


def test_split_without_a_partner_is_dropped():
    quotes = [quote("A", 17000, 3, available=4_000.0), quote("B", 17500, 4, available=5_000.0)]
    assert score(quotes, 10_000, {"A": 0.1, "B": 0.1}, {"A": 2.0, "B": 2.0}) == []


# ---------------------------------------------------------------- hand-checked values

def test_hand_checked_100109_in_s1():
    # Arrival 2026-10-03 is 3 days before the stock-out on 2026-10-06. On time: no gap.
    # Late by d_late 3.1 days: 3.1 - 3 = 0.1 day without stock. Expected: 0.22 x 0.1 = 0.022.
    days = ss.expected_stockout_days("2026-10-03", STOCKOUT, 0.22, 3.1)
    assert days == pytest.approx(0.022)
    part = ss.score_part(quote("100109", 17550, 3, available=12_500.0), 10_000, 0.22, 3.1,
                         TODAY, STOCKOUT)
    row = ss.finish_row([part], DOWNTIME)
    assert row["ARRIVAL"] == "2026-10-03"
    assert row["PURCHASE_COST"] == 175_500_000                  # 10,000 kg x Rp17,550
    assert row["RISK_COST"] == 3_300_000                        # 0.022 x Rp150,000,000
    assert row["EXPECTED_COST"] == 178_800_000
    assert row["EXPECTED_STOCKOUT_DAYS"] == 0.02


def test_capacity_at_or_above_qty_is_not_split():
    rows = score([quote("A", 17000, 3, available=10_000.0)], 10_000, {"A": 0.1}, {"A": 2.0})
    assert rows[0]["COVERS_FULL_QTY"] is True and rows[0]["SPLIT_WITH"] is None
    assert rows[0]["CAPACITY_NOTE"] is None and rows[0]["MENGE"] == 10_000.0


def test_capacity_below_qty_is_split_and_adds_up():
    quotes = [quote("A", 17000, 3, available=2_500.0), quote("B", 18000, 3)]
    rows = {r["LIFNR"]: r for r in score(quotes, 7_200, {"A": 0.0, "B": 0.0},
                                         {"A": 2.0, "B": 2.0})}
    a = rows["A"]
    assert a["CAPACITY_NOTE"] == ss.CANNOT_COVER_NOTE and a["COVERS_FULL_QTY"] is False
    assert a["AVAILABLE_KG"] == 2_500.0 and a["MENGE"] == 2_500.0
    assert a["MENGE"] + a["SPLIT_WITH"]["MENGE"] == 7_200.0
    assert a["PURCHASE_COST"] == 2_500 * 17000 + 4_700 * 18000


def test_zero_capacity_is_excluded_and_never_scored():
    q = quote("A", 17000, 3, available=0.0)
    assert ss.exclusion_reason(q, {"ZZPANEL": "X"}, TODAY) == ss.EXCLUDED_NO_SPARE_CAPACITY
    assert score([q, quote("B", 18000, 3)], 5_000, {"A": 0.0, "B": 0.0},
                 {"A": 2.0, "B": 2.0})[0]["LIFNR"] == "B"
    assert len(score([q, quote("B", 18000, 3)], 5_000, {"A": 0.0, "B": 0.0},
                     {"A": 2.0, "B": 2.0})) == 1


def test_score_is_100_times_lowest_over_own():
    rows = score([quote("A", 17000, 3), quote("B", 18000, 3)], 10_000,
                 {"A": 0.0, "B": 0.0}, {"A": 2.0, "B": 2.0})
    assert [r["SCORE"] for r in rows] == [100.0, round(100 * 170_000_000 / 180_000_000, 1)]
    assert rows[1]["SCORE"] == 94.4


def test_cheapest_and_fastest_marks_with_ties():
    # B and C share the lowest price; C ranks better (B arrives 1 day before the stock-out
    # and may be 3 days late), so C is the one marked cheapest.
    quotes = [quote("A", 17500, 2), quote("B", 17000, 5), quote("C", 17000, 3)]
    rows = score(quotes, 5_000, {"A": 0.0, "B": 0.5, "C": 0.0}, {"A": 3.0, "B": 3.0, "C": 3.0})
    assert [r["LIFNR"] for r in rows].index("C") < [r["LIFNR"] for r in rows].index("B")
    assert [r["LIFNR"] for r in rows if r["IS_CHEAPEST"]] == ["C"]
    assert [r["LIFNR"] for r in rows if r["IS_FASTEST"]] == ["A"]


def test_near_tie_under_one_percent():
    assert ss.is_near_tie(100_999_999, 100_000_000) is True     # 0.999...%
    assert ss.is_near_tie(101_000_000, 100_000_000) is False    # exactly 1%
    assert ss.is_near_tie(100_000_000, 100_000_000) is True


def test_comparison_never_has_negative_amounts():
    best = {"PURCHASE_COST": 175_500_000, "RISK_COST": 2_000_000, "EXPECTED_COST": 177_500_000}
    cheaper_but_risky = {"PURCHASE_COST": 174_500_000, "RISK_COST": 20_000_000,
                         "EXPECTED_COST": 194_500_000}
    dearer_and_safer = {"PURCHASE_COST": 184_000_000, "RISK_COST": 0,
                        "EXPECTED_COST": 184_000_000}
    a = ss.compare_to_best(cheaper_but_risky, best)
    assert a["PURCHASE"] == {"DIRECTION": "purchase saving", "AMOUNT": 1_000_000}
    assert a["RISK"] == {"DIRECTION": "extra risk cost", "AMOUNT": 18_000_000}
    assert a["EXPECTED_COST_GAP"] == 17_000_000 and a["NEAR_TIE"] is False
    b = ss.compare_to_best(dearer_and_safer, best)
    assert b["PURCHASE"] == {"DIRECTION": "extra purchase cost", "AMOUNT": 8_500_000}
    assert b["RISK"] == {"DIRECTION": "lower risk cost", "AMOUNT": 2_000_000}
    assert ss.compare_to_best(best, best)["PURCHASE"] == {"DIRECTION": "same purchase cost",
                                                          "AMOUNT": 0}


def test_main_driver_names_the_cost_that_makes_a_row_lose():
    # FR-SEL-4: the reason #1 wins, decided in code, never by the LLM.
    assert ss.main_driver(-1_000_000, 18_000_000) == "RISK"       # cheaper but risky
    assert ss.main_driver(8_500_000, -2_000_000) == "PURCHASE"    # dearer and safer
    assert ss.main_driver(3_000_000, 5_000_000) == "RISK"         # both extra: larger wins
    assert ss.main_driver(5_000_000, 3_000_000) == "PURCHASE"
    assert ss.main_driver(0, 0) is None
    best = {"PURCHASE_COST": 1, "RISK_COST": 1, "EXPECTED_COST": 2}
    assert ss.compare_to_best(best, best)["MAIN_DRIVER"] is None
