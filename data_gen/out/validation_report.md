# GESIT synthetic data: validation report

- PO lines: 434 (closed 429, open 5)
- Date range: 2024-10-01 to 2026-09-30
- Suppliers used: 10, buyers: 4

## 1. Prices follow PIHPS
- Correlation of monthly mean PO price vs PIHPS anchor: **0.949**
- PO price / anchor: mean 0.993, min 0.927, max 1.073

## 2. Observed late rate vs hidden truth (closed POs)
Observed rates include rainy season, Lebaran and overload effects, so they sit above the base rate for sensitive suppliers.

| LIFNR | Supplier | POs | Share | Late (observed) | Late (base truth) | Premium |
|---|---|---|---|---|---|---|
| 100101 | PT Sinar Manis Lampung | 81 | 19% | 46% | 34% | -4.0% |
| 100102 | PT Kristal Prima Jaya | 15 | 3% | 7% | 4% | +5.5% |
| 100103 | CV Tebu Makmur Sejahtera | 43 | 10% | 16% | 12% | +1.0% |
| 100104 | PT Cepat Antar Pangan | 31 | 7% | 19% | 8% | +3.0% |
| 100105 | CV Gula Rakyat Nusantara | 50 | 12% | 20% | 10% | -1.5% |
| 100106 | PT Distribusi Sentosa Abadi | 56 | 13% | 16% | 11% | +0.0% |
| 100107 | CV Mitra Karya Bersama | 36 | 8% | 33% | 22% | +3.5% |
| 100108 | PT Agro Niaga Utama | 51 | 12% | 18% | 7% | +0.5% |
| 100109 | PT Sumber Rasa Baru | 4 | 1% | 25% | 9% | +0.0% |
| 100110 | UD Berkah Tani | 62 | 14% | 32% | 18% | -2.5% |

## 3. Seasonal effects and planted pattern
- Rainy season: late rate 30% (n=178) vs 24% otherwise
- Lebaran window: late rate 17% (n=18) vs 27% otherwise
- Share of each buyer's POs going to 100107 (planted favourite of BUYER03): BUYER01 2%, BUYER02 4%, BUYER03 24%, BUYER04 4%
- 100108 late rate (drifting supplier): before 11% (n=36), since Mar 2026 33% (n=15)

## 3b. Demo preconditions
- S2 price revision: 17,650 to 18,700 (+5.9%), FR-DET-2 trigger >3%: **OK**
- Off-panel suppliers (LFA1.ZZPANEL blank): 100111; POs in history 0, quotes 1, POL-2 demo: **OK**

## 4. Baselines for the delay label (the bar SAP-RPT should beat)
- Train 340 rows (late 26%), test 74 rows (late 24%), time-based split
- Logistic regression AUC: **0.669**
- Gradient boosting AUC: **0.715**
- Healthy range is roughly 0.65 to 0.85. Above 0.9 means the generator is too easy (add noise); near 0.5 means the signal is too weak.
