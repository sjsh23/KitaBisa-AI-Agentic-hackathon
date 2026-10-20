"""Disruption flag rules and stock-out arithmetic (FR-DET-2, FR-DET-3).

Plain deterministic Python. The detection agent calls these; the LLM only
receives the results.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

P_LATE_THRESHOLD = 0.5            # FR-DET-2: flag when P(late) >= 0.5
PRICE_REVISION_THRESHOLD = 0.03   # FR-DET-2: flag when the revision is > 3% over the PO price

REASON_PREDICTED_LATE = "PREDICTED_LATE"
REASON_DELAY_NOTICE = "DELAY_NOTICE"
REASON_PRICE_INCREASE = "PRICE_INCREASE"
REASON_CAPACITY_SHORTFALL = "CAPACITY_SHORTFALL"


def stockout_date(today: str, days_of_cover: float) -> str:
    """FR-DET-3: stock-out date = today + days of cover (whole days, rounded down)."""
    return (date.fromisoformat(today) + timedelta(days=int(days_of_cover))).isoformat()


def gap_days(expected_arrival: str, stockout: str) -> int:
    """Days from the stock-out date to the expected arrival; positive = arrives too late."""
    return (date.fromisoformat(expected_arrival) - date.fromisoformat(stockout)).days


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def timing_text(gap: int) -> str:
    """GAP_DAYS in words, never a signed number: "arrives 3 days before stock runs out"."""
    if gap < 0:
        return f"arrives {_days(-gap)} before stock runs out"
    if gap > 0:
        return f"arrives {_days(gap)} after stock runs out"
    return "arrives on the day stock runs out"


def price_increase_pct(po_netpr: float, new_netpr: float) -> float:
    """Revision relative to the PO price, as a fraction (0.06 = +6%)."""
    return (new_netpr - po_netpr) / po_netpr


def evaluate_po(
    po: dict[str, Any],
    p_late: float | None,
    notices: list[dict[str, Any]],
    stockout: str,
) -> dict[str, Any]:
    """Apply FR-DET-2 and FR-DET-3 to one open PO line.

    `notices` are the supplier notices for this EBELN (scenarios.json shape).
    Returns the PO with P_LATE, FLAGGED, REASONS, PARTIAL_ARRIVAL, EXPECTED_ARRIVAL,
    GAP_DAYS and ARRIVAL_VS_STOCKOUT (the gap in words). For a capacity shortfall
    EXPECTED_ARRIVAL and GAP_DAYS describe the remainder.
    """
    reasons: list[dict[str, Any]] = []
    expected_arrival = po["EINDT"]
    partial_arrival: str | None = None   # date of the on-time part of a split delivery

    if p_late is not None and p_late >= P_LATE_THRESHOLD:
        reasons.append({"CODE": REASON_PREDICTED_LATE, "P_LATE": p_late, "THRESHOLD": P_LATE_THRESHOLD})

    for notice in notices:
        kind = notice.get("type")
        if kind == "delay_notice":
            expected_arrival = max(expected_arrival, notice["new_eindt"])
            reasons.append({
                "CODE": REASON_DELAY_NOTICE, "ORIGINAL_EINDT": po["EINDT"],
                "NEW_EINDT": notice["new_eindt"],
                "DELAY_DAYS": gap_days(notice["new_eindt"], po["EINDT"]),
            })
        elif kind == "price_increase":
            pct = price_increase_pct(po["NETPR"], notice["new_netpr"])
            if pct > PRICE_REVISION_THRESHOLD:
                new_netpr = int(round(notice["new_netpr"]))
                reasons.append({
                    "CODE": REASON_PRICE_INCREASE, "PO_NETPR": po["NETPR"], "NEW_NETPR": new_netpr,
                    "INCREASE_PCT": round(pct * 100, 1),
                    "THRESHOLD_PCT": round(PRICE_REVISION_THRESHOLD * 100, 1),
                    "EXTRA_COST": int(round((new_netpr - po["NETPR"]) * po["MENGE"])),
                })
        elif kind == "capacity_shortfall":
            deliverable = float(notice["deliverable_kg"])
            reason = {
                "CODE": REASON_CAPACITY_SHORTFALL, "ORDERED_KG": po["MENGE"],
                "DELIVERABLE_KG": deliverable, "SHORTFALL_KG": po["MENGE"] - deliverable,
            }
            # FR-DET-3: the on-time part arrives on the original date, the remainder on
            # the date the notice gives. The gap is computed for the remainder.
            if notice.get("remainder_eindt"):
                on_time_eindt = notice.get("original_eindt", po["EINDT"])
                partial_arrival = on_time_eindt
                expected_arrival = max(expected_arrival, notice["remainder_eindt"])
                reason.update({
                    "ON_TIME_EINDT": on_time_eindt,
                    "REMAINDER_KG": float(notice.get("remainder_kg", reason["SHORTFALL_KG"])),
                    "REMAINDER_EINDT": notice["remainder_eindt"],
                    "REMAINDER_GAP_DAYS": gap_days(notice["remainder_eindt"], stockout),
                })
            reasons.append(reason)

    gap = gap_days(expected_arrival, stockout)
    timing = timing_text(gap)
    if partial_arrival:   # say which part is late, so the on-time part is not mistaken for it
        timing = (f"the on-time part {timing_text(gap_days(partial_arrival, stockout))}, "
                  f"the remainder {timing}")
    return {
        **po,
        "P_LATE": p_late,
        "FLAGGED": bool(reasons),
        "REASONS": reasons,
        # Set only when the delivery is split: the date of the on-time part.
        "PARTIAL_ARRIVAL": partial_arrival,
        "EXPECTED_ARRIVAL": expected_arrival,
        "STOCKOUT_DATE": stockout,
        "GAP_DAYS": gap,
        "ARRIVAL_VS_STOCKOUT": timing,
    }


def urgency_key(po: dict[str, Any]) -> tuple[float, float, str]:
    """Sort key for flagged POs, most urgent first: largest gap after the stock-out date,
    then highest delay risk, then EBELN. Detection orders its flags by it, and Supplier
    Supplier Evaluation hands out spare capacity in the same fixed order (FR-SEL-2)."""
    return (-po["GAP_DAYS"], -(po["P_LATE"] or 0.0), po["EBELN"])
