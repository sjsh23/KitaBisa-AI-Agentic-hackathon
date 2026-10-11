"""Supplier reliability as a Beta posterior (FR-EVA-2, FR-EVA-3).

alpha counts on-time receipts, beta counts late ones. The prior is Beta(2, 1),
weak and slightly optimistic. Before each new receipt both counts are
multiplied by the forgetting factor 0.97, so recent receipts weigh more and a
deteriorating supplier shows up within weeks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from scipy.stats import beta as beta_dist

PRIOR_ALPHA = 2.0
PRIOR_BETA = 1.0
FORGETTING_FACTOR = 0.97
INTERVAL_MASS = 0.90


@dataclass(frozen=True)
class Reliability:
    """Posterior over a supplier's on-time probability."""

    alpha: float = PRIOR_ALPHA
    beta: float = PRIOR_BETA
    n_receipts: int = 0

    @property
    def mean_on_time(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def p_late(self) -> float:
        """Posterior mean of P(late), the FR-DET-4 fallback for SAP-RPT."""
        return 1.0 - self.mean_on_time

    def interval(self, mass: float = INTERVAL_MASS) -> tuple[float, float]:
        """Central credible interval of the on-time probability (90% by default)."""
        tail = (1.0 - mass) / 2.0
        low, high = beta_dist.ppf([tail, 1.0 - tail], self.alpha, self.beta)
        return float(low), float(high)

    def as_dict(self) -> dict[str, float | int]:
        low, high = self.interval()
        return {
            "ALPHA": round(self.alpha, 4), "BETA": round(self.beta, 4),
            "N_RECEIPTS": self.n_receipts,
            "ON_TIME_MEAN": round(self.mean_on_time, 4), "P_LATE": round(self.p_late, 4),
            "ON_TIME_P05": round(low, 4), "ON_TIME_P95": round(high, 4),
        }


def update(state: Reliability, late: bool, forgetting: float = FORGETTING_FACTOR) -> Reliability:
    """Posterior after one more goods receipt."""
    return Reliability(
        alpha=state.alpha * forgetting + (0.0 if late else 1.0),
        beta=state.beta * forgetting + (1.0 if late else 0.0),
        n_receipts=state.n_receipts + 1,
    )


def posterior(late_flags: Iterable[bool | int], forgetting: float = FORGETTING_FACTOR) -> Reliability:
    """Posterior after a supplier's receipts, given oldest first."""
    state = Reliability()
    for late in late_flags:
        state = update(state, bool(late), forgetting)
    return state
