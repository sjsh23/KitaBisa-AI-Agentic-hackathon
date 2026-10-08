"""Load PIHPS (Bank Indonesia) price exports into a clean daily price series.

Accepted inputs (one file or many, e.g. monthly chunks):
  * .xlsx / .xls  : the Excel file from the PIHPS website ("Unduh"/save button)
  * .json         : raw grid JSON saved by fetch_pihps.py
  * .csv          : either a wide PIHPS-style table (dates as columns), the long
                    CSV written by fetch_pihps.py, or a plain 2-column file
                    with headers `date,price` (fallback if the layout changes).

The loader does not assume an exact layout. It looks for a header row with
several date-like cells, then for the row whose name matches the series you ask
for (e.g. "Gula Pasir Lokal"). Prices like "18,050", "18.050" or 18050 all work.
"""
from __future__ import annotations

import glob
import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ID_MONTHS = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2, "peb": 2, "mar": 3, "maret": 3,
    "apr": 4, "april": 4, "mei": 5, "may": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
    "agu": 8, "agt": 8, "ags": 8, "agustus": 8, "aug": 8, "sep": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "nov": 11, "november": 11, "nop": 11,
    "des": 12, "desember": 12, "dec": 12,
}


def parse_number(v) -> float | None:
    """'18,050' / '18.050' / '18050' / 18050.0 -> 18050.0 ; '-' or '' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if pd.isna(v) else float(v)
    s = str(v).strip().replace("Rp", "").replace(" ", "")
    if s in ("", "-", "--", "N/A", "nan"):
        return None
    # Thousands separators: PIHPS prices are whole rupiah, so a separator
    # followed by exactly three digits is a thousands separator.
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return float(re.sub(r"[.,]", "", s))
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_date_header(h) -> date | None:
    """Turn a column header into a date, or None if it is not a date."""
    if h is None:
        return None
    if isinstance(h, datetime):
        return h.date()
    if isinstance(h, date):
        return h
    if isinstance(h, pd.Timestamp):
        return h.date()
    s = str(h).strip().lower()
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)  # 28/09/2026
    if m:
        d, mo, y = map(int, m.groups())
        return _safe_date(y, mo, d)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t].*)?", s)  # 2026-09-28
    if m:
        y, mo, d = map(int, m.groups())
        return _safe_date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})", s)  # 28 Sep 2026 / 1 Oktober 2026
    if m and m.group(2) in ID_MONTHS:
        return _safe_date(int(m.group(3)), ID_MONTHS[m.group(2)], int(m.group(1)))
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", s)  # Okt 2026 (monthly report)
    if m and m.group(1) in ID_MONTHS:
        return _safe_date(int(m.group(2)), ID_MONTHS[m.group(1)], 1)
    return None


def _safe_date(y, mo, d):
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _series_from_rows(rows: list[list], series_name: str, source: str) -> pd.Series:
    """Find the header row (dates) and the row for `series_name` in a grid."""
    header_idx, date_cols = None, {}
    for i, row in enumerate(rows[:50]):
        cols = {j: parse_date_header(c) for j, c in enumerate(row)}
        cols = {j: d for j, d in cols.items() if d is not None}
        if len(cols) >= 2:
            header_idx, date_cols = i, cols
            break
    if header_idx is None:
        raise ValueError(f"{source}: no header row with dates found")

    target = _norm(series_name)
    exact, partial = None, None
    for row in rows[header_idx + 1:]:
        names = [_norm(c) for c in row if isinstance(c, str)]
        if target in names:
            exact = row
            break
        if partial is None and any(target in n for n in names):
            partial = row
    row = exact or partial
    if row is None:
        available = sorted({str(c).strip() for r in rows[header_idx + 1:] for c in r
                            if isinstance(c, str) and c.strip() and not parse_number(c)})
        raise ValueError(f"{source}: series '{series_name}' not found. "
                         f"Names seen: {available[:30]}")
    data = {d: parse_number(row[j]) for j, d in date_cols.items() if j < len(row)}
    s = pd.Series(data, dtype="float64").dropna()
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _rows_from_excel(path: Path) -> list[list[list]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]


def _rows_from_grid_json(path: Path) -> list[list]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not data:
        return []
    keys = list(data[0].keys())
    return [keys] + [[rec.get(k) for k in keys] for rec in data]


def load_one(path: str | Path, series_name: str) -> pd.Series:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        errors = []
        for rows in _rows_from_excel(path):
            try:
                return _series_from_rows(rows, series_name, str(path))
            except ValueError as e:
                errors.append(str(e))
        raise ValueError(" | ".join(errors))
    if ext == ".json":
        rows = _rows_from_grid_json(path)
        return _series_from_rows(rows, series_name, str(path)) if rows else pd.Series(dtype="float64")
    if ext == ".csv":
        df = pd.read_csv(path, dtype=str)
        cols = [c.lower() for c in df.columns]
        if {"date", "price"} <= set(cols) and "name" not in cols:  # plain 2-column fallback
            df.columns = cols
            s = pd.Series(df["price"].map(parse_number).values,
                          index=pd.to_datetime(df["date"], dayfirst=False))
            return s.dropna().sort_index()
        if {"date", "name", "price"} <= set(cols):  # long CSV from fetch_pihps.py
            df.columns = cols
            df = df[df["name"].map(_norm) == _norm(series_name)]
            s = pd.Series(df["price"].map(parse_number).values, index=pd.to_datetime(df["date"]))
            return s.dropna().sort_index()
        rows = [list(df.columns)] + df.values.tolist()
        return _series_from_rows(rows, series_name, str(path))
    raise ValueError(f"Unsupported file type: {path}")


def load_pihps(paths: str | list[str], series_name: str) -> pd.Series:
    """Load and merge one or many PIHPS files (globs allowed) for one series."""
    if isinstance(paths, str):
        paths = [paths]
    # An unmatched glob contributes nothing; a plain missing path is kept so it fails loudly.
    files = sorted({f for p in paths
                    for f in (glob.glob(p) or ([] if glob.has_magic(p) else [p]))})
    if not files:
        raise FileNotFoundError(f"No PIHPS files match {paths}")
    parts = [load_one(f, series_name) for f in files]
    s = pd.concat([p for p in parts if not p.empty])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty:
        raise ValueError(f"No prices found for '{series_name}' in {files}")
    s.name = series_name
    return s


def to_daily(s: pd.Series, start: str, end: str, max_edge_gap_days: int = 10) -> pd.Series:
    """Calendar-daily series over [start, end]. Weekends/holidays and monthly or
    weekly reports are forward-filled (prices hold until the next survey)."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if s.index.min() > start + pd.Timedelta(days=max_edge_gap_days) or \
       s.index.max() < end - pd.Timedelta(days=max_edge_gap_days):
        raise ValueError(
            f"PIHPS data covers {s.index.min().date()}..{s.index.max().date()}, "
            f"but the simulation needs {start.date()}..{end.date()}. "
            f"Download a longer range or change HISTORY_START / AS_OF in config.py.")
    idx = pd.date_range(min(start, s.index.min()), max(end, s.index.max()), freq="D")
    daily = s.reindex(idx).ffill().bfill()
    return daily.loc[start:end]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Quick check of a PIHPS export")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--series", default="Gula Pasir Lokal")
    a = ap.parse_args()
    s = load_pihps(a.files, a.series)
    print(f"{a.series}: {len(s)} observations, {s.index.min().date()} .. {s.index.max().date()}")
    print(s.describe().round(0).to_string())
    print(s.resample("MS").mean().round(0).to_string())
