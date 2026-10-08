# GESIT data dictionary

Every CSV file in this repository: what it is, who may read it, and what each
column means. Requirements live in [PRD.md](../PRD.md) (section 6); how to
regenerate the files is in [data_gen/README.md](../data_gen/README.md).

All procurement data here is **simulated** (seed 42, generated 8 Oct 2026).
Only the PIHPS price series is real. Supplier and buyer names are fictional.

## Conventions

- **Field names:** SAP S/4HANA names are kept as they are (rule D-2). Columns that
  do not exist in SAP are marked "GESIT helper" below.
- **Amounts:** IDR. **Quantities:** KG. **Dates:** ISO `YYYY-MM-DD`.
- **Number format:** the generator writes amounts and quantities as floats
  (`16600.0`). They are always whole numbers, so adapters should cast them to integers.
- **AS_OF:** 2026-09-30 is "today" for the demo. A PO whose goods receipt falls
  after AS_OF is open.
- **Keys:** read `EBELN` and `LIFNR` as strings, not numbers.
- **Late:** a PO is late when the goods receipt date is after the promised date (`BUDAT > EINDT`).

## Overview

| File | Rows | Key | Agent may read | Content |
|---|---|---|---|---|
| `data_gen/out/LFA1.csv` | 11 | LIFNR | yes | Supplier master |
| `data_gen/out/MAKT.csv` | 1 | MATNR | yes | Material description |
| `data_gen/out/EKKO.csv` | 434 | EBELN | yes | PO header |
| `data_gen/out/EKPO.csv` | 434 | EBELN, EBELP | yes | PO item |
| `data_gen/out/EKET.csv` | 434 | EBELN, EBELP, ETENR | yes | PO schedule line (promised date) |
| `data_gen/out/EKBE.csv` | 429 | EBELN, EBELP | yes | Goods receipts |
| `data_gen/out/market_price_daily.csv` | 735 | MATNR, DATE | yes | Daily market price used as anchor |
| `data_gen/out/quotes_asof.csv` | 11 | MATNR, LIFNR | yes | Current supplier quotes |
| `data_gen/out/inventory_asof.csv` | 1 | MATNR, WERKS | yes | Stock position at AS_OF |
| `data_gen/out/rpt_po_lines.csv` | 434 | EBELN, EBELP | yes | Feature table for SAP-RPT and baselines |
| `data_gen/out/_truth/suppliers_truth.csv` | 11 | lifnr | **no (D-1)** | Hidden supplier parameters |
| `data_gen/out/_truth/buyers_truth.csv` | 4 | ERNAM | **no (D-1)** | Hidden buyer bias |
| `data_gen/out/_truth/open_po_outcomes.csv` | 5 | EBELN | **no (D-1)** | What will happen to the open POs |
| `data_gen/data/pihps/pihps_long.csv` | 16,833 | date, name | generator input | Raw PIHPS download (real data) |

How the PO tables join: `EKKO.EBELN` = `EKPO.EBELN` = `EKET.EBELN` = `EKBE.EBELN`.
Every PO has exactly one item (`EBELP` = 10) and one schedule line (`ETENR` = 1).
A closed PO has exactly one row in EKBE; an open PO has none.
`EKKO.LIFNR` joins to `LFA1.LIFNR`, and `EKPO.MATNR` joins to `MAKT.MATNR`.

---

## S/4HANA-shaped tables

### LFA1.csv: supplier master

One row per supplier. Ten suppliers (100101 to 100110) are on the approved
panel and have PO history. Supplier 100111 is off the panel: it has a quote
but no POs, and exists to demonstrate policy POL-2.

| Column | Type | Meaning |
|---|---|---|
| LIFNR | string | Supplier number, 100101 to 100111. |
| NAME1 | string | Supplier name (fictional). |
| ORT01 | string | City. |
| REGIO | string | Province. |
| LAND1 | string | Country, always `ID`. |
| ERDAT | date | Date the supplier record was created. `2020-01-01` for established suppliers; `2026-06-01` for the new supplier 100109, which has no POs before that date. |
| ZZPANEL | string | Custom field (not standard SAP). `X` = on the approved panel, blank = off the panel. Used by FR-SEL-1 and POL-2. |

### MAKT.csv: material description

One row, because the demo uses one material.

| Column | Type | Meaning |
|---|---|---|
| MATNR | string | Material number, `RM-GULA-001`. |
| SPRAS | string | Language of the description, `EN`. |
| MAKTX | string | Description: "Gula Pasir Lokal (GKP), karung 50 kg" (local white sugar, 50 kg sacks). |
| MEINS | string | Base unit of measure, `KG`. In real SAP this field is in MARA; it is kept here to avoid a one-row extra table. |
| PIHPS_SERIES | string | GESIT helper. Name of the PIHPS price series this material is priced on, "Gula Pasir Lokal". |

