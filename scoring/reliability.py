"""Supplier reliability as a Beta posterior (FR-EVA-2, FR-EVA-3).

alpha counts on-time receipts, beta counts late ones. The prior is Beta(2, 1),
weak and slightly optimistic. Before each new receipt both counts are
multiplied by the forgetting factor 0.97, so recent receipts weigh more and a
deteriorating supplier shows up within weeks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from scipy.stats import beta as beta_dist

PRIOR_ALPHA = 2.0
PRIOR_BETA = 1.0
FORGETTING_FACTOR = 0.97
INTERVAL_MASS = 0.90

# The delay risk GESIT uses is a fixed 50:50 blend of SAP-RPT and this posterior (team
# decision, 20 Oct 2026, in the PRD since 21 Oct: FR-DET-4, FR-EVA-2; fixed, not tuned). `blend` is the only place
# the two are combined; BLEND_RULE_TEXT is the only place the rule is worded for a report.
BLEND_WEIGHT_RPT = 0.5
BLEND_RULE_TEXT = (f"P(late) = {BLEND_WEIGHT_RPT:g} x SAP-RPT + {1 - BLEND_WEIGHT_RPT:g} x "
                   f"reliability posterior (Beta, forgetting {FORGETTING_FACTOR:g} per receipt)")


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


def posterior_before(receipts: Iterable[Mapping[str, Any]], today: str) -> Reliability:
    """Posterior from the goods receipts dated before `today` (BUDAT < today), oldest first.
    A receipt on or after the scenario date is never known when the decision is made.
    Every posterior a tool reports or uses goes through here (FR-EVA-2, FR-DET-4)."""
    return posterior(r["LATE"] for r in receipts if r["BUDAT"] < today)


def blend(p_rpt: float | None, p_post: float, weight_rpt: float = BLEND_WEIGHT_RPT) -> float:
    """P(late) GESIT uses: the weighted mean of SAP-RPT and the posterior, or the posterior
    alone when SAP-RPT gave no value (FR-DET-4)."""
    if p_rpt is None:
        return p_post
    return weight_rpt * p_rpt + (1.0 - weight_rpt) * p_post
