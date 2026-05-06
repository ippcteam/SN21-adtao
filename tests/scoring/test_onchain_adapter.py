"""Unit tests for hope/scoring/onchain_adapter.py."""

from __future__ import annotations

import os

import pytest

from hope.scoring.onchain_adapter import (
    HorizonTruth,
    _median,
    _mean,
    aggregate_outcomes_to_truth,
    bundle_to_predictions,
    compute_scoring_inputs_hash,
    make_scorer,
    score_one_miner,
    wrap_epoch_scorer,
)


def _plaintext(epoch_id="EP-A", hk=None, cost_q=(-50, 0, 50), conv_q=(-30, 10, 50),
               eff_q=(-20, 5, 40), miss_p=100_000, instab_p=50_000):
    return {
        "v": 1,
        "epoch_id": epoch_id,
        "miner_hotkey": hk or os.urandom(32),
        "submitted_round": 100,
        "horizons": [{
            "h": "7",
            "cost_q": list(cost_q),
            "conv_q": list(conv_q),
            "eff_q": list(eff_q),
            "miss_p": miss_p,
            "instab_p": instab_p,
        }],
        "inner_sig": b"\x00" * 64,
    }


@pytest.fixture
def truth():
    return {
        "7": HorizonTruth(
            horizon="7",
            truth_cost_p50_dpct=0,
            truth_conv_p50_dpct=0,
            truth_eff_p50_dpct=0,
            goal_miss_freq_ppm=100_000,
            instab_freq_ppm=50_000,
        ),
    }


class TestScoreOneMiner:
    def test_perfect_centered_prediction_high_score(self, truth):
        plain = _plaintext(cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                           miss_p=100_000, instab_p=50_000)
        s = score_one_miner(plain, truth)
        assert s >= 990_000  # near-perfect

    def test_far_off_prediction_low_score(self, truth):
        plain = _plaintext(cost_q=(500, 500, 500), conv_q=(500, 500, 500),
                           eff_q=(500, 500, 500), miss_p=900_000, instab_p=900_000)
        s = score_one_miner(plain, truth)
        assert s < 500_000

    def test_no_horizons_returns_zero(self, truth):
        plain = _plaintext()
        plain["horizons"] = []
        assert score_one_miner(plain, truth) == 0

    def test_unknown_horizon_in_truth_gets_zero(self, truth):
        plain = _plaintext()
        plain["horizons"] = [{**plain["horizons"][0], "h": "30"}]
        assert score_one_miner(plain, truth) == 0

    def test_score_is_in_micro_units_range(self, truth):
        plain = _plaintext()
        s = score_one_miner(plain, truth)
        assert 0 <= s <= 1_000_000


class TestMakeScorer:
    def test_round_trip_with_make_scorer(self, truth):
        scorer = make_scorer(truth)
        hk1, hk2 = os.urandom(32), os.urandom(32)
        plaintexts = {
            hk1: _plaintext(hk=hk1, cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0)),
            hk2: _plaintext(hk=hk2, cost_q=(500, 500, 500), conv_q=(500, 500, 500),
                            eff_q=(500, 500, 500)),
        }
        scores = scorer("EP-A", plaintexts)
        assert set(scores.keys()) == {hk1, hk2}
        assert scores[hk1] > scores[hk2]

    def test_empty_input_empty_output(self, truth):
        scorer = make_scorer(truth)
        assert scorer("EP-A", {}) == {}


class TestComputeScoringInputsHash:
    def test_deterministic(self, truth):
        plaintexts = {b"\x42" * 32: _plaintext(hk=b"\x42" * 32)}
        h1 = compute_scoring_inputs_hash(
            epoch_id="EP-A", plaintexts=plaintexts, truth_by_horizon=truth,
        )
        h2 = compute_scoring_inputs_hash(
            epoch_id="EP-A", plaintexts=plaintexts, truth_by_horizon=truth,
        )
        assert h1 == h2
        assert len(h1) == 32

    def test_changing_plaintext_changes_hash(self, truth):
        hk = b"\x42" * 32
        a = compute_scoring_inputs_hash(
            epoch_id="EP-A",
            plaintexts={hk: _plaintext(hk=hk, cost_q=(0, 0, 0))},
            truth_by_horizon=truth,
        )
        b = compute_scoring_inputs_hash(
            epoch_id="EP-A",
            plaintexts={hk: _plaintext(hk=hk, cost_q=(99, 99, 99))},
            truth_by_horizon=truth,
        )
        assert a != b

    def test_changing_epoch_id_changes_hash(self, truth):
        hk = b"\x42" * 32
        a = compute_scoring_inputs_hash(
            epoch_id="EP-A", plaintexts={hk: _plaintext(hk=hk)},
            truth_by_horizon=truth,
        )
        b = compute_scoring_inputs_hash(
            epoch_id="EP-B", plaintexts={hk: _plaintext(hk=hk)},
            truth_by_horizon=truth,
        )
        assert a != b

    def test_changing_truth_changes_hash(self, truth):
        hk = b"\x42" * 32
        a = compute_scoring_inputs_hash(
            epoch_id="EP-A",
            plaintexts={hk: _plaintext(hk=hk)},
            truth_by_horizon=truth,
        )
        # Same logical truth but with different field values
        modified = {
            "7": HorizonTruth(
                horizon="7",
                truth_cost_p50_dpct=99,
                truth_conv_p50_dpct=truth["7"].truth_conv_p50_dpct,
                truth_eff_p50_dpct=truth["7"].truth_eff_p50_dpct,
                goal_miss_freq_ppm=truth["7"].goal_miss_freq_ppm,
                instab_freq_ppm=truth["7"].instab_freq_ppm,
            ),
        }
        b = compute_scoring_inputs_hash(
            epoch_id="EP-A",
            plaintexts={hk: _plaintext(hk=hk)},
            truth_by_horizon=modified,
        )
        assert a != b


