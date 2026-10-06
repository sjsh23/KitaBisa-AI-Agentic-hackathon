"""The hidden "world": true supplier behaviour that generated the history.

generate.py uses it to write the history. The evaluation (Monte Carlo against
the cheapest / fastest baselines) should import the same World so that
"what would have happened with supplier B" is sampled from the same truth.

Never give the agent anything from this module or from out/_truth/. The agent
only sees the S/4-style tables, like a real procurement team would.
"""
from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

import config as C
from pihps_loader import load_pihps, to_daily


def round50(x: float) -> float:
    return float(np.round(x / 50.0) * 50.0)


class World:
    def __init__(self, seed: int = C.SEED, anchors: dict[str, pd.Series] | None = None):
        self.rng = np.random.default_rng(seed)
        self.suppliers = {s.lifnr: s for s in C.SUPPLIERS}
        self.materials = {m.matnr: m for m in C.MATERIALS if m.enabled}
        self.idul_fitri = [pd.Timestamp(d) for d in C.IDUL_FITRI]
        if anchors is None:
            start = pd.Timestamp(C.HISTORY_START) - pd.Timedelta(days=5)
            anchors = {}
            for m in self.materials.values():
                raw = load_pihps(C.PIHPS_FILES, m.pihps_series)
                anchors[m.matnr] = to_daily(raw, start, C.AS_OF) * C.PRICE_SCALE
        self.anchor = anchors

    # ------------------------------------------------------------ calendar
    def is_rainy(self, d) -> bool:
        return pd.Timestamp(d).month in C.RAINY_MONTHS

    def in_lebaran_window(self, d) -> bool:
        d = pd.Timestamp(d)
        lo, hi = C.LEBARAN_WINDOW
        return any(lo <= (d - f).days <= hi for f in self.idul_fitri)

    # ------------------------------------------------------------ prices
    def market_price(self, matnr: str, d) -> float:
        s = self.anchor[matnr]
        d = pd.Timestamp(d).normalize()
        if d > s.index[-1]:
            return float(s.iloc[-1])   # beyond data: hold last known price
        return float(s.loc[max(d, s.index[0])])

    def quote_price(self, lifnr: str, matnr: str, d, rng=None) -> float:
        rng = rng or self.rng
        sup = self.suppliers[lifnr]
        noise = np.exp(rng.normal(0.0, sup.price_sigma))
        return round50(self.market_price(matnr, d) * (1 + sup.premium) * noise)

    # ------------------------------------------------------------ delivery
    def is_active(self, lifnr: str, d) -> bool:
        sup = self.suppliers[lifnr]
        return sup.active_from is None or pd.Timestamp(d) >= pd.Timestamp(sup.active_from)

    def on_time_prob(self, lifnr: str, delivery_date, utilization: float = 0.0) -> float:
        """True probability of on-time delivery. Hidden from the agent."""
        sup = self.suppliers[lifnr]
        d = pd.Timestamp(delivery_date)
        p = sup.on_time_p
        if sup.drift:
            t0, t1 = pd.Timestamp(sup.drift["start"]), pd.Timestamp(sup.drift["end"])
            if d >= t0:
                frac = min(1.0, (d - t0).days / max(1, (t1 - t0).days))
                p = p + frac * (sup.drift["on_time_p"] - p)
        if self.is_rainy(d):
            p -= sup.rainy_penalty
        if self.in_lebaran_window(d):
            p -= sup.lebaran_penalty
        p -= C.CAPACITY_PENALTY * max(0.0, utilization - 1.0)
        return float(np.clip(p, 0.05, 0.99))

    def sample_delivery(self, lifnr: str, promised_date, qty: float,
                        utilization: float = 0.0, rng=None) -> dict:
        """Sample one delivery outcome: delay in days and quantity received."""
        rng = rng or self.rng
        sup = self.suppliers[lifnr]
        p_on_time = self.on_time_prob(lifnr, promised_date, utilization)
        if rng.random() < p_on_time:
            delay = -int(rng.random() < 0.15)          # occasionally a day early
        else:
            mean = sup.mean_delay_days * (1.5 if self.in_lebaran_window(promised_date) else 1.0)
            n = 2.0                                     # dispersion of the delay distribution
            extra = mean - 1.0
            delay = 1 + int(rng.negative_binomial(n, n / (n + extra))) if extra > 0 else 1
        received = qty
        if rng.random() < sup.short_ship_p:
            received = round50(qty * rng.uniform(0.6, 0.95))
        return {"delay_days": delay, "received_qty": received, "p_on_time": p_on_time}

    def truth_table(self) -> pd.DataFrame:
        rows = []
        for s in C.SUPPLIERS:
            r = asdict(s)
            r["materials"] = ",".join(s.materials)
            r["drift"] = str(s.drift) if s.drift else ""
            rows.append(r)
        return pd.DataFrame(rows)
