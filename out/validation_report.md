# GESIT synthetic data: validation report

- PO lines: 434 (closed 429, open 5)
- Date range: 2024-10-01 to 2026-09-30
- Suppliers used: 10, buyers: 4

## 1. Prices follow PIHPS
- Correlation of monthly mean PO price vs PIHPS anchor: **0.956**
- PO price / anchor: mean 0.995, min 0.927, max 1.073

## 2. Observed late rate vs hidden truth (closed POs)
Observed rates include rainy season, Lebaran and overload effects, so they sit above the base rate for sensitive suppliers.

| LIFNR | Supplier | POs | Share | Late (observed) | Late (base truth) | Premium |
|---|---|---|---|---|---|---|
| 100101 | PT Sinar Manis Lampung | 74 | 17% | 47% | 34% | -4.0% |
| 100102 | PT Kristal Prima Jaya | 20 | 5% | 5% | 4% | +5.5% |
| 100103 | CV Tebu Makmur Sejahtera | 42 | 10% | 17% | 12% | +1.0% |
| 100104 | PT Cepat Antar Pangan | 34 | 8% | 18% | 8% | +3.0% |
| 100105 | CV Gula Rakyat Nusantara | 50 | 12% | 18% | 10% | -1.5% |
| 100106 | PT Distribusi Sentosa Abadi | 60 | 14% | 15% | 11% | +0.0% |
| 100107 | CV Mitra Karya Bersama | 37 | 9% | 30% | 22% | +3.5% |
| 100108 | PT Agro Niaga Utama | 49 | 11% | 12% | 7% | +0.5% |
| 100109 | PT Sumber Rasa Baru | 5 | 1% | 0% | 9% | +0.0% |
| 100110 | UD Berkah Tani | 58 | 14% | 29% | 18% | -2.5% |

## 3. Seasonal effects and planted pattern
- Rainy season: late rate 30% (n=178) vs 19% otherwise
- Lebaran window: late rate 17% (n=18) vs 24% otherwise
- Share of each buyer's POs going to 100107 (planted favourite of BUYER03): BUYER01 3%, BUYER02 3%, BUYER03 24%, BUYER04 5%
- 100108 late rate (drifting supplier): before 11%, since Mar 2026 15%

## 4. Baselines for the delay label (the bar SAP-RPT should beat)
- Train 340 rows (late 26%), test 74 rows (late 14%), time-based split
- Logistic regression AUC: **0.694**
- Gradient boosting AUC: **0.603**
- Healthy range is roughly 0.65 to 0.85. Above 0.9 means the generator is too easy (add noise); near 0.5 means the signal is too weak.
