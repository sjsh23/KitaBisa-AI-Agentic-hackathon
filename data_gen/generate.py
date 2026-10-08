"""Generate the GESIT synthetic procurement dataset, priced on PIHPS.

    python generate.py                # uses config.py
    python generate.py --out out      # output folder

Agent-visible output (out/):
  LFA1.csv  MAKT.csv  EKKO.csv  EKPO.csv  EKET.csv  EKBE.csv   S/4HANA-style tables
  market_price_daily.csv   the PIHPS anchor actually used (public data)
  quotes_asof.csv          current quotes from every active supplier
  inventory_asof.csv       stock and days of cover at AS_OF
  scenarios.json           the 3 demo disruption scenarios
  rpt_po_lines.csv         one row per PO line, features + label, for SAP-RPT
Hidden (out/_truth/, never give to the agent):
  suppliers_truth.csv  buyers_truth.csv  open_po_outcomes.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import config as C
from world import World, round50

DAY = pd.Timedelta(days=1)


def simulate_history(world: World) -> pd.DataFrame:
    rng = world.rng
    as_of = pd.Timestamp(C.AS_OF)
    buyers = C.BUYERS
    booked: list[tuple] = []   # (bedat, lifnr, qty)

    def utilization(lifnr, d, extra):
        cap = world.suppliers[lifnr].capacity_kg_month
        recent = sum(q for b, l, q in booked if l == lifnr and d - 30 * DAY < b <= d)
        return (recent + extra) / cap

    rows = []
    for m in world.materials.values():
        d = pd.Timestamp(C.HISTORY_START)
        while d <= as_of:
            if d.weekday() == 6:          # no POs on Sunday
                d += DAY
                continue
            buyer = buyers[rng.integers(len(buyers))]
            elig = [l for l, s in world.suppliers.items()
                    if s.on_panel and world.is_active(l, d) and m.matnr in s.materials]
            market = world.market_price(m.matnr, d)
            quotes = {l: world.quote_price(l, m.matnr, d) for l in elig}
            util_score = np.array([
                -C.PRICE_WEIGHT * (quotes[l] / market - 1)
                - C.LEAD_WEIGHT * world.suppliers[l].lead_days
                + buyer.bias.get(l, 0.0)
                for l in elig]) / C.CHOICE_TEMPERATURE
            p = np.exp(util_score - util_score.max())
            lifnr = elig[rng.choice(len(elig), p=p / p.sum())]

            lo, hi = m.order_qty_kg
            qty = float(rng.integers(lo // 500, hi // 500 + 1) * 500)
            sup = world.suppliers[lifnr]
            eindt = d + sup.lead_days * DAY
            u = utilization(lifnr, d, qty)
            out = world.sample_delivery(lifnr, eindt, qty, u)
            gr = eindt + out["delay_days"] * DAY
            rows.append({
                "BEDAT": d, "ERNAM": buyer.ernam, "LIFNR": lifnr, "MATNR": m.matnr,
                "MENGE": qty, "NETPR": quotes[lifnr], "MARKET_PRICE": market,
                "EINDT": eindt, "GR_DATE": gr, "RECEIVED": out["received_qty"],
                "DELAY_DAYS": out["delay_days"], "P_ON_TIME_TRUE": out["p_on_time"],
                "UTIL_30D": u, "SCENARIO": "",
            })
            booked.append((d, lifnr, qty))
            g0, g1 = m.order_every_days
            d += int(rng.integers(g0, g1 + 1)) * DAY
    return pd.DataFrame(rows)


def add_scenarios(world: World, hist: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Three open POs with a scripted disruption each, for the live demo."""
    as_of = pd.Timestamp(C.AS_OF)
    m = next(iter(world.materials.values()))

    specs = [
        ("S1", "100101", 2, 10_000, "BUYER01"),
        ("S2", "100103", 1, 8_000, "BUYER02"),
        ("S3", "100104", 1, 12_000, "BUYER04"),
    ]
    rows, scen = [], []
    for sid, lifnr, days_ago, qty, ernam in specs:
        sup = world.suppliers[lifnr]
        bedat = as_of - days_ago * DAY
        eindt = bedat + sup.lead_days * DAY
        price = world.quote_price(lifnr, m.matnr, bedat)
        rows.append({
            "BEDAT": bedat, "ERNAM": ernam, "LIFNR": lifnr, "MATNR": m.matnr,
            "MENGE": float(qty), "NETPR": price,
            "MARKET_PRICE": world.market_price(m.matnr, bedat),
            "EINDT": eindt, "GR_DATE": pd.NaT, "RECEIVED": np.nan, "DELAY_DAYS": np.nan,
            "P_ON_TIME_TRUE": world.on_time_prob(lifnr, eindt), "UTIL_30D": np.nan,
            "SCENARIO": sid,
        })
        if sid == "S1":
            scen.append({
                "id": sid, "type": "delay_notice", "lifnr": lifnr, "matnr": m.matnr,
                "notice_date": str(as_of.date()), "original_eindt": str(eindt.date()),
                "new_eindt": str((eindt + 7 * DAY).date()), "delay_days": 7,
                "message": f"{sup.name} reports a 7-day delay on its sugar delivery "
                           f"(rough seas on the Bakauheni crossing).",
            })
        elif sid == "S2":
            # FR-DET-2: scripted size from config, reported after rounding to Rp50
            new_price = round50(price * (1 + C.S2_PRICE_INCREASE_PCT))
            pct = new_price / price - 1
            scen.append({
                "id": sid, "type": "price_increase", "lifnr": lifnr, "matnr": m.matnr,
                "notice_date": str(as_of.date()), "old_netpr": price, "new_netpr": new_price,
                "increase_pct": round(pct * 100, 1),
                "note": "Scripted increase (S2_PRICE_INCREASE_PCT in config.py), simulated.",
                "message": f"{sup.name} asks to revise the price by "
                           f"+{pct:.1%} before confirming the PO.",
            })
        else:
            scen.append({
                "id": sid, "type": "capacity_shortfall", "lifnr": lifnr, "matnr": m.matnr,
                "notice_date": str(as_of.date()), "ordered_kg": qty,
                "deliverable_kg": qty * 0.4,
                "message": f"{sup.name} can only deliver 40% of the ordered quantity "
                           f"on time; the rest would follow 10 days later.",
            })
    return pd.concat([hist, pd.DataFrame(rows)], ignore_index=True), scen


