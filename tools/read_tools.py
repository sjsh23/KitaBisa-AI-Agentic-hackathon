"""Read-only data tools (PRD section 8): get_open_pos, get_inventory, get_supplier_history.

All return JSON-ready dicts with SAP field names (D-2); amounts IDR, quantities KG, dates ISO.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from adapters import CsvAdapter, default_adapter
from scoring.reliability import posterior


def get_open_pos(matnr: str | None = None, adapter: CsvAdapter | None = None) -> list[dict[str, Any]]:
    """Open PO lines, optionally for one material."""
    adapter = adapter or default_adapter()
    return adapter.open_po_lines(matnr)


def get_inventory(matnr: str, adapter: CsvAdapter | None = None) -> dict[str, Any]:
    """Stock, daily usage, safety stock, days of cover and downtime cost for a material."""
    adapter = adapter or default_adapter()
    row = adapter.inventory(matnr)
    if row is None:
        raise KeyError(f"no inventory row for material {matnr}")
    return row


def get_supplier_history(lifnr: str, days: int = 180, today: str | None = None,
                         adapter: CsvAdapter | None = None) -> dict[str, Any]:
    """Receipts with delay in the last `days`, plus the reliability posterior (FR-EVA-3).

    The posterior uses every receipt of the supplier, oldest first; `days` only
    limits the receipts that are listed.
    """
    adapter = adapter or default_adapter()
    receipts = adapter.receipts(lifnr)
    if today is None:
        today = max((r["BUDAT"] for r in adapter.receipts()), default=date.today().isoformat())
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    return {
        "LIFNR": lifnr,
        "DAYS": days,
        "RECEIPTS": [r for r in receipts if r["BUDAT"] >= cutoff],
        "RELIABILITY": posterior(r["LATE"] for r in receipts).as_dict(),
    }