### EKKO.csv: purchase order header

One row per PO. This is the table the Integrity Guard reads to compare buyers (FR-INT-3).

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number, 4500000001 to 4500000434, in order date order. |
| BUKRS | string | Company code, always `1000`. |
| BSTYP | string | Document category, always `F` (purchase order). |
| BSART | string | Document type, always `NB` (standard PO). |
| LIFNR | string | Supplier the PO was placed with. |
| EKORG | string | Purchasing organisation, always `1000`. |
| EKGRP | string | Purchasing group, always `P01` (raw materials). |
| BEDAT | date | PO date (order date). 2024-10-01 to 2026-09-30, never a Sunday. |
| ERNAM | string | Buyer who created the PO: `BUYER01` to `BUYER04`. |
| WAERS | string | Currency, always `IDR`. |

### EKPO.csv: purchase order item

One row per PO (item 10).

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number. |
| EBELP | integer | Item number, always 10. |
| MATNR | string | Material number. |
| TXZ01 | string | Short text, copy of the material description. |
| WERKS | string | Plant, always `1100` (fictional Cikarang factory). |
| MENGE | KG | Ordered quantity. Routine POs are 3,000 to 8,000 kg in steps of 500; the three scenario POs are 8,000 to 12,000 kg. |
| MEINS | string | Order unit, always `KG`. |
| NETPR | IDR per KG | Net price per unit, rounded to Rp50. This is the supplier's quote on the order date. |
| PEINH | integer | Price unit, always 1 (NETPR is per 1 KG). |
| NETWR | IDR | Net order value = MENGE x NETPR. This is the amount compared with the Rp150,000,000 approval threshold (FR-ACT-2, POL-1). |
| ELIKZ | string | Delivery completed indicator. `X` = goods received (closed PO), blank = open PO. Five POs are open. |

### EKET.csv: schedule line

One row per PO. Holds the delivery date the supplier promised.

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number. |
| EBELP | integer | Item number, always 10. |
| ETENR | integer | Schedule line number, always 1. |
| EINDT | date | Promised delivery date = BEDAT + the supplier's quoted lead time. |
| MENGE | KG | Scheduled quantity, same as EKPO.MENGE. |
| WEMNG | KG | Quantity received so far. Equals MENGE for a full delivery, less for a short shipment (24 closed POs), and 0 for the five open POs. |

### EKBE.csv: purchase order history (goods receipts)

One row per closed PO. Open POs have no row. Compare `BUDAT` with `EKET.EINDT`
to get the delay; this is the input to `record_outcome` and the reliability
posterior (FR-EVA-1, FR-EVA-2).

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number. |
| EBELP | integer | Item number, always 10. |
| VGABE | string | Transaction type, always `1` (goods receipt). |
| BEWTP | string | PO history category, always `E` (goods receipt). |
| BWART | string | Movement type, always `101` (goods receipt for a PO). |
| BUDAT | date | Posting date of the goods receipt, that is the day the goods arrived. Can be one day before EINDT (early delivery). |
| MENGE | KG | Quantity received. Less than the ordered quantity for a short shipment. |
| DMBTR | IDR | Value received = received quantity x NETPR. |
| WAERS | string | Currency, always `IDR`. |

---

## Supporting tables (GESIT helpers, not SAP tables)

### market_price_daily.csv: price anchor

The PIHPS wholesale price for the material, one row per calendar day from
2024-09-26 to 2026-09-30. This is public data, so the agent may use it
(tool `get_market_price`).

| Column | Type | Meaning |
|---|---|---|
| MATNR | string | Material number. |
| DATE | date | Calendar day. |
| PRICE_IDR_KG | IDR per KG | PIHPS price of "Gula Pasir Lokal", Pedagang Besar, national average. Range Rp16,250 to Rp17,600. PIHPS has no values on weekends and holidays; those days repeat the last published price (forward fill). |
| SOURCE | string | Provenance note: source, market type, scale factor (`x1.0` = price used as is) and the fill method. |

### quotes_asof.csv: current supplier quotes

What each supplier offers today (AS_OF). One row per supplier, including the
off-panel supplier 100111. This is the candidate list for supplier scoring
(FR-SEL-1, FR-SEL-2), after filtering on `LFA1.ZZPANEL`.

