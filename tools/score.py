"""score_suppliers (PRD section 8): ranked backup suppliers by expected cost (FR-SEL-1 to FR-SEL-3).

Gathers the inputs (quotes, approved panel, receipts, market price), asks
`predict_delay_risk` for P(late) of a hypothetical PO per candidate (SAP-RPT,
posterior fallback per FR-DET-4) and hands everything to `scoring.supplier_score`.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from adapters import CsvAdapter, default_adapter
from integrations.sap.rpt_client import RptClient
from scoring import rpt_features, supplier_score
from scoring.reliability import BLEND_RULE_TEXT, posterior_before
from tools.predict import predict_delay_risk
from tools.read_tools import get_inventory, get_market_price, get_quotes

CANDIDATE_ROW_PREFIX = "CAND-"   # EBELN of a hypothetical PO row, never a real PO number


def score_suppliers(matnr: str, qty: float, need_by: str, exclude_lifnr: Iterable[str] = (),
                    today: str | None = None, reserved_kg: Mapping[str, float] | None = None,
                    adapter: CsvAdapter | None = None,
                    client: RptClient | None = None,
                    current_po: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Ranked table for buying `qty` kg of `matnr` that must arrive by `need_by` (stock-out date).

    `exclude_lifnr` are suppliers with a flagged PO right now; they are not used as backups.
    `reserved_kg` is spare capacity already promised to an earlier replacement in the same
    run; it is taken off AVAILABLE_KG_NEXT_30D.
    `current_po` (EBELN, LIFNR, NETPR, ARRIVAL, P_LATE) adds "keep the current PO" as a
    ranked option (proposed, docs/prd_change_proposals.md item 4). P_LATE None falls
    back to the supplier's reliability posterior (FR-DET-4).
    """
    adapter = adapter or default_adapter()
    inventory = get_inventory(matnr, adapter=adapter)
    today = today or inventory["DATE"]
    downtime = int(inventory["DOWNTIME_COST_PER_DAY"])
    exclude = {str(lifnr) for lifnr in exclude_lifnr}
    suppliers = adapter.suppliers()

    candidates: dict[str, dict[str, Any]] = {}
    excluded = []
    reserved = reserved_kg or {}
    for quote in get_quotes(matnr, adapter=adapter):                       # FR-SEL-1
        if reserved.get(quote["LIFNR"]):
            quote = {**quote, "AVAILABLE_KG_NEXT_30D":
                     max(0.0, quote["AVAILABLE_KG_NEXT_30D"] - reserved[quote["LIFNR"]])}
        reason = supplier_score.exclusion_reason(quote, suppliers.get(quote["LIFNR"]), today,
                                                 exclude)
        if reason is None:
            candidates[quote["LIFNR"]] = quote
        else:
            excluded.append({"LIFNR": quote["LIFNR"], "NETPR": int(quote["NETPR"]),
                             "LEAD_DAYS": quote["LEAD_DAYS"],
                             "AVAILABLE_KG_NEXT_30D": quote["AVAILABLE_KG_NEXT_30D"],
                             "REASON": reason})

    market = get_market_price(matnr, today, adapter=adapter)
    po_lines = adapter.po_lines()
    receipts = {lifnr: adapter.receipts(lifnr) for lifnr in candidates}
    rows = [
        rpt_features.hypothetical_row(
            f"{CANDIDATE_ROW_PREFIX}{lifnr}", quote, min(qty, quote["AVAILABLE_KG_NEXT_30D"]),
            today, market["PRICE_IDR_KG"], receipts[lifnr],
            rpt_features.recent_ordered_kg(po_lines, lifnr, today))
        for lifnr, quote in candidates.items()
    ]
    prediction = predict_delay_risk(rows, adapter=adapter, client=client, today=today)
    p_late = {p["LIFNR"]: p["P_LATE"] for p in prediction["predictions"]}   # P_USED if blended
    sources = {p["LIFNR"]: {k: p[k] for k in ("P_RPT", "P_POST", "P_USED")}
               for p in prediction["predictions"] if "P_USED" in p}
    keep = None
    if current_po is not None:
        lifnr = str(current_po["LIFNR"])
        receipts.setdefault(lifnr, adapter.receipts(lifnr))
        own = current_po.get("P_LATE")
        current = {**current_po, "P_LATE": own if own is not None
                   else posterior_before(receipts[lifnr], today).p_late}   # FR-EVA-2
        keep_d_late = delay_when_late({lifnr: receipts[lifnr]}, adapter)[lifnr]
        keep = supplier_score.keep_row(current, qty, keep_d_late, today, need_by, downtime)
    d_late = delay_when_late({k: receipts[k] for k in candidates}, adapter)

    ranked = supplier_score.score_candidates(candidates, qty, p_late, d_late, today, need_by,
                                             downtime, keep=keep)          # FR-SEL-2, FR-SEL-3
    for row in ranked:   # track record behind P_LATE and D_LATE, so a thin one is visible
        row["N_RECEIPTS"] = len(receipts[row["LIFNR"]])
    # Candidates with no row: they cannot cover the quantity, not even as a split (reported
    # so a case without any new order can say why; nothing is scored differently).
    ranked_lifnrs = {r["LIFNR"] for r in ranked if r["OPTION"] == supplier_score.OPTION_NEW_PO}
    not_ranked = sorted(set(candidates) - ranked_lifnrs)
    return {
        "MATNR": matnr,
        "QTY": float(qty),
        "TODAY": today,
        "NEED_BY": need_by,
        "RESERVED_KG": {k: v for k, v in sorted(reserved.items()) if v},
        "DOWNTIME_COST_PER_DAY": downtime,
        "MARKET_PRICE": {"DATE": market["DATE"], "PRICE_IDR_KG": int(market["PRICE_IDR_KG"])},
        "PREDICTION": {"MODEL_ID": prediction["model_id"], "FALLBACK": prediction["fallback"],
                       "FALLBACK_REASON": prediction["fallback_reason"], "ROWS": len(rows),
                       "RETRIES": prediction.get("retries", [])},
        # The reliability blend, outside PREDICTION so the model's facts do not change.
        "P_LATE_RULE": BLEND_RULE_TEXT if prediction.get("blend") else None,
        "P_LATE_SOURCES": sources,   # per candidate LIFNR: P_RPT, P_POST, P_USED (blend only)
        "CANDIDATES": ranked,
        "EXCLUDED": sorted(excluded, key=lambda e: e["LIFNR"]),
        "CANNOT_COVER": not_ranked,
        "RECOMMENDED": ranked[0]["LIFNR"] if ranked else None,
        "RECOMMENDED_OPTION": ranked[0]["OPTION"] if ranked else None,
        "KEEP": next((r for r in ranked if r["OPTION"] == supplier_score.OPTION_KEEP), None),
        "CHEAPEST": next((r["LIFNR"] for r in ranked if r["IS_CHEAPEST"]), None),
        "FASTEST": next((r["LIFNR"] for r in ranked if r["IS_FASTEST"]), None),
    }


def delay_when_late(receipts: dict[str, list[dict[str, Any]]],
                    adapter: CsvAdapter) -> dict[str, float]:
    """d_late per supplier, shrunk towards the all-supplier average delay when late."""
    pooled = supplier_score.mean_delay_when_late(r["DELAY_DAYS"] for r in adapter.receipts())
    return {lifnr: supplier_score.delay_when_late((r["DELAY_DAYS"] for r in rows), pooled or 0.0)
            for lifnr, rows in receipts.items()}
