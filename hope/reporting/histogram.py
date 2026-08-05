"""Histogram binning + k-anonymity merging for the leaderboard report.

Two responsibilities:

  * `compute_histogram(scores, n_bins, range_)` — equal-width binning
    of miner scores over a fixed range. Returns the canonical
    `(bin_edges, bin_counts)` pair plus a five-number summary.
  * `merge_for_k_anonymity(bin_edges, bin_counts, floor)` — applies
    the deterministic left-merge rule from contract Q13 so the
    published histogram never carries a bin with `1 ≤ count < floor`.

The left-merge rule (Q13 lock-in):

  Scan `bin_counts` left-to-right. For each bin with
  `1 ≤ count < floor`:
      - If it is the first bin (no left neighbor), merge it RIGHT
        into the next bin.
      - Otherwise merge it LEFT into the previous bin.
  After each merge, re-scan from the start. Iterate to fixed point.

Why this rule:

  * Deterministic — both the operator and any external verifier reach
    byte-identical output for the same input.
  * Trivial to implement, trivial to audit.
  * Loses minimal histogram shape vs. "merge-into-smaller-neighbor"
    rules, because all merges happen sequentially from the left edge.

The functions are pure: no I/O, no clock, no random state. Same input
→ same output forever. The aggregator and the verifier both call
these directly; reproducibility is mechanical.
"""

from __future__ import annotations

from typing import Iterable

from hope.reporting.payload import ScoreSummary


def _clip(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def compute_histogram(
    scores: Iterable[float],
    *,
    n_bins: int = 15,
    range_: tuple[float, float] = (0.0, 1.0),
) -> tuple[list[float], list[int]]:
    """Bin `scores` into `n_bins` equal-width buckets over `range_`.

    Args:
        scores: iterable of miner scores. Values outside `range_` are
            clipped into range (a `MinerScore.final_score > 1.0` floats
            into the top bin, etc.). The aggregator filters to the
            qualifying pool before calling this — non-qualifying
            miners do not contribute.
        n_bins: number of buckets (default 15 per Q2 / the operator's reply).
        range_: `(lo, hi)` inclusive bounds for the binning domain.
            Defaults to `(0.0, 1.0)` per Q3 (`MinerScore.final_score`
            lives in `[0, 1]`).

    Returns:
        `(bin_edges, bin_counts)` where `bin_edges` has length
        `n_bins + 1` and `bin_counts` has length `n_bins`. The last
        bin is inclusive at the upper bound so a score equal to `hi`
        falls into `bin_counts[-1]`.

    Raises:
        ValueError: `n_bins < 1` or `range_` is degenerate.
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    lo, hi = range_
    if hi <= lo:
        raise ValueError(f"range_ must have hi > lo, got {range_!r}")

    step = (hi - lo) / n_bins
    bin_edges = [lo + i * step for i in range(n_bins)]
    bin_edges.append(hi)   # exactly hi, no float drift

    bin_counts = [0] * n_bins
    for raw in scores:
        s = _clip(float(raw), lo, hi)
        if s == hi:
            idx = n_bins - 1
        else:
            idx = int((s - lo) / step)
            if idx >= n_bins:
                idx = n_bins - 1
            elif idx < 0:
                idx = 0
        bin_counts[idx] += 1

    return bin_edges, bin_counts


def compute_summary(scores: Iterable[float]) -> ScoreSummary:
    """Five-number summary of the qualifying pool.

    Uses linear interpolation between order statistics for the
    quantiles (the standard numpy / pandas default). For an empty
    input every field is `0.0`.
    """
    sorted_scores = sorted(float(s) for s in scores)
    n = len(sorted_scores)
    if n == 0:
        return ScoreSummary(mean=0.0, median=0.0, p25=0.0, p75=0.0, max=0.0)

    def _q(p: float) -> float:
        idx_f = p * (n - 1)
        idx_l = int(idx_f)
        idx_h = min(idx_l + 1, n - 1)
        frac = idx_f - idx_l
        return sorted_scores[idx_l] * (1.0 - frac) + sorted_scores[idx_h] * frac

    return ScoreSummary(
        mean=sum(sorted_scores) / n,
        median=_q(0.5),
        p25=_q(0.25),
        p75=_q(0.75),
        max=sorted_scores[-1],
    )


def merge_for_k_anonymity(
    bin_edges: list[float],
    bin_counts: list[int],
    *,
    floor: int = 5,
) -> tuple[list[float], list[int]]:
    """Apply the deterministic k-anonymity left-merge rule.

    Per contract Q13, scan left-to-right. For each bin where
    `1 <= count < floor`:

        - First bin (no left neighbor)? → merge RIGHT into the next bin.
        - Otherwise → merge LEFT into the previous bin.

    Re-scan after each merge; iterate until no bin is in the forbidden
    range. Empty bins (count == 0) are left alone — they convey no
    information about any specific miner.

    Args:
        bin_edges: list of `n+1` edges, strictly ascending.
        bin_counts: list of `n` non-negative counts.
        floor: k-anonymity threshold (default 5 per contract §3.2).

    Returns:
        `(merged_edges, merged_counts)`. Length depends on how many
        merges happened. Total count is preserved exactly. Edges
        remain strictly ascending.

    Raises:
        ValueError: lengths inconsistent or floor < 1.
    """
    if floor < 1:
        raise ValueError(f"floor must be >= 1, got {floor}")
    if len(bin_edges) != len(bin_counts) + 1:
        raise ValueError(
            f"bin_edges (len {len(bin_edges)}) must be one longer than "
            f"bin_counts (len {len(bin_counts)})"
        )

    edges = list(bin_edges)
    counts = list(bin_counts)

    while True:
        target = next(
            (i for i, c in enumerate(counts) if 1 <= c < floor),
            None,
        )
        if target is None:
            break
        if len(counts) == 1:
            # A single tiny bin can't be merged with anything; the
            # aggregator's pool-below-floor short-circuit should have
            # caught this before we got here, but bail safely anyway.
            break
        if target == 0:
            # Merge right into bin 1.
            counts[1] += counts[0]
            del counts[0]
            del edges[1]   # boundary between (old) bins 0 and 1
        else:
            # Merge into left neighbor.
            counts[target - 1] += counts[target]
            del counts[target]
            del edges[target]   # boundary between bins target-1 and target

    return edges, counts