| Column | Type | Meaning |
|---|---|---|
| MATNR | string | Material number. |
| LIFNR | string | Supplier number. |
| QUOTE_DATE | date | Date of the quote, always 2026-09-30. |
| NETPR | IDR per KG | Quoted price, rounded to Rp50. |
| PEINH | integer | Price unit, always 1. |
| WAERS | string | Currency, always `IDR`. |
| LEAD_DAYS | days | Quoted lead time in calendar days. Arrival = today + LEAD_DAYS. |
| AVAILABLE_KG_NEXT_30D | KG | Spare capacity = the supplier's monthly capacity minus what was ordered from it in the last 30 days, never below 0. If the needed quantity is larger, the supplier cannot cover the full order and a split order is evaluated (FR-SEL-2). |
| VALID_TO | date | Last day the quote is valid, QUOTE_DATE + 7 days. |

### inventory_asof.csv: stock position

One row for the material at the plant, at AS_OF. Input to the stock-out date
(FR-DET-3) and to the downtime part of expected cost (FR-SEL-2).

| Column | Type | Meaning |
|---|---|---|
| MATNR | string | Material number. |
| WERKS | string | Plant, `1100`. |
| DATE | date | Stock date, 2026-09-30. |
| LABST | KG | Unrestricted stock on hand: 19,800 kg. |
| DAILY_USAGE_KG | KG per day | Production consumption: 3,300 kg per day (assumption). |
| EISBE | KG | Safety stock: 16,500 kg, which is 5 days of usage. |
| DAYS_OF_COVER | days | LABST / DAILY_USAGE_KG = 6.0. Stock-out date = today + DAYS_OF_COVER. |
| OPEN_PO_QTY | KG | Total quantity on open POs: 39,500 kg. |
| DOWNTIME_COST_PER_DAY | IDR per day | Cost of one day of stopped production: Rp150,000,000 (assumption, rule D-4). |

### rpt_po_lines.csv: feature table for delay prediction

One row per PO line with features that were known on the order date, plus the
outcome label. This is the input schema of `predict_delay_risk` (SAP-RPT) and
of the classical baselines (FR-DET-1, FR-EVA-5).

No leakage: every `SUP_*` feature uses only goods receipts posted before the
PO's order date.

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number. |
| EBELP | integer | Item number, always 10. |
| LIFNR | string | Supplier. |
| MATNR | string | Material. |
| BEDAT | date | Order date. |
| EINDT | date | Promised delivery date. |
| ORDER_MONTH | 1 to 12 | Month of the order date. |
| MENGE | KG | Ordered quantity. |
| LEAD_DAYS | days | EINDT minus BEDAT, the quoted lead time. |
| NETPR | IDR per KG | PO price. |
| PRICE_VS_MARKET | ratio | NETPR / market price on the order date. 1.03 means 3% above the PIHPS anchor. |
| RAINY_SEASON | 0 or 1 | 1 when the promised date falls in November to March. |
| LEBARAN_WINDOW | 0 or 1 | 1 when the promised date is between 10 days before and 7 days after Idul Fitri. |
| SUPPLIER_UTIL_30D | ratio | Quantity ordered from this supplier in the 30 days up to and including this PO, divided by its monthly capacity. Above 1 means the supplier is overbooked. Note: the capacity figure comes from the supplier parameters; in a real setting it would come from the contract. |
| SUP_N_RECEIPTS_ALL | count | Goods receipts from this supplier before the order date, all time. 0 means no track record yet. |
| SUP_N_RECEIPTS_90D | count | Same, in the 90 days before the order date. |
| SUP_LATE_RATE_90D | 0 to 1 | Share of those 90-day receipts that were late. Empty when there were none (19 rows). |
| SUP_AVG_DELAY_90D | days | Average delay of those 90-day receipts, counting on-time and early deliveries as 0. Empty when there were none. |
| LATE | 0 or 1 | **Label.** 1 when the goods arrived after EINDT. Empty for the five open POs. |
| DELAY_DAYS | days | **Label.** Goods receipt date minus EINDT. Range -1 (one day early) to 13. Empty for open POs. |
| SPLIT | string | Which set the row belongs to, see below. |

`SPLIT` values (time-based, so the model is never tested on the past):

| Value | Rows | Definition | Use |
|---|---|---|---|
| `train` | 340 | Closed POs ordered before 2026-04-30 | Context rows for SAP-RPT, training for baselines |
| `test` | 74 | Closed POs ordered from 2026-04-30 to 2026-08-31 | Benchmark (AUC, Brier) |
| `holdout_recent` | 15 | Closed POs ordered in the last 30 days | Not used for scoring models: late POs from these weeks are still open, so this set looks better than reality |
| `predict` | 5 | Open POs | The rows SAP-RPT scores in the demo |