def build_tables(world: World, po: pd.DataFrame) -> dict[str, pd.DataFrame]:
    as_of = pd.Timestamp(C.AS_OF)
    po = po.sort_values(["BEDAT", "MATNR"]).reset_index(drop=True)
    po["EBELN"] = [f"45{i + 1:08d}" for i in range(len(po))]
    po["EBELP"] = 10
    po["CLOSED"] = po["GR_DATE"].notna() & (po["GR_DATE"] <= as_of)
    co = C.COMPANY
    mat = world.materials

    lfa1 = pd.DataFrame([{"LIFNR": s.lifnr, "NAME1": s.name, "ORT01": s.city,
                          "REGIO": s.province, "LAND1": "ID",
                          "ERDAT": (s.active_from or "2020-01-01"),
                          # POL-2: custom field, X = on the approved panel
                          "ZZPANEL": "X" if s.on_panel else ""} for s in C.SUPPLIERS])
    makt = pd.DataFrame([{"MATNR": m.matnr, "SPRAS": "EN", "MAKTX": m.maktx,
                          "MEINS": m.meins, "PIHPS_SERIES": m.pihps_series}
                         for m in mat.values()])
    ekko = pd.DataFrame({"EBELN": po.EBELN, "BUKRS": co["BUKRS"], "BSTYP": "F",
                         "BSART": "NB", "LIFNR": po.LIFNR, "EKORG": co["EKORG"],
                         "EKGRP": co["EKGRP"], "BEDAT": po.BEDAT.dt.date,
                         "ERNAM": po.ERNAM, "WAERS": "IDR"})
    ekpo = pd.DataFrame({"EBELN": po.EBELN, "EBELP": po.EBELP, "MATNR": po.MATNR,
                         "TXZ01": po.MATNR.map(lambda x: mat[x].maktx),
                         "WERKS": co["WERKS"], "MENGE": po.MENGE, "MEINS": "KG",
                         "NETPR": po.NETPR, "PEINH": 1, "NETWR": po.MENGE * po.NETPR,
                         "ELIKZ": np.where(po.CLOSED, "X", "")})
    eket = pd.DataFrame({"EBELN": po.EBELN, "EBELP": po.EBELP, "ETENR": 1,
                         "EINDT": po.EINDT.dt.date, "MENGE": po.MENGE,
                         "WEMNG": np.where(po.CLOSED, po.RECEIVED, 0.0)})
    closed = po[po.CLOSED]
    ekbe = pd.DataFrame({"EBELN": closed.EBELN, "EBELP": closed.EBELP, "VGABE": "1",
                         "BEWTP": "E", "BWART": "101", "BUDAT": closed.GR_DATE.dt.date,
                         "MENGE": closed.RECEIVED, "DMBTR": closed.RECEIVED * closed.NETPR,
                         "WAERS": "IDR"})

    open_truth = po[~po.CLOSED][["EBELN", "LIFNR", "MATNR", "BEDAT", "EINDT", "GR_DATE",
                                 "DELAY_DAYS", "RECEIVED", "P_ON_TIME_TRUE", "SCENARIO"]]
    return {"po": po, "LFA1": lfa1, "MAKT": makt, "EKKO": ekko, "EKPO": ekpo,
            "EKET": eket, "EKBE": ekbe, "_open_truth": open_truth}


