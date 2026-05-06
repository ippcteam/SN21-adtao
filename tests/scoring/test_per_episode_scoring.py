"""Per-episode scoring (Phase E path).

The on-chain plaintext binds an `episodes_root`; the off-chain bundle
carries per-episode quantile triples. `score_one_miner_per_episode`
takes those entries and per-(episode, horizon) ground truth and
returns the same micro-units score shape as the legacy aggregate
scorer.
"""

from __future__ import annotations

from hope.commitment.episode_artifacts import (
    PerEpisodeEntry,
    build_per_episode_entry,
)
from hope.scoring.onchain_adapter import (
    EpisodeTruth,
    score_one_miner_per_episode,
)


def _entry(ep_id: str, horizon: str, p50_cost: float = 0.0):
    return build_per_episode_entry(
        PerEpisodeEntry(
            episode_id=ep_id,
            horizon=horizon,
            cost_q=(p50_cost - 5.0, p50_cost, p50_cost + 5.0),
            conv_q=(-3.0, 0.0, 3.0),
            eff_q=(-2.0, 0.0, 2.0),
            goal_miss_probability=0.5,
            instability_risk=0.2,
        )
    )


def _truth(ep_id: str, horizon: str, cost_p50_dpct: int):
    return EpisodeTruth(
        episode_id=ep_id,
        horizon=horizon,
        truth_cost_p50_dpct=cost_p50_dpct,
        truth_conv_p50_dpct=0,
        truth_eff_p50_dpct=0,
        goal_miss_ppm=500_000,
        instab_ppm=200_000,
    )


def test_per_episode_perfect_score_when_p50_matches_truth():
    entries = [_entry("EP-001", "7", p50_cost=0.0)]
    truth = {("EP-001", "7"): _truth("EP-001", "7", cost_p50_dpct=0)}
    score = score_one_miner_per_episode(entries, truth)
    # P50 hits truth, P10/P90 wrap symmetrically — strong but not 1.0
    # because conv/eff also contribute and probability matches truth exactly
    # (Brier=0). Expect a high score.
    assert score >= 800_000


def test_per_episode_zero_when_no_truth_overlap():
    entries = [_entry("EP-001", "7")]
    truth = {("EP-002", "7"): _truth("EP-002", "7", cost_p50_dpct=0)}
    score = score_one_miner_per_episode(entries, truth)
    # No overlap → 0
    assert score == 0


def test_per_episode_averages_across_episodes_and_horizons():
    entries = [
        _entry("EP-001", "7", p50_cost=0.0),     # P50 hits truth
        _entry("EP-002", "14", p50_cost=200.0),  # P50 way off from truth
    ]
    truth = {
        ("EP-001", "7"): _truth("EP-001", "7", cost_p50_dpct=0),
        ("EP-002", "14"): _truth("EP-002", "14", cost_p50_dpct=0),
    }
    perfect_only = score_one_miner_per_episode(
        entries[:1], {("EP-001", "7"): truth[("EP-001", "7")]}
    )
    averaged = score_one_miner_per_episode(entries, truth)
    # The weak entry pulls the average down vs the perfect-only baseline.
    assert averaged < perfect_only
    assert averaged > 0


def test_empty_entries_returns_zero():
    assert score_one_miner_per_episode([], {}) == 0


def test_skips_entries_outside_truth():
    entries = [
        _entry("EP-001", "7", p50_cost=0.0),
        _entry("EP-002", "7", p50_cost=0.0),  # no truth → skipped
    ]
    truth = {("EP-001", "7"): _truth("EP-001", "7", cost_p50_dpct=0)}
    score = score_one_miner_per_episode(entries, truth)
    # Equivalent to the single-episode case; truth-less entries are
    # skipped, not zeroed.
    assert score >= 800_000
