"""Pure aggregator — turns a private EpochArtifact into a public payload.

The single entry point is `aggregate(artifact) -> EpochReportPayload`.

Pure function discipline:
  * No I/O. No clock. No randomness. No chain reads. No env lookups.
  * Same input → byte-identical output, forever.
  * The verifier (`scripts/verify_epoch.py --emit-report`) calls this
    function on a chain-reconstructed artifact and produces the same
    payload as the operator, by construction.

Anti-doxxing discipline:
  * The return type is `EpochReportPayload`, which forbids extra fields
    (`extra="forbid"`). Per-UID data from the artifact never reaches
    the public payload — only counts, shares, and distribution shape.
  * The aggregator never returns the artifact, the score map, or any
    derived list of per-miner values. Only scalar / distribution
    aggregates flow out.
"""

from __future__ import annotations

from typing import Any

from hope.reporting.histogram import (
    compute_histogram,
    compute_summary,
    merge_for_k_anonymity,
)
from hope.reporting.payload import (
    COMPETITIVE_EMISSION_SHARE,
    ELITE_EMISSION_SHARE,
    EmergencyIntervention,
    EpochReportPayload,
    PARTICIPATING_EMISSION_SHARE,
    POOL_SIZE_DISTRIBUTION_FLOOR,
    ScoreDistribution,
    TierDistribution,
    TierSlice,
)
from hope.reporting.epoch_artifact import EpochArtifact


# In v1 the simplified gate is "raw_score > baseline" with baseline=0.
# Phase 1 of richer scoring (Q11) plumbs in per-episode conditional
# priors; the aggregator becomes a more meaningful filter at that
# point. Until then this constant is the bar miners must clear.
BASELINE_SCORE_V1 = 0.0


def _qualifying_scores(artifact: EpochArtifact) -> list[float]:
    """Return raw_score values for miners that passed the participation gate.

    Cross-references `artifact.per_uid_scores` (per-miner detail) with
    `artifact.tier_result["qualifying"]` (the gate-passing hotkey set).
    """
    qualifying_hotkeys = set(artifact.tier_result.get("qualifying", []))
    return [
        float(row["raw_score"])
        for row in artifact.per_uid_scores
        if row.get("hotkey") in qualifying_hotkeys
    ]


def _baseline_beat_rate(qualifying_scores: list[float]) -> float:
    """Fraction of qualifying miners with raw_score strictly above baseline.

    Per contract §3.3: denominator is pool_size, numerator is the count
    of qualifying miners whose miner_score beats the conditional-prior
    baseline. In v1 with BASELINE_SCORE_V1=0 and a gate that already
    requires raw_score > 0, this collapses to 1.0 for any non-empty
    pool. Computed literally so that when richer per-miner baselines
    plumb through, the same code produces a meaningful number.
    """
    pool_size = len(qualifying_scores)
    if pool_size == 0:
        return 0.0
    beats = sum(1 for s in qualifying_scores if s > BASELINE_SCORE_V1)
    return beats / pool_size


def _build_score_distribution(
    qualifying_scores: list[float],
    *,
    n_bins: int = 15,
    score_range: tuple[float, float] = (0.0, 1.0),
    k_anon_floor: int = 5,
) -> ScoreDistribution:
    """Histogram + summary, with k-anonymity merge applied."""
    edges, counts = compute_histogram(
        qualifying_scores, n_bins=n_bins, range_=score_range,
    )
    edges, counts = merge_for_k_anonymity(edges, counts, floor=k_anon_floor)
    summary = compute_summary(qualifying_scores)
    return ScoreDistribution(bin_edges=edges, bin_counts=counts, summary=summary)


def _build_tier_distribution(
    tier_result: dict[str, Any],
    *,
    pool_size: int,
    tier_split_active: bool,
) -> TierDistribution:
    """Build the public tier_distribution from the artifact's tier_result.

    When `tier_split_active` is False (pool below the floor), per Q14
    all three counts are zero. The emission-share constants are still
    set so the payload is self-describing regardless of pool state.
    """
    if tier_split_active:
        elite_count = len(tier_result.get("elite", []))
        competitive_count = len(tier_result.get("competitive", []))
        participating_count = len(tier_result.get("participating", []))
        elite_floor_met = bool(tier_result.get("elite_floor_cleared", False))
    else:
        elite_count = 0
        competitive_count = 0
        participating_count = 0
        elite_floor_met = False

    def _slice(count: int, emission_share: float) -> TierSlice:
        share = (count / pool_size) if pool_size > 0 else 0.0
        return TierSlice(
            count=count,
            share_of_pool=share,
            share_of_emissions=emission_share,
        )

    return TierDistribution(
        elite=_slice(elite_count, ELITE_EMISSION_SHARE),
        competitive=_slice(competitive_count, COMPETITIVE_EMISSION_SHARE),
        participating=_slice(participating_count, PARTICIPATING_EMISSION_SHARE),
        elite_floor_met=elite_floor_met,
    )


