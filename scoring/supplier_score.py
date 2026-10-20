"""Expected-cost supplier scoring (FR-SEL-1 to FR-SEL-3).

Plain deterministic Python; the LLM never computes these numbers (FR-SEL-2).
For a candidate s and quantity Q:

    arrival        = today + LEAD_DAYS_s
    stockout_days  = p_late x max(0, arrival + d_late - stockout_date)
                     + (1 - p_late) x max(0, arrival - stockout_date)
    expected_cost  = Q x NETPR_s + stockout_days x DOWNTIME_COST_PER_DAY
    score          = 100 x (lowest expected_cost / expected_cost_s)

p_late comes from SAP-RPT (or the reliability fallback); d_late is the
supplier's average delay when late, shrunk towards the all-supplier average.

A candidate is never ranked for more than it can supply. When Q is above its
AVAILABLE_KG_NEXT_30D the row is marked "cannot cover full quantity" and
evaluated as a split order: it delivers its spare capacity, the best other
candidate delivers the rest (the kilograms add up to Q). The line stops while
either part is missing, so the split's expected stock-out days are the larger of
the two parts' (a conservative choice). A supplier with no spare capacity is not
a candidate at all.

Keeping the current PO is ranked in the same table (OPTION KEEP_CURRENT_PO, not
yet in FR-SEL-1, proposed in docs/prd_change_proposals.md): the same formula with
the PO's own price (the revised one after a price increase), its expected arrival
and its P(late). If it ranks #1, GESIT recommends no new order. The cheapest and
fastest marks are given among the new orders only (the FR-SEL-4 baselines); the
fastest only among those that cover the full quantity from one supplier (FR-SEL-3).

Comparisons between #1 and another row are computed here too, so the LLM only
quotes them: differences are always positive amounts with a direction word, a
gap in expected cost under 1 percent is a near tie, and the main driver says
whether the extra risk cost or the extra purchase cost makes the row lose.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

# Why a quote is not a candidate (FR-SEL-1). Listed in the output, never hidden.
EXCLUDED_OFF_PANEL = "OFF_PANEL"                 # LFA1.ZZPANEL blank (see POL-2)
EXCLUDED_QUOTE_NOT_VALID = "QUOTE_NOT_VALID"     # today outside QUOTE_DATE..VALID_TO
EXCLUDED_NO_SPARE_CAPACITY = "NO_SPARE_CAPACITY" # AVAILABLE_KG_NEXT_30D is 0
EXCLUDED_DISRUPTED = "DISRUPTED_SUPPLIER"        # has a flagged PO in this detection run

OPTION_NEW_PO = "NEW_PO"              # buy the quantity from a backup supplier
OPTION_KEEP = "KEEP_CURRENT_PO"       # keep the flagged PO as it is, no new order

CANNOT_COVER_NOTE = "cannot cover full quantity"
NEAR_TIE_PCT = 1.0   # expected cost gap to #1 below this many percent is a near tie

# d_late shrinkage: the supplier's late receipts count alongside this many pseudo-receipts
# at the all-supplier average delay (same idea as the Beta prior in scoring.reliability).
D_LATE_PRIOR_WEIGHT = 3.0


def arrival_date(today: str, lead_days: int) -> str:
    """Arrival of an order placed today = today + LEAD_DAYS."""
    return (date.fromisoformat(today) + timedelta(days=int(lead_days))).isoformat()


def days_between(later: str, earlier: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def expected_stockout_days(arrival: str, stockout: str, p_late: float, d_late: float) -> float:
    """FR-SEL-2: expected days without stock, on time with 1 - p_late, late by d_late with p_late."""
    after = days_between(arrival, stockout)   # positive = arrives after the stock-out date
    return p_late * max(0.0, after + d_late) + (1.0 - p_late) * max(0.0, float(after))


def mean_delay_when_late(delays: Iterable[int]) -> float | None:
    """Average delay of the late receipts (DELAY_DAYS > 0), or None when there are none."""
    late = [d for d in delays if d > 0]
    return sum(late) / len(late) if late else None


def delay_when_late(delays: Iterable[int], pooled: float,
                    prior_weight: float = D_LATE_PRIOR_WEIGHT) -> float:
    """d_late for one supplier (FR-SEL-2, "from history").

    The supplier's own average delay when late, pulled towards the all-supplier
    average `pooled` as if it had `prior_weight` extra late receipts at that
    average. One or two late receipts then cannot set d_late on their own, and a
    supplier that was never late gets the pooled value.
    """
    late = [d for d in delays if d > 0]
    return (sum(late) + prior_weight * pooled) / (len(late) + prior_weight)


def exclusion_reason(quote: dict[str, Any], supplier: dict[str, Any] | None, today: str,
                     disrupted: Iterable[str] = ()) -> str | None:
    """FR-SEL-1: None when the quote is a candidate, otherwise the reason it is not."""
    if supplier is None or supplier.get("ZZPANEL") != "X":
        return EXCLUDED_OFF_PANEL
    if not quote["QUOTE_DATE"] <= today <= quote["VALID_TO"]:
        return EXCLUDED_QUOTE_NOT_VALID
    if quote["LIFNR"] in set(disrupted):
        return EXCLUDED_DISRUPTED
    if (quote.get("AVAILABLE_KG_NEXT_30D") or 0.0) <= 0.0:
        return EXCLUDED_NO_SPARE_CAPACITY
    return None


def score_part(quote: dict[str, Any], qty: float, p_late: float, d_late: float, today: str,
               stockout: str) -> dict[str, Any]:
    """Cost components of buying `qty` kg from one supplier (no downtime cost yet)."""
    arrival = arrival_date(today, quote["LEAD_DAYS"])
    return {
        "LIFNR": quote["LIFNR"],
        "MENGE": float(qty),
        "NETPR": int(quote["NETPR"]),
        "LEAD_DAYS": int(quote["LEAD_DAYS"]),
        "AVAILABLE_KG": float(quote["AVAILABLE_KG_NEXT_30D"]),
        "ARRIVAL": arrival,
        "SLACK_DAYS": days_between(stockout, arrival),   # positive = arrives before stock-out
        "P_LATE": p_late,
        "D_LATE": d_late,
        "PURCHASE_COST": int(round(qty * quote["NETPR"])),
        "STOCKOUT_DAYS": expected_stockout_days(arrival, stockout, p_late, d_late),
    }


def finish_row(parts: list[dict[str, Any]], downtime_cost_per_day: int) -> dict[str, Any]:
    """One table row from one part (full order) or two parts (split order)."""
    main = parts[0]
    stockout_days = max(p["STOCKOUT_DAYS"] for p in parts)
    purchase = sum(p["PURCHASE_COST"] for p in parts)
    risk = int(round(stockout_days * downtime_cost_per_day))
    return {
        "LIFNR": main["LIFNR"],
        "NETPR": main["NETPR"],
        "LEAD_DAYS": main["LEAD_DAYS"],
        "ARRIVAL": main["ARRIVAL"],
        "SLACK_DAYS": main["SLACK_DAYS"],
        "P_LATE": round(main["P_LATE"], 4),
        "P_LATE_PCT": round(main["P_LATE"] * 100, 1),
        "D_LATE": round(main["D_LATE"], 2),
        "MENGE": main["MENGE"],                 # kg from this supplier
        "AVAILABLE_KG": main["AVAILABLE_KG"],   # its spare capacity in this run
        "COVERS_FULL_QTY": len(parts) == 1,
        "CAPACITY_NOTE": None if len(parts) == 1 else CANNOT_COVER_NOTE,
        "SPLIT_WITH": None if len(parts) == 1 else _split_summary(parts[1]),
        "PURCHASE_COST": purchase,
        "EXPECTED_STOCKOUT_DAYS": round(stockout_days, 2),
        "RISK_COST": risk,
        "EXPECTED_COST": purchase + risk,
    }


def _split_summary(part: dict[str, Any]) -> dict[str, Any]:
    """The second supplier of a split order, as shown in the table."""
    return {"LIFNR": part["LIFNR"], "MENGE": part["MENGE"], "NETPR": part["NETPR"],
            "ARRIVAL": part["ARRIVAL"], "P_LATE": round(part["P_LATE"], 4),
            "PURCHASE_COST": part["PURCHASE_COST"],
            "EXPECTED_STOCKOUT_DAYS": round(part["STOCKOUT_DAYS"], 2)}


def best_partner(remainder: float, short_lifnr: str, quotes: dict[str, dict[str, Any]],
                 p_late: dict[str, float], d_late: dict[str, float], today: str,
                 stockout: str, downtime_cost_per_day: int) -> dict[str, Any] | None:
    """The other candidate with the lowest expected cost for `remainder` kg, or None."""
    options = [
        score_part(q, remainder, p_late[lifnr], d_late[lifnr], today, stockout)
        for lifnr, q in quotes.items()
        if lifnr != short_lifnr and q["AVAILABLE_KG_NEXT_30D"] >= remainder
    ]
    if not options:
        return None
    return min(options, key=lambda p: (
        p["PURCHASE_COST"] + p["STOCKOUT_DAYS"] * downtime_cost_per_day, p["NETPR"], p["LIFNR"]))


def keep_row(current: dict[str, Any], qty: float, d_late: float, today: str, stockout: str,
             downtime_cost_per_day: int) -> dict[str, Any]:
    """Keeping the flagged PO as it is, costed with the same formula as a new order.

    `current` has EBELN, LIFNR, NETPR (revised after a price increase), ARRIVAL (the
    expected arrival from detection: a delay notice's new date, or the remainder's date)
    and P_LATE (its own prediction).
    """
    arrival = current["ARRIVAL"]
    part = {
        "LIFNR": current["LIFNR"],
        "MENGE": float(qty),
        "NETPR": int(current["NETPR"]),
        "LEAD_DAYS": max(0, days_between(arrival, today)),
        "AVAILABLE_KG": None,                  # already ordered, no spare capacity involved
        "ARRIVAL": arrival,
        "SLACK_DAYS": days_between(stockout, arrival),
        "P_LATE": current["P_LATE"],
        "D_LATE": d_late,
        "PURCHASE_COST": int(round(qty * current["NETPR"])),
        "STOCKOUT_DAYS": expected_stockout_days(arrival, stockout, current["P_LATE"], d_late),
    }
    return {**finish_row([part], downtime_cost_per_day), "OPTION": OPTION_KEEP,
            "KEEP_EBELN": current["EBELN"]}


def score_candidates(quotes: dict[str, dict[str, Any]], qty: float, p_late: dict[str, float],
                     d_late: dict[str, float], today: str, stockout: str,
                     downtime_cost_per_day: int,
                     keep: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """FR-SEL-2 and FR-SEL-3: ranked rows, best first, with score, cheapest and fastest marked.

    `quotes` are the candidates only (already filtered by `exclusion_reason`), keyed by LIFNR.
    `keep` is the row from `keep_row`, ranked alongside the new orders when given.
    """
    rows = []
    for lifnr, quote in quotes.items():
        available = quote["AVAILABLE_KG_NEXT_30D"]
        if available <= 0:
            continue   # nothing to offer; `exclusion_reason` normally removed it already
        if available >= qty:
            parts = [score_part(quote, qty, p_late[lifnr], d_late[lifnr], today, stockout)]
        else:
            partner = best_partner(qty - available, lifnr, quotes, p_late, d_late, today,
                                   stockout, downtime_cost_per_day)
            if partner is None:
                continue   # nobody can deliver the rest, so this split is not an option
            parts = [score_part(quote, available, p_late[lifnr], d_late[lifnr], today, stockout),
                     partner]
        rows.append(finish_row(parts, downtime_cost_per_day))
    if keep is not None:
        rows.append(keep)
    return rank(rows)


# Which new order is "fastest" (FR-SEL-3). Only a new order that covers the full quantity
# from one supplier can be (COVERS_FULL_QTY, the same capacity test that makes a row a
# split); a split stays ranked but is never marked fastest. Among those, these fields in
# order, lowest first, each with the table column that shows it. After LEAD_DAYS they are
# the ranking order of `rank`, so on an equal lead time the better-ranked row wins.
FASTEST_SCOPE = "new orders that can cover the full quantity from one supplier"
FASTEST_ORDER = (("LEAD_DAYS", "LEAD"), ("EXPECTED_COST", "EXPECTED"), ("NETPR", "NETPR"),
                 ("LIFNR", "LIFNR"))
NO_FASTEST_NOTE = "none can cover the full quantity"


def can_be_fastest(row: dict[str, Any]) -> bool:
    """FASTEST_SCOPE: a new order that needs no split."""
    return row.get("OPTION", OPTION_NEW_PO) == OPTION_NEW_PO and bool(row["COVERS_FULL_QTY"])


def fastest_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Sort key for the "fastest" mark, built from FASTEST_ORDER."""
    return tuple(row[field] for field, _ in FASTEST_ORDER)