def build_rpt_table(world: World, po: pd.DataFrame) -> pd.DataFrame:
    """Features known at order time (no leakage) + label, for SAP-RPT / baselines."""
    as_of = pd.Timestamp(C.AS_OF)
    closed = po[po.CLOSED]
    test_start = as_of - pd.DateOffset(months=C.TEST_MONTHS)
    test_end = as_of - 30 * DAY   # recent late POs are still open: avoid censoring bias
    rows = []
    for r in po.itertuples():
        prior = closed[(closed.LIFNR == r.LIFNR) & (closed.GR_DATE < r.BEDAT)]
        win = prior[prior.GR_DATE >= r.BEDAT - 90 * DAY]
        late = (win.GR_DATE > win.EINDT)
        booked = po[(po.LIFNR == r.LIFNR) & (po.BEDAT > r.BEDAT - 30 * DAY) & (po.BEDAT <= r.BEDAT)]
        if r.CLOSED:
            split = "test" if test_start <= r.BEDAT <= test_end else (
                "train" if r.BEDAT < test_start else "holdout_recent")
        else:
            split = "predict"
        rows.append({
            "EBELN": r.EBELN, "EBELP": r.EBELP, "LIFNR": r.LIFNR, "MATNR": r.MATNR,
            "BEDAT": r.BEDAT.date(), "EINDT": r.EINDT.date(),
            "ORDER_MONTH": r.BEDAT.month, "MENGE": r.MENGE,
            "LEAD_DAYS": (r.EINDT - r.BEDAT).days, "NETPR": r.NETPR,
            "PRICE_VS_MARKET": round(r.NETPR / r.MARKET_PRICE, 4),
            "RAINY_SEASON": int(world.is_rainy(r.EINDT)),
            "LEBARAN_WINDOW": int(world.in_lebaran_window(r.EINDT)),
            "SUPPLIER_UTIL_30D": round(booked.MENGE.sum()
                                       / world.suppliers[r.LIFNR].capacity_kg_month, 3),
            "SUP_N_RECEIPTS_ALL": len(prior),
            "SUP_N_RECEIPTS_90D": len(win),
            "SUP_LATE_RATE_90D": round(late.mean(), 3) if len(win) else np.nan,
            "SUP_AVG_DELAY_90D": round((win.GR_DATE - win.EINDT).dt.days.clip(lower=0).mean(), 2)
            if len(win) else np.nan,
            "LATE": (int(r.GR_DATE > r.EINDT) if r.CLOSED else np.nan),
            "DELAY_DAYS": ((r.GR_DATE - r.EINDT).days if r.CLOSED else np.nan),
            "SPLIT": split,
        })
    return pd.DataFrame(rows)


