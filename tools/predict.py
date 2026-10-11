"""predict_delay_risk (PRD section 8): P(late) per PO row from SAP-RPT (FR-DET-1).

When SAP-RPT is unavailable the tool falls back to each supplier's reliability
posterior and says so in the output (FR-DET-4).
"""
from __future__ import annotations

from typing import Any

from adapters import CsvAdapter, default_adapter
from integrations.sap.rpt_client import RptClient, RptUnavailable
from scoring.reliability import posterior

# Labelled history sent to SAP-RPT as context. `holdout_recent` is left out: late
# POs from those weeks are still open, so that set looks better than reality.
CONTEXT_SPLITS = ("train", "test")
FALLBACK_MODEL_ID = "beta-posterior-fallback"


def predict_delay_risk(rows: list[dict[str, Any]], adapter: CsvAdapter | None = None,
                       client: RptClient | None = None) -> dict[str, Any]:
    """P(late) for each row (rpt_po_lines schema), with the model id and a fallback flag."""
    adapter = adapter or default_adapter()
    client = client or RptClient()
    if not rows:
        return {"predictions": [], "model_id": client.model_id, "fallback": False,
                "fallback_reason": None}

    try:
        p_late = client.predict_late(adapter.rpt_rows(CONTEXT_SPLITS), rows)
        model_id, fallback, reason = client.model_id, False, None
    except RptUnavailable as exc:
        p_late = _posterior_p_late(rows, adapter)
        model_id, fallback, reason = FALLBACK_MODEL_ID, True, str(exc)

    return {
        "predictions": [
            {"EBELN": str(r["EBELN"]), "EBELP": r.get("EBELP"), "LIFNR": str(r["LIFNR"]),
             "P_LATE": round(p_late[str(r["EBELN"])], 4)}
            for r in rows
        ],
        "model_id": model_id,
        "fallback": fallback,
        "fallback_reason": reason,
    }


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