class TestAggregateOutcomesToTruth:
    def test_basic(self):
        eps = [
            {"7": {"cost_delta_pct": -3.0, "conversions_delta_pct": 5.0,
                   "efficiency_delta_pct": 2.0, "goal_miss": 0}},
            {"7": {"cost_delta_pct": -5.0, "conversions_delta_pct": 7.0,
                   "efficiency_delta_pct": 3.0, "goal_miss": 1}},
            {"7": {"cost_delta_pct": -4.0, "conversions_delta_pct": 6.0,
                   "efficiency_delta_pct": 2.5, "goal_miss": 0}},
        ]
        truth = aggregate_outcomes_to_truth(eps)
        assert "7" in truth
        # median of -3, -4, -5 = -4 → ×10 = -40
        assert truth["7"].truth_cost_p50_dpct == -40
        # median of 5, 6, 7 = 6 → ×10 = 60
        assert truth["7"].truth_conv_p50_dpct == 60
        # mean of [0, 1, 0] = 1/3 → ppm
        assert 333_000 <= truth["7"].goal_miss_freq_ppm <= 334_000

    def test_two_horizons(self):
        eps = [
            {"7": {"cost_delta_pct": 1.0, "conversions_delta_pct": 1.0,
                   "efficiency_delta_pct": 1.0, "goal_miss": 0},
             "14": {"cost_delta_pct": 2.0, "conversions_delta_pct": 2.0,
                    "efficiency_delta_pct": 2.0, "goal_miss": 1}},
        ]
        truth = aggregate_outcomes_to_truth(eps)
        assert set(truth.keys()) == {"7", "14"}
        assert truth["7"].truth_cost_p50_dpct == 10
        assert truth["14"].truth_cost_p50_dpct == 20

    def test_empty_input(self):
        assert aggregate_outcomes_to_truth([]) == {}


class TestCRPSScorer:
    """Verify the CRPS-based scorer's monotonicity + bounds."""

    def test_perfect_prediction_perfect_score(self, truth):
        """Prediction with P50 == truth, tight band → near-perfect score."""
        plain = _plaintext(cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                           miss_p=100_000, instab_p=50_000)
        s = score_one_miner(plain, truth)
        assert s >= 980_000

    def test_score_monotonically_decreasing_with_error(self, truth):
        """Larger P50 distance from truth → lower score."""
        results = []
        for offset in (10, 50, 200, 500):
            plain = _plaintext(
                cost_q=(offset - 30, offset, offset + 30),
                conv_q=(offset - 30, offset, offset + 30),
                eff_q=(offset - 30, offset, offset + 30),
            )
            results.append(score_one_miner(plain, truth))
        # Each successive score is no greater than the previous
        for a, b in zip(results, results[1:]):
            assert b <= a

    def test_wide_band_correct_p50_better_than_far_off(self, truth):
        """Wide-band prediction with correct P50 beats narrow far-off."""
        wide = _plaintext(cost_q=(-100, 0, 100), conv_q=(-50, 0, 50),
                          eff_q=(-50, 0, 50))
        far = _plaintext(cost_q=(295, 300, 305), conv_q=(195, 200, 205),
                         eff_q=(195, 200, 205))
        assert score_one_miner(wide, truth) > score_one_miner(far, truth)

    def test_score_in_unit_micro_range(self, truth):
        for offset in range(-1000, 1000, 100):
            plain = _plaintext(cost_q=(offset, offset, offset))
            s = score_one_miner(plain, truth)
            assert 0 <= s <= 1_000_000


