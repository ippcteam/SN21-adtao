"""Simplified tier computation for the leaderboard reporter.

The Reward Mechanism's full `TieredAllocator` (see `hope/validator/
tiered_weights.py`) wants per-miner inputs richer than the on-chain
scorer produces — specifically `baseline_score` and per-bucket coverage.
For v1, the on-chain `score_map` (`miner_hotkey → score_micro`) is the
only data available at scoring time. This module wraps the allocator
with documented simplifying assumptions so a tier result can be
computed alongside the on-chain commit sequence without rewriting the
chain-scoring path.

Simplifying assumptions in v1:
  * `raw_score = score_micro / 1_000_000` — direct rescale.
  * `baseline_score = 0.0` — any positive `raw_score` clears the gate.
    Real conditional-prior baselines are tied to per-episode scoring,
    which the on-chain scorer does not produce. Phase 1's richer
    artifact-write path replaces this default with the per-episode
    conditional prior when available.
  * `coverage_fraction = 1.0` — same reason. Phase 1 sources real
    coverage from `MinerScore.coverage_fraction`.
  * `per_bucket_coverage = {}` — Phase 1 ships only the
    SEARCH/CAMPAIGN bucket so the gate check degenerates to "submit
    on at least 80% of episodes overall" already enforced by the
    on-chain scoreability rule.

The output `TierAllocationResult` is structurally identical to what
the production tiered emission path produces — when the leaderboard
graduates to driving emissions, the same shape flows through both
surfaces.
"""

from __future__ import annotations

from hope.validator.tiered_weights import (
    MinerEpochInputs,
    TierAllocationResult,
    TieredAllocator,
)


def compute_tier_result_from_score_map(
    score_map: dict[bytes, int],
    *,
    baseline_score: float = 0.0,
    coverage_fraction: float = 1.0,
    allocator: TieredAllocator | None = None,
) -> TierAllocationResult:
    """Run `TieredAllocator.allocate()` against the on-chain score_map.

    Args:
        score_map: `miner_hotkey_bytes → score_micro_int` exactly as
            produced by the on-chain scorer (`hope/scoring/onchain_adapter.py`).
        baseline_score: gate threshold. Default 0.0 means any positive
            raw_score passes the conditional-prior gate. Override with
            the conditional-prior baseline once Phase 1 sources it.
        coverage_fraction: per-miner coverage default. Default 1.0
            means every miner counts as fully covered. Override
            per-miner when richer data is available.
        allocator: optional injected `TieredAllocator` (used in tests
            to seed EMA history). Default constructs a fresh allocator,
            i.e. cold-start single-epoch placement.

    Returns:
        A `TierAllocationResult` with `weights`, tier rosters, and the
        elite-floor flag. Empty `score_map` returns an empty result
        (no qualifying miners) without raising.
    """
    inputs = [
        MinerEpochInputs(
            hotkey=hk.hex(),
            raw_score=v / 1_000_000.0,
            baseline_score=baseline_score,
            coverage_fraction=coverage_fraction,
        )
        for hk, v in score_map.items()
    ]
    if allocator is None:
        allocator = TieredAllocator()
    return allocator.allocate(inputs)
