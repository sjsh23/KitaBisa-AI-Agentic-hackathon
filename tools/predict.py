"""predict_delay_risk (PRD section 8): P(late) per PO row from SAP-RPT (FR-DET-1).

When SAP-RPT is unavailable the tool falls back to each supplier's reliability
posterior and says so in the output (FR-DET-4).

Reliability blend (team decision, 20 Oct 2026, proposed for the PRD): with the switch
GESIT_RELIABILITY_BLEND on (the default), each row also gets P_RPT (SAP-RPT), P_POST (the
supplier's reliability posterior from its goods receipts before `today`, FR-EVA-2) and
P_USED = `scoring.reliability.blend` of the two, or P_POST alone when SAP-RPT is
unavailable. P_LATE is then P_USED, so Detection and Supplier Evaluation use it without
any change to their formulas. With the switch off the tool behaves exactly as before.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from adapters import CsvAdapter, default_adapter
from integrations.sap.rpt_client import RptClient, RptUnavailable
from scoring.reliability import BLEND_WEIGHT_RPT, blend, posterior, posterior_before

# Labelled history sent to SAP-RPT as context. `holdout_recent` is left out: late
# POs from those weeks are still open, so that set looks better than reality.
CONTEXT_SPLITS = ("train", "test")
FALLBACK_MODEL_ID = "beta-posterior-fallback"
SOURCE_BLEND = "SAP-RPT+POSTERIOR"
SOURCE_POSTERIOR = "POSTERIOR"


def blend_enabled(env: Mapping[str, str] | None = None) -> bool:
    """GESIT_RELIABILITY_BLEND: on unless set to 0, false, no or off."""
    env = os.environ if env is None else env
    return env.get("GESIT_RELIABILITY_BLEND", "").strip().lower() not in ("0", "false", "no", "off")


def predict_delay_risk(rows: list[dict[str, Any]], adapter: CsvAdapter | None = None,
                       client: RptClient | None = None, today: str | None = None,
                       use_blend: bool | None = None) -> dict[str, Any]:
    """P(late) for each row (rpt_po_lines schema), with the model id and a fallback flag.

    `today` is the scenario date: the posterior uses goods receipts before it only
    (default: the inventory date of the rows' material). `use_blend` overrides the switch.
    """
    adapter = adapter or default_adapter()
    client = client or RptClient()
    use_blend = blend_enabled() if use_blend is None else use_blend
    if not rows:
        return {"predictions": [], "model_id": client.model_id, "fallback": False,
                "fallback_reason": None, "retries": [], "blend": use_blend}

    retries = getattr(client, "retries", [])   # stand-in clients may not keep a list
    retries_before = len(retries)
    try:
        p_rpt: dict[str, float] | None = client.predict_late(adapter.rpt_rows(CONTEXT_SPLITS), rows)
        model_id, fallback, reason = client.model_id, False, None
    except RptUnavailable as exc:
        p_rpt = None
        model_id, fallback, reason = FALLBACK_MODEL_ID, True, str(exc)

    if not use_blend:   # the behaviour before the blend, unchanged
        p_late = p_rpt if p_rpt is not None else _posterior_p_late(rows, adapter)
        predictions = [{"EBELN": str(r["EBELN"]), "EBELP": r.get("EBELP"), "LIFNR": str(r["LIFNR"]),
                        "P_LATE": round(p_late[str(r["EBELN"])], 4)} for r in rows]
    else:
        today = today or _default_today(rows, adapter)
        p_post = _posterior_before_today(rows, adapter, today)
        predictions = []
        for r in rows:
            ebeln, lifnr = str(r["EBELN"]), str(r["LIFNR"])
            rpt = None if p_rpt is None else p_rpt[ebeln]
            used = blend(rpt, p_post[lifnr])
            predictions.append({
                "EBELN": ebeln, "EBELP": r.get("EBELP"), "LIFNR": lifnr,
                "P_LATE": round(used, 4),
                "P_RPT": None if rpt is None else round(rpt, 4),
                "P_POST": round(p_post[lifnr], 4), "P_USED": round(used, 4),
                "SOURCE": SOURCE_POSTERIOR if rpt is None else SOURCE_BLEND,
            })

    out = {
        "predictions": predictions,
        "model_id": model_id,
        "fallback": fallback,
        "fallback_reason": reason,
        "retries": retries[retries_before:],   # network retries in this call (NFR-6)
        "blend": use_blend,
    }
    if use_blend:
        out.update({"blend_weight_rpt": BLEND_WEIGHT_RPT, "posterior_before": today})
    return out


def _posterior_p_late(rows: list[dict[str, Any]], adapter: CsvAdapter) -> dict[str, float]:
    """FR-DET-4: posterior mean of P(late) per row, from the supplier's receipts."""
    by_supplier: dict[str, float] = {}
    result = {}
    for row in rows:
        lifnr = str(row["LIFNR"])
        if lifnr not in by_supplier:
            by_supplier[lifnr] = posterior(r["LATE"] for r in adapter.receipts(lifnr)).p_late
        result[str(row["EBELN"])] = by_supplier[lifnr]
    return result


def _posterior_before_today(rows: list[dict[str, Any]], adapter: CsvAdapter,
                            today: str) -> dict[str, float]:
    """FR-EVA-2: posterior mean of P(late) per supplier, from its receipts before `today`."""
    return {lifnr: posterior_before(adapter.receipts(lifnr), today).p_late
            for lifnr in sorted({str(r["LIFNR"]) for r in rows})}


def _default_today(rows: list[dict[str, Any]], adapter: CsvAdapter) -> str:
    """The scenario date: the inventory date of the rows' material (`get_inventory`)."""
    matnr = rows[0].get("MATNR")
    inventory = adapter.inventory(matnr) if matnr else None
    if inventory is None:
        raise ValueError("predict_delay_risk needs `today` when the rows carry no known MATNR")
    return inventory["DATE"]