class TestBundleToPredictions:
    def test_round_trip(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from hope.commitment.episode_artifacts import (
            PerEpisodeEntry,
            build_per_episode_bundle,
            parse_per_episode_bundle,
        )

        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        entries = [
            PerEpisodeEntry(
                episode_id=f"EP-{i:03d}", horizon=h,
                cost_q=(-3.0, 0.0, 3.0), conv_q=(-1.0, 1.0, 3.0),
                eff_q=(-1.0, 0.5, 2.5),
                goal_miss_probability=0.1, instability_risk=0.05,
            )
            for i in range(3)
            for h in ("7", "14")
        ]
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        preds = bundle_to_predictions(decoded)
        # 3 episodes × 1 Prediction each (with 2 horizons inside)
        assert len(preds) == 3
        assert all(set(p.horizons.keys()) == {"7", "14"} for p in preds)
        # Quantile reverse-scaling
        sample = preds[0].horizons["7"].cost_delta_pct
        assert sample.p10 == -3.0
        assert sample.p50 == 0.0
        assert sample.p90 == 3.0


class TestWrapEpochScorer:
    def test_scores_via_real_epoch_scorer(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from hope.commitment.episode_artifacts import (
            PerEpisodeEntry,
            build_per_episode_bundle,
            parse_per_episode_bundle,
        )
        from hope.protocol.episode import (
            AccountState, Episode, EpisodeMetadata,
        )
        from hope.protocol.outcomes import HorizonOutcome, Outcome
        from hope.scoring.scorer import EpochScorer

        # Build 3 miners + their bundles
        miners = []
        for _ in range(3):
            sk = Ed25519PrivateKey.generate()
            pk = sk.public_key().public_bytes_raw()
            entries = [
                PerEpisodeEntry(
                    episode_id=f"EP-{i:03d}", horizon="7",
                    cost_q=(-3.0, 0.0, 3.0), conv_q=(-1.0, 1.0, 3.0),
                    eff_q=(-1.0, 0.5, 2.5),
                    goal_miss_probability=0.1, instability_risk=0.05,
                )
                for i in range(2)
            ]
            blob = build_per_episode_bundle(
                epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
                entries=entries, miner_signing_key=sk,
            )
            miners.append((pk, parse_per_episode_bundle(blob)))

        episodes = [
            Episode(
                episode_metadata=EpisodeMetadata(episode_id=f"EP-{i:03d}"),
                account_state=AccountState(customer_id_hash=f"C-{i}"),
            )
            for i in range(2)
        ]
        outcomes = [
            Outcome(
                episode_id=f"EP-{i:03d}",
                t7=HorizonOutcome(
                    cost_delta_pct=0.5, conversions_delta_pct=1.0,
                    efficiency_delta_pct=0.6, goal_miss=0,
                ),
            )
            for i in range(2)
        ]

        bundles = {pk: bundle for pk, bundle in miners}
        scorer = wrap_epoch_scorer(
            EpochScorer(),
            episodes=episodes,
            outcomes=outcomes,
            bundles_by_miner=bundles,
        )

        # The aggregated plaintexts dict tells the scorer which miners passed
        # scoreability. Pass all three; the wrapper looks up bundles by the
        # same hotkeys.
        agg = {pk: {"v": 1, "epoch_id": "EPOCH-A"} for pk, _ in miners}
        scores = scorer("EPOCH-A", agg)
        assert set(scores.keys()) == {pk for pk, _ in miners}
        assert all(0 <= v <= 1_000_000 for v in scores.values())

    def test_excluded_miner_skipped(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from hope.commitment.episode_artifacts import (
            PerEpisodeEntry,
            build_per_episode_bundle,
            parse_per_episode_bundle,
        )
        from hope.protocol.episode import (
            AccountState, Episode, EpisodeMetadata,
        )
        from hope.protocol.outcomes import HorizonOutcome, Outcome
        from hope.scoring.scorer import EpochScorer

        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        entries = [PerEpisodeEntry(
            episode_id="EP-1", horizon="7",
            cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
            goal_miss_probability=0.1, instability_risk=0.0,
        )]
        bundle = parse_per_episode_bundle(build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        ))
        scorer = wrap_epoch_scorer(
            EpochScorer(),
            episodes=[Episode(
                episode_metadata=EpisodeMetadata(episode_id="EP-1"),
                account_state=AccountState(customer_id_hash="C"),
            )],
            outcomes=[Outcome(
                episode_id="EP-1",
                t7=HorizonOutcome(
                    cost_delta_pct=0, conversions_delta_pct=0,
                    efficiency_delta_pct=0, goal_miss=0,
                ),
            )],
            bundles_by_miner={pk: bundle},
        )
        # Pass an EMPTY aggregated plaintexts dict — every miner is "excluded".
        scores = scorer("EPOCH-A", {})
        assert scores == {}


class TestHelpers:
    def test_median_odd(self):
        assert _median([1, 2, 3]) == 2

    def test_median_even(self):
        assert _median([1, 2, 3, 4]) == 2.5

    def test_median_empty(self):
        assert _median([]) == 0.0

    def test_mean(self):
        assert _mean([1, 2, 3, 4]) == 2.5

    def test_mean_empty(self):
        assert _mean([]) == 0.0