When calling SAP-RPT, never send `LATE` or `DELAY_DAYS` for the rows being predicted.

---

## Hidden truth (`data_gen/out/_truth/`)

**Rule D-1:** agent code, tools and the UI must never read these files. Only
`eval/` may, for the Monte Carlo comparison (FR-EVA-4). They hold the answers
the agent is supposed to estimate.

### suppliers_truth.csv: hidden supplier parameters

One row per supplier, a dump of `SUPPLIERS` in `data_gen/config.py`. Column
names are lower case because they are generator parameters, not SAP fields.

| Column | Type | Meaning |
|---|---|---|
| lifnr | string | Supplier number. |
| name, city, province | string | Same as LFA1. |
| archetype | string | Plain description of the supplier's role in the story, for example "Cheapest but often late". |
| premium | ratio | Price level against the PIHPS anchor. -0.04 = 4% below market, 0.055 = 5.5% above. |
| price_sigma | ratio | Random noise on each quote (standard deviation, log scale). |
| lead_days | days | Quoted lead time. |
| on_time_p | 0 to 1 | Probability of on-time delivery in normal conditions. |
| mean_delay_days | days | Average delay when a delivery is late. |
| capacity_kg_month | KG | Monthly capacity. |
| rainy_penalty | 0 to 1 | Drop in on-time probability in the rainy season. |
| lebaran_penalty | 0 to 1 | Drop in on-time probability in the Lebaran window. |
| short_ship_p | 0 to 1 | Probability of delivering less than ordered. |
| active_from | date | First day the supplier can receive POs. Empty = active for the whole history. |
| drift | text | Change in reliability over time. Only 100108: on-time probability moves from 0.93 to 0.55 between 2026-03-01 and 2026-05-01. |
| on_panel | True or False | Approved panel membership, source of LFA1.ZZPANEL. |
| materials | text | Materials the supplier can deliver, comma separated. |

### buyers_truth.csv: hidden buyer bias

| Column | Type | Meaning |
|---|---|---|
| ERNAM | string | Buyer id, matches EKKO.ERNAM. |
| NAME | string | Buyer first name (fictional). |
| BIAS | JSON text | Extra preference per supplier when the buyer chose a supplier in the simulated history. `{}` = none. BUYER03 has `{"100107": 1.6}`, the planted pattern the Integrity Guard should find (FR-INT-3). |

### open_po_outcomes.csv: outcome of the open POs

One row per open PO (5 rows).

| Column | Type | Meaning |
|---|---|---|
| EBELN | string | PO number. |
| LIFNR, MATNR, BEDAT, EINDT | | Same as in the PO tables. |
| GR_DATE | date | When the goods will really arrive. Empty for the three scenario POs, whose outcome is scripted in `scenarios.json`. |
| DELAY_DAYS | days | Real delay against EINDT. Empty for scenario POs. |
| RECEIVED | KG | Quantity that will really arrive. Empty for scenario POs. |
| P_ON_TIME_TRUE | 0 to 1 | True on-time probability for this PO. |
| SCENARIO | string | `S1`, `S2`, `S3` for the scenario POs, empty for ordinary open POs. |

---

## Generator input (real data)

### data_gen/data/pihps/pihps_long.csv

The raw download from PIHPS (Bank Indonesia), market type Pedagang Besar
(`price_type_id=3`), national average, 2024-09-02 to 2026-09-30. It contains
all 31 commodity rows PIHPS publishes; the generator uses only "Gula Pasir Lokal".
Written by `fetch_pihps.py`, read by `pihps_loader.py`.

| Column | Type | Meaning |
|---|---|---|
| date | date | Trading day. Weekends and public holidays are missing. |
| name | string | Commodity name as shown on the PIHPS website, for example "Beras", "Gula Pasir", "Gula Pasir Lokal". |
| level | 1 or 2 | 1 = commodity group (for example "Gula Pasir"), 2 = a specific quality inside the group (for example "Gula Pasir Lokal"). |
| price | text | Price in IDR per kg with a thousands comma, for example `"17,550"`. Parse it before use. |

The monthly files in `data_gen/data/pihps/raw/` are the same data as served by
the website (JSON), kept as evidence of the source.

---

## Related files that are not CSV

- `data_gen/out/scenarios.json`: the three demo disruptions (S1 delay notice,
  S2 price increase, S3 capacity shortfall), each with the affected `ebeln`,
  `lifnr` and the supplier's message. The agent may read it; it plays the role
  of incoming supplier notices.
- `data_gen/out/meta.json`: seed, date window, input files and row counts of the last run.
- `data_gen/out/validation_report.md`: sanity checks on the generated data.
