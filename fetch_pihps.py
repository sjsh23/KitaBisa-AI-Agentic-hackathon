"""Download PIHPS (Bank Indonesia) daily prices for a date range.

Run this on your own laptop (it needs normal internet access):

    # 1) Find which price_type_id is "Pedagang Besar" (wholesale).
    #    Compare the printed sugar prices with the website for the same dates.
    python fetch_pihps.py --probe --start 2026-09-21 --end 2026-09-25

    # 2) Download the history with the id you confirmed (3 is a guess, verify it!)
    python fetch_pihps.py --start 2024-09-01 --end 2026-09-30 --price-type 3

Output: data/pihps/raw/*.json (one file per month, as served) and
        data/pihps/pihps_long.csv (date, name, level, price).

The endpoint is the same one the PIHPS website calls when you open a price
table. It needs no key. Be polite: the script waits between requests.
If it stops working, use the website's Excel export instead; pihps_loader.py
reads that file too.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE = "https://www.bi.go.id/hargapangan/WebSite/TabelHarga/GetGridDataDaerah"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (GESIT hackathon research script)",
    "Referer": "https://www.bi.go.id/hargapangan/TabelHarga/PasarTradisionalDaerah",
    "Accept": "application/json",
}
WATCH = ("Gula Pasir", "Minyak Goreng")


def fetch(price_type: int, start: date, end: date, province: str | None = None,
          report_type: int = 1, retries: int = 3) -> dict:
    params = {
        "price_type_id": str(price_type),
        "tipe_laporan": str(report_type),  # 1 = harian (daily)
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    if province:
        params["province_id"] = province
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt}/{retries}: {e}")
            time.sleep(2 * attempt)
    return {"data": []}


def month_chunks(start: date, end: date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - timedelta(days=1))
        cur = nxt


def date_columns(rec: dict) -> list[str]:
    return [k for k in rec if k.count("/") == 2]


def probe(start: date, end: date):
    print(f"Probing price types for {start}..{end}. Match each block to the website's\n"
          f"'Pasar Tradisional', 'Pasar Modern' and 'Pedagang Besar' tables.\n")
    seen = {}
    for pt in (1, 2, 3, 4):
        data = fetch(pt, start, end).get("data", [])
        rows = [r for r in data if str(r.get("name", "")).startswith(WATCH)]
        print(f"price_type_id={pt}: {len(data)} rows")
        if rows:
            cols = date_columns(rows[0])
            print(f"   dates returned: {cols[0] if cols else '-'} .. {cols[-1] if cols else '-'}")
            for r in rows:
                print(f"   {r.get('name', ''):35s} " + "  ".join(str(r.get(c)) for c in cols))
            seen[pt] = json.dumps(rows, sort_keys=True)
        time.sleep(1)
    if len(set(seen.values())) < len(seen):
        print("\nWARNING: some price types returned identical numbers. The server may be "
              "ignoring a parameter. Trust the website's Excel export instead.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--price-type", type=int, default=3,
                    help="PIHPS market type id. Verify with --probe first.")
    ap.add_argument("--province", default=None, help="BI province id; empty = national")
    ap.add_argument("--out-dir", default="data/pihps")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    if a.probe:
        probe(a.start, a.end)
        return

    out = Path(a.out_dir)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    long_rows = []
    for s, e in month_chunks(a.start, a.end):
        payload = fetch(a.price_type, s, e, a.province)
        tag = f"pt{a.price_type}_{a.province or 'nasional'}_{s:%Y-%m}"
        (out / "raw" / f"{tag}.json").write_text(json.dumps(payload), encoding="utf-8")
        data = payload.get("data", [])
        n_dates = 0
        for rec in data:
            cols = date_columns(rec)
            n_dates = max(n_dates, len(cols))
            for c in cols:
                d, m, y = c.split("/")
                long_rows.append({"date": f"{y}-{m}-{d}", "name": rec.get("name"),
                                  "level": rec.get("level"), "price": rec.get(c)})
        print(f"{s:%Y-%m}: {len(data)} rows x {n_dates} dates")
        time.sleep(1)

    with open(out / "pihps_long.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "name", "level", "price"])
        w.writeheader()
        w.writerows(long_rows)
    print(f"wrote {out / 'pihps_long.csv'} ({len(long_rows)} values)")
    print("Check it:  python pihps_loader.py data/pihps/pihps_long.csv --series 'Gula Pasir Lokal'")


if __name__ == "__main__":
    main()
