"""Sanity checks on a generated dataset. Writes out/validation_report.md.

    python validate.py --out out

Checks that (1) PO prices follow the PIHPS anchor, (2) observed late rates
match the hidden truth, (3) seasonal effects and the planted buyer pattern
are visible, and (4) the delay label is learnable but not trivial
(classical baselines, the bar SAP-RPT has to beat).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    o = Path(ap.parse_args().out)
    ekko = pd.read_csv(o / "EKKO.csv", dtype=str, parse_dates=["BEDAT"])
    ekpo = pd.read_csv(o / "EKPO.csv", dtype={"EBELN": str})
    eket = pd.read_csv(o / "EKET.csv", dtype={"EBELN": str}, parse_dates=["EINDT"])
    ekbe = pd.read_csv(o / "EKBE.csv", dtype={"EBELN": str}, parse_dates=["BUDAT"])
    mp = pd.read_csv(o / "market_price_daily.csv", parse_dates=["DATE"])
    rpt = pd.read_csv(o / "rpt_po_lines.csv", dtype={"EBELN": str, "LIFNR": str})
    truth = pd.read_csv(o / "_truth" / "suppliers_truth.csv", dtype={"lifnr": str})
    lfa1 = pd.read_csv(o / "LFA1.csv", dtype=str, keep_default_na=False)
    quotes = pd.read_csv(o / "quotes_asof.csv", dtype={"LIFNR": str})
    scenarios = json.loads((o / "scenarios.json").read_text(encoding="utf-8"))

    po = ekko.merge(ekpo, on="EBELN").merge(eket[["EBELN", "EINDT"]], on="EBELN")
    po = po.merge(ekbe[["EBELN", "BUDAT"]], on="EBELN", how="left")
    po["LIFNR"] = po["LIFNR"].astype(str)
    lines = ["# GESIT synthetic data: validation report", ""]
    add = lines.append

    add(f"- PO lines: {len(po)} (closed {po.BUDAT.notna().sum()}, open {po.BUDAT.isna().sum()})")
    add(f"- Date range: {po.BEDAT.min().date()} to {po.BEDAT.max().date()}")
    add(f"- Suppliers used: {po.LIFNR.nunique()}, buyers: {po.ERNAM.nunique()}")
    add("")

    # 1. price tracks anchor
    m = po.merge(mp, left_on=["MATNR", "BEDAT"], right_on=["MATNR", "DATE"])
    ratio = m.NETPR / m.PRICE_IDR_KG
    monthly = m.groupby(m.BEDAT.dt.to_period("M")).agg(po=("NETPR", "mean"), anchor=("PRICE_IDR_KG", "mean"))
    corr = monthly.po.corr(monthly.anchor)
    add("## 1. Prices follow PIHPS")
    add(f"- Correlation of monthly mean PO price vs PIHPS anchor: **{corr:.3f}**")
    add(f"- PO price / anchor: mean {ratio.mean():.3f}, min {ratio.min():.3f}, max {ratio.max():.3f}")
    add("")

    # 2. observed late rate vs truth
    c = po[po.BUDAT.notna()].copy()
    c["LATE"] = c.BUDAT > c.EINDT
    obs = c.groupby("LIFNR").agg(n=("LATE", "size"), late_rate=("LATE", "mean"),
                                 share=("LATE", lambda s: len(s) / len(c)),
                                 avg_price_ratio=("NETPR", "mean"))
    obs = obs.join(truth.set_index("lifnr")[["name", "on_time_p", "premium", "archetype"]])
    obs["true_late_base"] = 1 - obs.on_time_p
    add("## 2. Observed late rate vs hidden truth (closed POs)")
    add("Observed rates include rainy season, Lebaran and overload effects, so they sit "
        "above the base rate for sensitive suppliers.")
    add("")
    add("| LIFNR | Supplier | POs | Share | Late (observed) | Late (base truth) | Premium |")
    add("|---|---|---|---|---|---|---|")
    for l, r in obs.iterrows():
        add(f"| {l} | {r['name']} | {int(r.n)} | {r.share:.0%} | {r.late_rate:.0%} | "
            f"{r.true_late_base:.0%} | {r.premium:+.1%} |")
    add("")

    # 3. seasonality + planted pattern
    rp = rpt[rpt.LATE.notna()]
    add("## 3. Seasonal effects and planted pattern")
    for col, label in (("RAINY_SEASON", "Rainy season"), ("LEBARAN_WINDOW", "Lebaran window")):
        g = rp.groupby(col).LATE.agg(["mean", "size"])
        add(f"- {label}: late rate {g.loc[1, 'mean']:.0%} (n={g.loc[1, 'size']}) vs "
            f"{g.loc[0, 'mean']:.0%} otherwise" if 1 in g.index else f"- {label}: no rows")
    share = pd.crosstab(po.ERNAM, po.LIFNR == "100107", normalize="index")[True]
    add("- Share of each buyer's POs going to 100107 (planted favourite of BUYER03): "
        + ", ".join(f"{b} {v:.0%}" for b, v in share.items()))
    drift = rp[rp.LIFNR == "100108"].copy()
    drift["half"] = np.where(pd.to_datetime(drift.BEDAT) >= "2026-03-01", "since Mar 2026", "before")
    add("- 100108 late rate (drifting supplier): "
        + ", ".join(f"{k} {r['mean']:.0%} (n={int(r['size'])})"
                    for k, r in drift.groupby("half").LATE.agg(["mean", "size"]).iterrows()))
    add("")

    # 3b. demo preconditions (FR-DET-2, POL-2)
    add("## 3b. Demo preconditions")
    s2 = next(s for s in scenarios if s["id"] == "S2")
    s2_pct = s2["new_netpr"] / s2["old_netpr"] - 1
    add(f"- S2 price revision: {s2['old_netpr']:,.0f} to {s2['new_netpr']:,.0f} "
        f"(+{s2_pct:.1%}), FR-DET-2 trigger >3%: **{'OK' if s2_pct > 0.03 else 'FAIL'}**")
    off = lfa1[lfa1.ZZPANEL != "X"].LIFNR.tolist()
    off_pos = int(po.LIFNR.isin(off).sum())
    off_quotes = int(quotes.LIFNR.isin(off).sum())
    ok = len(off) >= 1 and off_pos == 0 and off_quotes >= 1
    add(f"- Off-panel suppliers (LFA1.ZZPANEL blank): {', '.join(off) or 'none'}; "
        f"POs in history {off_pos}, quotes {off_quotes}, POL-2 demo: **{'OK' if ok else 'FAIL'}**")
    add("")

    # 4. learnability
    add("## 4. Baselines for the delay label (the bar SAP-RPT should beat)")
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        feats = ["LEAD_DAYS", "MENGE", "PRICE_VS_MARKET", "RAINY_SEASON", "LEBARAN_WINDOW",
                 "SUPPLIER_UTIL_30D", "SUP_LATE_RATE_90D", "SUP_AVG_DELAY_90D",
                 "SUP_N_RECEIPTS_90D", "ORDER_MONTH"]
        tr, te = rpt[rpt.SPLIT == "train"], rpt[rpt.SPLIT == "test"]
        Xtr = pd.get_dummies(tr[feats + ["LIFNR"]], columns=["LIFNR"])
        Xte = pd.get_dummies(te[feats + ["LIFNR"]], columns=["LIFNR"]).reindex(columns=Xtr.columns, fill_value=0)
        med = Xtr.median()
        add(f"- Train {len(tr)} rows (late {tr.LATE.mean():.0%}), test {len(te)} rows "
            f"(late {te.LATE.mean():.0%}), time-based split")
        if te.LATE.nunique() == 2:
            lr = LogisticRegression(max_iter=2000).fit((Xtr.fillna(med) - med) / (Xtr.std() + 1e-9), tr.LATE)
            auc_lr = roc_auc_score(te.LATE, lr.predict_proba((Xte.fillna(med) - med) / (Xtr.std() + 1e-9))[:, 1])
            gb = HistGradientBoostingClassifier(max_depth=3, random_state=0).fit(Xtr, tr.LATE)
            auc_gb = roc_auc_score(te.LATE, gb.predict_proba(Xte)[:, 1])
            add(f"- Logistic regression AUC: **{auc_lr:.3f}**")
            add(f"- Gradient boosting AUC: **{auc_gb:.3f}**")
            add("- Healthy range is roughly 0.65 to 0.85. Above 0.9 means the generator is too "
                "easy (add noise); near 0.5 means the signal is too weak.")
    except ImportError:
        add("- scikit-learn not installed, skipped")
    add("")
    text = "\n".join(lines)
    (o / "validation_report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