def build_quotes_and_inventory(world: World, po: pd.DataFrame):
    as_of = pd.Timestamp(C.AS_OF)
    q, inv = [], []
    for m in world.materials.values():
        for l, s in world.suppliers.items():
            if not world.is_active(l, as_of) or m.matnr not in s.materials:
                continue
            recent = po[(po.LIFNR == l) & (po.BEDAT > as_of - 30 * DAY)].MENGE.sum()
            q.append({"MATNR": m.matnr, "LIFNR": l, "QUOTE_DATE": as_of.date(),
                      "NETPR": world.quote_price(l, m.matnr, as_of), "PEINH": 1,
                      "WAERS": "IDR", "LEAD_DAYS": s.lead_days,
                      "AVAILABLE_KG_NEXT_30D": max(0.0, s.capacity_kg_month - recent),
                      "VALID_TO": (as_of + 7 * DAY).date()})
        open_qty = po[(po.MATNR == m.matnr) & ~po.CLOSED].MENGE.sum()
        stock = m.daily_usage_kg * m.stock_at_as_of_days
        inv.append({"MATNR": m.matnr, "WERKS": C.COMPANY["WERKS"], "DATE": as_of.date(),
                    "LABST": stock, "DAILY_USAGE_KG": m.daily_usage_kg,
                    "EISBE": m.daily_usage_kg * m.safety_stock_days,
                    "DAYS_OF_COVER": round(stock / m.daily_usage_kg, 1),
                    "OPEN_PO_QTY": open_qty,
                    "DOWNTIME_COST_PER_DAY": C.DOWNTIME_COST_PER_DAY})
    return pd.DataFrame(q), pd.DataFrame(inv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--pihps", nargs="+", help="override PIHPS_FILES from config.py")
    a = ap.parse_args()
    if a.pihps:
        C.PIHPS_FILES = a.pihps
    out = Path(a.out)
    (out / "_truth").mkdir(parents=True, exist_ok=True)

    world = World()
    hist = simulate_history(world)
    po, scenarios = add_scenarios(world, hist)
    t = build_tables(world, po)
    rpt = build_rpt_table(world, t["po"])
    quotes, inv = build_quotes_and_inventory(world, t["po"])

    for name in ("LFA1", "MAKT", "EKKO", "EKPO", "EKET", "EKBE"):
        t[name].to_csv(out / f"{name}.csv", index=False)
    rpt.to_csv(out / "rpt_po_lines.csv", index=False)
    quotes.to_csv(out / "quotes_asof.csv", index=False)
    inv.to_csv(out / "inventory_asof.csv", index=False)
    # attach EBELN of each scenario PO
    po_s = t["po"][t["po"].SCENARIO != ""].set_index("SCENARIO").EBELN
    for s in scenarios:
        s["ebeln"] = po_s[s["id"]]
    (out / "scenarios.json").write_text(json.dumps(scenarios, indent=2), encoding="utf-8")

    mp = pd.concat([pd.DataFrame({"MATNR": k, "DATE": v.index.date, "PRICE_IDR_KG": v.values,
                                  "SOURCE": f"PIHPS Bank Indonesia, {C.PIHPS_MARKET}, "
                                            f"x{C.PRICE_SCALE}, forward-filled"})
                    for k, v in world.anchor.items()])
    mp.to_csv(out / "market_price_daily.csv", index=False)

    world.truth_table().to_csv(out / "_truth" / "suppliers_truth.csv", index=False)
    pd.DataFrame([{"ERNAM": b.ernam, "NAME": b.name, "BIAS": json.dumps(b.bias)}
                  for b in C.BUYERS]).to_csv(out / "_truth" / "buyers_truth.csv", index=False)
    t["_open_truth"].to_csv(out / "_truth" / "open_po_outcomes.csv", index=False)

    meta = {"seed": C.SEED, "history_start": C.HISTORY_START, "as_of": C.AS_OF,
            "pihps_files": C.PIHPS_FILES, "pihps_market": C.PIHPS_MARKET,
            "price_scale": C.PRICE_SCALE, "po_lines": len(t["po"]),
            "closed": int(t["po"].CLOSED.sum()), "open": int((~t["po"].CLOSED).sum())}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
