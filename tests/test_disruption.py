"""Flag rules and stock-out arithmetic (FR-DET-2, FR-DET-3)."""
import pytest

from scoring import disruption

STOCKOUT = "2026-10-06"
PO = {"EBELN": "4500000431", "LIFNR": "100101", "EINDT": "2026-10-04",
      "MENGE": 10000.0, "NETPR": 17150}


def codes(result):
    return [r["CODE"] for r in result["REASONS"]]


def test_stockout_date_is_today_plus_days_of_cover():
    assert disruption.stockout_date("2026-09-30", 6.0) == "2026-10-06"
    assert disruption.stockout_date("2026-09-30", 6.9) == "2026-10-06"


def test_gap_days_sign():
    assert disruption.gap_days("2026-10-11", STOCKOUT) == 5
    assert disruption.gap_days("2026-10-04", STOCKOUT) == -2


def test_timing_text_never_has_a_signed_number():
    # Team note 19 Oct: "-3 days after stock runs out" reads badly; say "before".
    assert disruption.timing_text(-3) == "arrives 3 days before stock runs out"
    assert disruption.timing_text(-1) == "arrives 1 day before stock runs out"
    assert disruption.timing_text(5) == "arrives 5 days after stock runs out"
    assert disruption.timing_text(0) == "arrives on the day stock runs out"


def test_no_flag_below_threshold_and_without_notice():
    result = disruption.evaluate_po(PO, 0.49, [], STOCKOUT)
    assert result["FLAGGED"] is False
    assert result["EXPECTED_ARRIVAL"] == "2026-10-04"
    assert result["GAP_DAYS"] == -2


def test_predicted_late_flags_at_threshold():
    assert codes(disruption.evaluate_po(PO, 0.5, [], STOCKOUT)) == ["PREDICTED_LATE"]


def test_missing_prediction_does_not_flag():
    assert disruption.evaluate_po(PO, None, [], STOCKOUT)["FLAGGED"] is False


def test_delay_notice_moves_expected_arrival():
    notice = {"type": "delay_notice", "new_eindt": "2026-10-11"}
    result = disruption.evaluate_po(PO, 0.1, [notice], STOCKOUT)
    assert codes(result) == ["DELAY_NOTICE"]
    assert result["REASONS"][0]["DELAY_DAYS"] == 7
    assert result["EXPECTED_ARRIVAL"] == "2026-10-11"
    assert result["GAP_DAYS"] == 5


@pytest.mark.parametrize("new_netpr, flagged", [(17650, False), (17664, False), (17665, True)])
def test_price_revision_flags_only_above_3_percent(new_netpr, flagged):
    # 17,150 x 1.03 = 17,664.5, so 17,664 is under the threshold and 17,665 is over it.
    notice = {"type": "price_increase", "new_netpr": new_netpr}
    assert disruption.evaluate_po(PO, 0.1, [notice], STOCKOUT)["FLAGGED"] is flagged


def test_price_revision_reports_extra_cost_in_idr():
    notice = {"type": "price_increase", "new_netpr": 18200.0}
    reason = disruption.evaluate_po(PO, 0.1, [notice], STOCKOUT)["REASONS"][0]
    assert reason["INCREASE_PCT"] == 6.1
    assert reason["EXTRA_COST"] == 10_500_000


def test_capacity_shortfall_reports_missing_quantity():
    notice = {"type": "capacity_shortfall", "deliverable_kg": 4000.0}
    reason = disruption.evaluate_po(PO, 0.1, [notice], STOCKOUT)["REASONS"][0]
    assert reason["CODE"] == "CAPACITY_SHORTFALL"
    assert reason["SHORTFALL_KG"] == 6000.0


def test_capacity_shortfall_gap_is_for_the_remainder():
    notice = {"type": "capacity_shortfall", "deliverable_kg": 4000.0, "remainder_kg": 6000.0,
              "original_eindt": "2026-10-04", "remainder_eindt": "2026-10-14"}
    result = disruption.evaluate_po(PO, 0.1, [notice], STOCKOUT)
    assert result["PARTIAL_ARRIVAL"] == "2026-10-04"
    assert result["EXPECTED_ARRIVAL"] == "2026-10-14" and result["GAP_DAYS"] == 8
    assert result["REASONS"][0]["REMAINDER_GAP_DAYS"] == 8
    assert result["ARRIVAL_VS_STOCKOUT"] == ("the on-time part arrives 2 days before stock runs "
                                             "out, the remainder arrives 8 days after stock runs out")
