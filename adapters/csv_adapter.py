"""Read the S/4-shaped CSV files in `data_gen/out/`.

D-1: this adapter never reads `_truth/`. D-2: SAP field names are kept as they
are (LIFNR, EBELN, BEDAT, EINDT, NETPR, MENGE, NETWR). Amounts are IDR
integers, quantities KG, dates ISO `YYYY-MM-DD` strings.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data_gen" / "out"

_INT_FIELDS = {
    "EBELP", "ETENR", "PEINH", "NETPR", "NETWR", "DMBTR", "DOWNTIME_COST_PER_DAY",
    "LEAD_DAYS", "ORDER_MONTH", "RAINY_SEASON", "LEBARAN_WINDOW",
    "SUP_N_RECEIPTS_ALL", "SUP_N_RECEIPTS_90D", "LATE", "DELAY_DAYS",
}
_FLOAT_FIELDS = {
    "MENGE", "WEMNG", "LABST", "DAILY_USAGE_KG", "EISBE", "DAYS_OF_COVER", "OPEN_PO_QTY",
    "PRICE_VS_MARKET", "SUPPLIER_UTIL_30D", "SUP_LATE_RATE_90D", "SUP_AVG_DELAY_90D",
    "AVAILABLE_KG_NEXT_30D", "MINBM", "PRICE_IDR_KG",
}


def _convert(field: str, value: str) -> Any:
    """Typed value for one CSV cell; an empty cell becomes None."""
    if value == "":
        return None
    if field in _INT_FIELDS:
        return int(round(float(value)))
    if field in _FLOAT_FIELDS:
        return float(value)
    return value


class CsvAdapter:
    """Data access over one dataset folder. Swap this class to connect to real S/4 (D-2)."""

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        path = Path(data_dir or os.environ.get("GESIT_DATA_DIR") or DEFAULT_DATA_DIR).resolve()
        if "_truth" in path.parts:
            raise ValueError("D-1: adapters must never read data_gen/out/_truth/")
        self.data_dir = path

    # ------------------------------------------------------------- raw tables
    def table(self, name: str) -> list[dict[str, Any]]:
        """All rows of one CSV table (for example `EKKO`), typed."""
        if "_truth" in name or "/" in name or "\\" in name:
            raise ValueError("D-1: adapters must never read data_gen/out/_truth/")
        return _read_csv(str(self.data_dir / f"{name}.csv"))

    def scenarios(self) -> list[dict[str, Any]]:
        """Scripted supplier notices S1 to S3 (PRD section 6)."""
        with open(self.data_dir / "scenarios.json", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------ PO queries
    def po_lines(self) -> list[dict[str, Any]]:
        """Every PO line: EKKO header, EKPO item and EKET schedule joined on EBELN."""
        headers = {r["EBELN"]: r for r in self.table("EKKO")}
        schedules = {(r["EBELN"], r["EBELP"]): r for r in self.table("EKET")}
        lines = []
        for item in self.table("EKPO"):
            header = headers[item["EBELN"]]
            schedule = schedules[(item["EBELN"], item["EBELP"])]
            lines.append({
                "EBELN": item["EBELN"], "EBELP": item["EBELP"],
                "LIFNR": header["LIFNR"], "ERNAM": header["ERNAM"], "BEDAT": header["BEDAT"],
                "MATNR": item["MATNR"], "MENGE": item["MENGE"], "MEINS": item["MEINS"],
                "NETPR": item["NETPR"], "NETWR": item["NETWR"], "WAERS": header["WAERS"],
                "EINDT": schedule["EINDT"], "WEMNG": schedule["WEMNG"],
                "ELIKZ": item["ELIKZ"] or "",
            })
        return lines

    def open_po_lines(self, matnr: str | None = None) -> list[dict[str, Any]]:
        """PO lines without the delivery-completed flag (EKPO-ELIKZ blank)."""
        return [
            line for line in self.po_lines()
            if line["ELIKZ"] != "X" and (matnr is None or line["MATNR"] == matnr)
        ]

    def receipts(self, lifnr: str | None = None) -> list[dict[str, Any]]:
        """Goods receipts (EKBE, VGABE 1) with the promised date, oldest first.

        DELAY_DAYS = BUDAT minus EINDT; LATE = 1 when DELAY_DAYS > 0.
        """
        lines = {(line["EBELN"], line["EBELP"]): line for line in self.po_lines()}
        out = []
        for gr in self.table("EKBE"):
            if gr["VGABE"] != "1":
                continue
            line = lines[(gr["EBELN"], gr["EBELP"])]
            if lifnr is not None and line["LIFNR"] != lifnr:
                continue
            delay = (date.fromisoformat(gr["BUDAT"]) - date.fromisoformat(line["EINDT"])).days
            out.append({
                "EBELN": gr["EBELN"], "EBELP": gr["EBELP"], "LIFNR": line["LIFNR"],
                "EINDT": line["EINDT"], "BUDAT": gr["BUDAT"], "MENGE": gr["MENGE"],
                "DELAY_DAYS": delay, "LATE": 1 if delay > 0 else 0,
            })
        return sorted(out, key=lambda r: (r["BUDAT"], r["EBELN"]))

    # --------------------------------------------------------- other lookups
    def inventory(self, matnr: str) -> dict[str, Any] | None:
        """Stock position row for a material, or None."""
        return next((r for r in self.table("inventory_asof") if r["MATNR"] == matnr), None)

    def quotes(self, matnr: str) -> list[dict[str, Any]]:
        """Current supplier quotes for a material (quotes_asof), every supplier incl. off-panel."""
        return [r for r in self.table("quotes_asof") if r["MATNR"] == matnr]

    def market_price(self, matnr: str, day: str) -> dict[str, Any] | None:
        """PIHPS anchor row for a material on `day`, or the last one before it; None if none."""
        rows = [r for r in self.table("market_price_daily")
                if r["MATNR"] == matnr and r["DATE"] <= day]
        return max(rows, key=lambda r: r["DATE"], default=None)

    def suppliers(self) -> dict[str, dict[str, Any]]:
        """LFA1 keyed by LIFNR."""
        return {r["LIFNR"]: r for r in self.table("LFA1")}

    def rpt_rows(self, splits: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Rows of `rpt_po_lines` (the `predict_delay_risk` schema), optionally by SPLIT."""
        rows = self.table("rpt_po_lines")
        return [r for r in rows if splits is None or r["SPLIT"] in splits]


@lru_cache(maxsize=32)
def _read_csv_cached(path: str, mtime: float) -> tuple[dict[str, Any], ...]:
    with open(path, newline="", encoding="utf-8") as fh:
        return tuple({k: _convert(k, v) for k, v in row.items()} for row in csv.DictReader(fh))


def _read_csv(path: str) -> list[dict[str, Any]]:
    """Read one CSV; cached per file version, returned as fresh dicts."""
    return [dict(row) for row in _read_csv_cached(path, os.path.getmtime(path))]


def default_adapter() -> CsvAdapter:
    """Adapter on `GESIT_DATA_DIR`, or on `data_gen/out/` when it is not set."""
    return CsvAdapter()
