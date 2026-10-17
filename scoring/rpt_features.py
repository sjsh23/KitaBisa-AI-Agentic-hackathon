"""Feature row for a PO that does not exist yet (FR-SEL-2: p_late from SAP-RPT).

Builds the `rpt_po_lines` schema (docs/data_dictionary.md) for "order Q kg from
supplier s today", with the same definitions the history rows use, so SAP-RPT
scores it like any other open PO. Only data the agent may read is used (D-1).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# Same calendar assumptions as the generator (D-4, data_gen/config.py). Copied, not
# imported, because that module also holds the hidden supplier parameters.
RAINY_MONTHS = frozenset({11, 12, 1, 2, 3})
IDUL_FITRI = ("2024-04-10", "2025-03-31", "2026-03-20", "2027-03-10")
LEBARAN_WINDOW = (-10, 7)   # days before and after Idul Fitri
RECENT_ORDER_DAYS = 30      # window of SUPPLIER_UTIL_30D and AVAILABLE_KG_NEXT_30D
HISTORY_WINDOW_DAYS = 90    # window of the SUP_*_90D features


def is_rainy(day: str) -> bool:
    return date.fromisoformat(day).month in RAINY_MONTHS


def in_lebaran_window(day: str) -> bool:
    d = date.fromisoformat(day)
    low, high = LEBARAN_WINDOW
    return any(low <= (d - date.fromisoformat(f)).days <= high for f in IDUL_FITRI)


def recent_ordered_kg(po_lines: list[dict[str, Any]], lifnr: str, today: str) -> float:
    """KG ordered from `lifnr` in the 30 days up to and including today."""
    start = (date.fromisoformat(today) - timedelta(days=RECENT_ORDER_DAYS)).isoformat()
    return sum(line["MENGE"] for line in po_lines
               if line["LIFNR"] == lifnr and start < line["BEDAT"] <= today)


def utilization(recent_kg: float, available_kg: float, qty: float) -> float | None:
    """SUPPLIER_UTIL_30D including this order.

    Monthly capacity is not published, but the quote gives the spare part of it:
    capacity = AVAILABLE_KG_NEXT_30D + recent orders. When nothing is spare the
    capacity is unknown and the feature is left empty.
    """
    capacity = available_kg + recent_kg
    if available_kg <= 0 or capacity <= 0:
        return None
    return round((recent_kg + qty) / capacity, 3)


def history_features(receipts: list[dict[str, Any]], today: str) -> dict[str, Any]:
    """SUP_* features from the supplier's goods receipts posted before today."""
    start = (date.fromisoformat(today) - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    prior = [r for r in receipts if r["BUDAT"] < today]
    window = [r for r in prior if r["BUDAT"] >= start]
    return {
        "SUP_N_RECEIPTS_ALL": len(prior),
        "SUP_N_RECEIPTS_90D": len(window),
        "SUP_LATE_RATE_90D": round(sum(r["LATE"] for r in window) / len(window), 3)
        if window else None,
        "SUP_AVG_DELAY_90D": round(sum(max(0, r["DELAY_DAYS"]) for r in window) / len(window), 2)
        if window else None,
    }


def hypothetical_row(row_id: str, quote: dict[str, Any], qty: float, today: str,
                     market_price: float, receipts: list[dict[str, Any]],
                     recent_kg: float) -> dict[str, Any]:
    """`rpt_po_lines` row for ordering `qty` kg today at the quoted price and lead time."""
    eindt = (date.fromisoformat(today) + timedelta(days=int(quote["LEAD_DAYS"]))).isoformat()
    return {
        "EBELN": row_id, "EBELP": 10, "LIFNR": quote["LIFNR"], "MATNR": quote["MATNR"],
        "BEDAT": today, "EINDT": eindt, "ORDER_MONTH": date.fromisoformat(today).month,
        "MENGE": float(qty), "LEAD_DAYS": int(quote["LEAD_DAYS"]), "NETPR": int(quote["NETPR"]),
        "PRICE_VS_MARKET": round(quote["NETPR"] / market_price, 4),
        "RAINY_SEASON": int(is_rainy(eindt)),
        "LEBARAN_WINDOW": int(in_lebaran_window(eindt)),
        "SUPPLIER_UTIL_30D": utilization(recent_kg, quote["AVAILABLE_KG_NEXT_30D"], qty),
        **history_features(receipts, today),
        "LATE": None, "DELAY_DAYS": None, "SPLIT": "predict",
    }