def fastest_rule_text() -> str:
    """The fastest rule in words, from FASTEST_SCOPE and FASTEST_ORDER, so it cannot drift."""
    first, *ties = [column for _, column in FASTEST_ORDER]
    return (f"Fastest rule: shortest {first} among {FASTEST_SCOPE}; on an equal {first}, lower "
            f"{ties[0]} first, " + ", ".join(f"then lower {t}" for t in ties[1:]) + ".")


def cheapest_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    """Which new order is "cheapest": lowest NETPR; on an equal price fewer expected
    stock-out days, then more spare capacity, then the lower LIFNR (FR-SEL-3)."""
    return (row["NETPR"], row["EXPECTED_STOCKOUT_DAYS"], -(row["AVAILABLE_KG"] or 0.0),
            row["LIFNR"])


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by expected cost, then add RANK, SCORE, IS_CHEAPEST and IS_FASTEST.

    On equal expected cost keeping the current PO goes first (no new order needed).
    Cheapest and fastest are marked among the new orders only; `cheapest_key` breaks
    a tie in price, so the label never depends on the order the quotes came in.
    """
    rows = sorted(({"OPTION": OPTION_NEW_PO, **r} for r in rows),
                  key=lambda r: (r["EXPECTED_COST"], r["OPTION"] != OPTION_KEEP, r["NETPR"],
                                 r["LIFNR"]))
    if not rows:
        return rows
    lowest = rows[0]["EXPECTED_COST"]
    new = [i for i, r in enumerate(rows) if r["OPTION"] == OPTION_NEW_PO]
    cheapest = min(new, key=lambda i: cheapest_key(rows[i]), default=None)
    fastest = min((i for i in new if can_be_fastest(rows[i])),
                  key=lambda i: fastest_key(rows[i]), default=None)
    return [
        {"RANK": i + 1, **row,
         "SCORE": round(100.0 * lowest / row["EXPECTED_COST"], 1),
         "IS_CHEAPEST": i == cheapest,
         "IS_FASTEST": i == fastest}
        for i, row in enumerate(rows)
    ]


def is_near_tie(expected_cost: int, best_expected_cost: int, pct: float = NEAR_TIE_PCT) -> bool:
    """True when `expected_cost` is less than `pct` percent above the lowest expected cost."""
    return (expected_cost - best_expected_cost) * 100.0 < pct * best_expected_cost


def _direction(amount: int, less: str, more: str, same: str) -> dict[str, Any]:
    """A difference as a positive amount plus the word that says which way it goes."""
    if amount == 0:
        return {"DIRECTION": same, "AMOUNT": 0}
    return {"DIRECTION": less if amount < 0 else more, "AMOUNT": abs(amount)}


def main_driver(purchase_gap: int, risk_gap: int) -> str | None:
    """Which part makes a row cost more than #1: "RISK", "PURCHASE", or None (nothing extra).

    The larger extra amount wins; when one part is a saving, the other part is the
    one that outweighs it (FR-SEL-4: why #1 beats the cheapest and the fastest).
    """
    if max(purchase_gap, risk_gap) <= 0:
        return None
    return "RISK" if risk_gap >= purchase_gap else "PURCHASE"


def compare_to_best(row: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    """How `row` compares with the recommended row; no amount is ever negative (FR-SEL-4)."""
    gap = row["EXPECTED_COST"] - best["EXPECTED_COST"]   # >= 0, best has the lowest
    purchase_gap = row["PURCHASE_COST"] - best["PURCHASE_COST"]
    risk_gap = row["RISK_COST"] - best["RISK_COST"]
    return {
        "EXPECTED_COST_GAP": gap,
        "EXPECTED_COST_GAP_PCT": round(100.0 * gap / best["EXPECTED_COST"], 2),
        "NEAR_TIE": is_near_tie(row["EXPECTED_COST"], best["EXPECTED_COST"]),
        "MAIN_DRIVER": main_driver(purchase_gap, risk_gap),
        "PURCHASE": _direction(purchase_gap,
                               "purchase saving", "extra purchase cost", "same purchase cost"),
        "RISK": _direction(risk_gap,
                           "lower risk cost", "extra risk cost", "same risk cost"),
    }