TOP_N_SCORES_MAX = 20


def aggregate(
    artifact: EpochArtifact,
    *,
    n_bins: int = 20,
    score_range: tuple[float, float] = (0.0, 1.0),
    k_anon_floor: int = 5,
    pool_size_floor: int = POOL_SIZE_DISTRIBUTION_FLOOR,
    commentary_markdown: str | None = None,
    top_n: int = TOP_N_SCORES_MAX,
    supersedes: str | None = None,
) -> EpochReportPayload:
    """Aggregate a private artifact into the public payload.

    Args:
        artifact: the operator-private record produced by
            `hope.reporting.epoch_artifact.build_artifact`.
        n_bins: histogram resolution. Default 20 (v2 — finer than v1's 15
            per the §8 Q1 contract follow-up; bins of width 0.05 over
            [0, 1] show distribution shape better for medium pools).
        score_range: histogram domain (default `(0.0, 1.0)` per Q3).
        k_anon_floor: k-anonymity floor (default 5 per contract §3.2).
        pool_size_floor: pool-size threshold below which the histogram
            is omitted and tiers collapse (default 15 per
            `tiered_weights.MIN_MINERS_FOR_TIER_SPLIT`).
        commentary_markdown: optional human commentary. Default None
            for routine epochs (Q20). Operator can override via the
            writer to pre-populate for special epochs.
        top_n: maximum number of ranked top scores to surface in
            ``top_n_scores`` (v2 field). Default 20; capped by the
            schema's ``max_length=20``. Set to 0 to omit.

    Returns:
        A fully-populated `EpochReportPayload` ready to POST.
    """
    qualifying_scores = _qualifying_scores(artifact)
    pool_size = len(qualifying_scores)

    pool_below_floor = pool_size < pool_size_floor
    tier_split_active = not pool_below_floor

    if pool_below_floor:
        score_distribution: ScoreDistribution | None = None
        top_n_scores: list[float] | None = None
    else:
        score_distribution = _build_score_distribution(
            qualifying_scores,
            n_bins=n_bins,
            score_range=score_range,
            k_anon_floor=k_anon_floor,
        )
        # Top-N ranked scores (descending) — payload-only, no UIDs.
        # If the pool is smaller than top_n, the list is just len(pool).
        # If top_n is 0 the field is omitted entirely.
        if top_n > 0:
            ranked = sorted(qualifying_scores, reverse=True)
            top_n_scores = ranked[: min(top_n, len(ranked))]
        else:
            top_n_scores = None

    tier_distribution = _build_tier_distribution(
        artifact.tier_result,
        pool_size=pool_size,
        tier_split_active=tier_split_active,
    )

    # v1 routine emergency state — always false. Q19 freezes this until
    # trigger-state machines land in SN21_REWARD_MECHANISM.md.
    emergency = EmergencyIntervention(triggered=False)

    return EpochReportPayload(
        epoch_id=artifact.epoch_id,
        epoch_type=artifact.epoch_type,
        epoch_subtype=artifact.epoch_subtype,
        block_range_start=artifact.block_range_start or 0,
        block_range_end=artifact.block_range_end or 0,
        scoring_formula_version=artifact.scoring_formula_version,
        scoring_formula_commit=artifact.scoring_formula_commit,
        horizon_set=list(artifact.horizon_set),
        epoch_type_multiplier=artifact.epoch_type_multiplier,
        pool_size=pool_size,
        total_registered_uids=artifact.total_registered_uids,
        pool_size_below_distribution_floor=pool_below_floor,
        baseline_beat_rate=_baseline_beat_rate(qualifying_scores),
        score_distribution=score_distribution,
        tier_distribution=tier_distribution,
        tier_split_active=tier_split_active,
        emergency_intervention=emergency,
        validator_output_snapshot_timestamp=artifact.validator_output_snapshot_timestamp,
        chain_fetch_timestamp=artifact.chain_fetch_timestamp,
        commentary_markdown=commentary_markdown,
        top_n_scores=top_n_scores,
        supersedes=supersedes,
        aggregator_version=2,
    )
