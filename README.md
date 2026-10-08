# GESIT synthetic procurement data (PIHPS-anchored)

Generates 24 months of S/4HANA-style purchasing history for one F&B raw
material (sugar), priced on real PIHPS (Bank Indonesia) wholesale prices.
Supplier behaviour (reliability, seasonality, capacity, a planted buyer bias)
is simulated from hidden parameters, so the same "world" can later replay
"what if we had picked supplier B" for the Monte Carlo evaluation.

## 1. Get the PIHPS data (on your own laptop)

Option A, website (simplest, always works):
1. Open https://www.bi.go.id/hargapangan and go to the price table (Tabel Harga)
   for market type **Pedagang Besar**.
2. Commodity **Gula Pasir**, report **Harian** (daily), whole country,
   period **01 Sep 2024 to 30 Sep 2026**.
3. Click the save button (Excel). If one file can't hold the full range,
   download several shorter ranges. Put every file in `data/pihps/`.

Option B, script:
```bash
python fetch_pihps.py --probe --start 2026-09-21 --end 2026-09-25
# compare the printed numbers with the website, note which id is Pedagang Besar
python fetch_pihps.py --start 2024-09-01 --end 2026-09-30 --price-type <that id>
```
The probe matters: from my side three different ids returned identical numbers,
so confirm the id before trusting a scripted download.

Check what was loaded:
```bash
python pihps_loader.py data/pihps/*.xlsx --series "Gula Pasir Lokal"
```
If the Excel layout is not recognised, save a plain `date,price` CSV into
`data/pihps/` and it will be read instead.

## 2. Generate and validate

```bash
pip install pandas numpy openpyxl scikit-learn
python generate.py          # writes out/
python validate.py          # writes out/validation_report.md
```

Same seed + same PIHPS files = identical output.

## What comes out

| File | Who sees it | Content |
|---|---|---|
| `LFA1, MAKT, EKKO, EKPO, EKET, EKBE.csv` | agent | supplier master, material, PO header/item/schedule line, goods receipts (S/4 field names) |
| `market_price_daily.csv` | agent | the PIHPS series actually used (public data) |
| `quotes_asof.csv` | agent | today's quote, lead time and spare capacity from each active supplier |
| `inventory_asof.csv` | agent | stock, daily usage, safety stock, days of cover, downtime cost |
| `scenarios.json` | demo script | S1 delay notice, S2 price increase (size taken from the largest 30-day rise in PIHPS), S3 capacity shortfall |
| `rpt_po_lines.csv` | SAP-RPT / baselines | one row per PO line, features known at order time, label `LATE` / `DELAY_DAYS`, `SPLIT` = train / test / holdout_recent / predict |
| `_truth/*` | **never the agent** | hidden supplier parameters, buyer bias, realised outcome of open POs |

`SPLIT` notes: the test set ends 30 days before AS_OF because late POs from
the last weeks are still open, which would make the recent closed POs look
better than they are. `predict` rows are the open POs SAP-RPT should score.

## What is real and what is assumed

Real: the PIHPS price series (level, trend, Ramadan/Lebaran movements), Idul
Fitri dates (check against the official calendar), rainy-season months.

Assumed (all in `config.py`, state them on the assumptions slide): supplier
premiums and reliability, delay distribution, capacity, how buyers chose
suppliers before GESIT, the BUYER03 to supplier 100107 bias, stock level and
the Rp150 juta/day downtime cost. Supplier and buyer names are fictional.

Wholesale PIHPS is a market price, not a factory contract price. Large
factories buy refined sugar (GKR); this dataset models a mid-size producer
buying local GKP. If you only have retail data, set `PRICE_SCALE` to about
0.93 in `config.py` and say so.

## Using the world in the evaluation

```python
from world import World
w = World()
outcome = w.sample_delivery("100102", promised_date="2026-10-03", qty=8000)
price = w.quote_price("100102", "RM-GULA-001", "2026-09-30")
```
Run many samples per scenario for GESIT's pick, the cheapest supplier and the
fastest supplier, add `DOWNTIME_COST_PER_DAY` for every day the material runs
out, and compare total cost.

## Testing without internet

`tests/make_fake_pihps.py` writes an obviously FAKE file in PIHPS layout so
the pipeline can be tested offline:
```bash
python tests/make_fake_pihps.py
python generate.py --pihps tests/FAKE_pihps_export.xlsx --out test_out
```
Never use that file for the demo.
