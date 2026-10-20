"""Integrity Guard pattern flag over EKKO history (FR-INT-3, FR-INT-4).

Plain deterministic Python, no LLM. For every buyer b (EKKO-ERNAM) and supplier s
(EKKO-LIFNR) that appear in the PO headers:

- n_b = POs placed by b, k = POs b placed with s, share_b = k / n_b
- share_others = POs with s by all other buyers / POs by all other buyers
- ratio = share_b / share_others (None when share_others is 0: no other buyer uses s)
- p = one-sided binomial test P(X >= k), X ~ Binomial(n_b, share_others)

A pair is flagged only when k >= 5, ratio >= 2 (or share_others = 0) and p < 0.01.
Every buyer and supplier combination counts as one test, including k = 0, so the
number of tests reported is the honest multiple-testing count.

The system flags, it never accuses or blocks (FR-INT-4): `review_text` builds a
neutral sentence from the numbers, and nothing here touches a PO.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from math import comb
from typing import Any, Iterable, Mapping

MIN_PAIR_POS = 5        # FR-INT-3: b placed at least 5 POs with s
MIN_SHARE_RATIO = 2.0   # FR-INT-3: b's share to s at least 2x the other buyers' share
MAX_P_VALUE = 0.01      # FR-INT-3: one-sided binomial test p < 0.01

RULE_TEXT = (f"k >= {MIN_PAIR_POS} POs with the supplier, share at least "
             f"{MIN_SHARE_RATIO:g}x the other buyers' share, one-sided binomial p < {MAX_P_VALUE}")


@dataclass(frozen=True)
class PairStats:
    """Evidence for one (buyer, supplier) pair; FLAGGED says whether all three rules hold."""
    buyer: str
    supplier: str
    k: int
    n_b: int
    share_b: float
    share_others: float
    ratio: float | None
    p_value: float
    flagged: bool
    override_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {name.upper(): value for name, value in asdict(self).items()}


@dataclass(frozen=True)
class IntegrityScan:
    """All pairs tested and the flagged ones, so the report can say how many tests ran."""
    pairs_tested: int
    flagged: tuple[PairStats, ...]
    pairs: tuple[PairStats, ...]

    @property
    def pairs_flagged(self) -> int:
        return len(self.flagged)


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed with math.comb (n is a few hundred at most)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    tail = sum(comb(n, i) * p ** i * (1.0 - p) ** (n - i) for i in range(k, n + 1))
    return min(1.0, tail)


def pair_stats(buyer: str, supplier: str, k: int, n_b: int, k_others: int, n_others: int,
               override_count: int = 0) -> PairStats:
    """FR-INT-3 for one pair: b's counts against the counts of all other buyers together."""
    share_b = k / n_b if n_b else 0.0
    share_others = k_others / n_others if n_others else 0.0
    ratio = share_b / share_others if share_others > 0 else None
    p_value = binomial_tail(k, n_b, share_others)
    ratio_ok = (ratio >= MIN_SHARE_RATIO) if ratio is not None else k > 0
    flagged = k >= MIN_PAIR_POS and ratio_ok and p_value < MAX_P_VALUE
    return PairStats(buyer, supplier, k, n_b, share_b, share_others, ratio, p_value, flagged,
                     override_count)


def scan_pairs(ekko: Iterable[Mapping[str, Any]],
               override_counts: Mapping[str, int] | None = None) -> IntegrityScan:
    """Test every buyer and supplier combination in the EKKO headers (ERNAM, LIFNR)."""
    override_counts = override_counts or {}
    headers = [(row["ERNAM"], row["LIFNR"]) for row in ekko]
    total = len(headers)
    by_buyer = Counter(b for b, _ in headers)
    by_supplier = Counter(s for _, s in headers)
    by_pair = Counter(headers)
    pairs = []
    for b in sorted(by_buyer):
        for s in sorted(by_supplier):
            k = by_pair[(b, s)]
            pairs.append(pair_stats(b, s, k, by_buyer[b], by_supplier[s] - k,
                                    total - by_buyer[b], override_counts.get(b, 0)))
    flagged = tuple(sorted((p for p in pairs if p.flagged), key=lambda p: p.p_value))
    return IntegrityScan(len(pairs), flagged, tuple(pairs))


def p_text(p_value: float) -> str:
    """'p=0.0040', or 'p<0.0001' for very small values."""
    return "p<0.0001" if p_value < 0.0001 else f"p={p_value:.4f}"


def review_text(pair: PairStats) -> str:
    """Neutral sentence for the manager view, from a template and the numbers only (FR-INT-4)."""
    ratio = f"{pair.ratio:.1f}x" if pair.ratio is not None else "no other buyer uses this supplier"
    return (f"Pattern for review: {pair.buyer} placed {pair.k} of {pair.n_b} POs "
            f"({pair.share_b:.1%}) with {pair.supplier}, vs {pair.share_others:.1%} for other "
            f"buyers ({ratio}, {p_text(pair.p_value)}, simulated data). Overrides logged for "
            f"{pair.buyer}: {pair.override_count}. This is a statistic, not a finding.")
