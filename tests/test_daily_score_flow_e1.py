"""E1 daily score-flow — pins the entry semantics that make D13 hold:
per-(episode,horizon) entries on finalisation day, blend weights summing
to 1.0 per episode, nothing rescored, weighted evidence mass."""
from datetime import date

import pytest

from hope.scoring.daily_score_flow import (
    DAILY_STREAM_HORIZON_WEIGHTS,
    HorizonResult,
    day_flow,
    episode_total_weight,
    horizon_entry_weight,
)
from hope.scoring.episode_average import (
    ScoredEpisode,
    episode_weighted_average,
    scored_prediction_count,
)

D = date(2026, 9, 1)


class TestBlendWeights:
    def test_every_resolution_sums_to_one(self):
        for res in DAILY_STREAM_HORIZON_WEIGHTS:
            assert episode_total_weight(res) == pytest.approx(1.0), res

    def test_unknown_horizon_is_an_error_not_silent(self):
        with pytest.raises(ValueError):
            horizon_entry_weight(21)

    def test_unknown_resolution_falls_back_to_high(self):
        assert horizon_entry_weight(7, "weird") == horizon_entry_weight(7, "high")


class TestDayFlow:
    def test_only_todays_finalisations_enter(self):
        rs = [
            HorizonResult("e1", 7, "m1", 0.8, D),
            HorizonResult("e1", 14, "m1", 0.7, date(2026, 9, 8)),   # future day
            HorizonResult("e0", 28, "m1", 0.6, date(2026, 8, 31)),  # yesterday
        ]
        entries = day_flow(rs, D)
        assert len(entries) == 1
        assert entries[0].horizon_days == 7
        assert entries[0].weight == pytest.approx(0.30)

    def test_one_episode_three_days_total_weight_one(self):
        rs = [
            HorizonResult("e1", 7, "m1", 0.8, date(2026, 9, 1)),
            HorizonResult("e1", 14, "m1", 0.7, date(2026, 9, 8)),
            HorizonResult("e1", 28, "m1", 0.6, date(2026, 9, 22)),
        ]
        total = sum(
            e.weight
            for d in (date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 22))
            for e in day_flow(rs, d)
        )
        assert total == pytest.approx(1.0)


class TestWeightedStanding:
    def test_weighted_entries_feed_average(self):
        eps = [
            ScoredEpisode(score=0.9, scored_on=D, weight=0.30),   # 7d entry
            ScoredEpisode(score=0.5, scored_on=D, weight=0.40),   # 14d entry
        ]
        avg = episode_weighted_average(eps, D)
        assert avg == pytest.approx((0.9 * 0.30 + 0.5 * 0.40) / 0.70)

    def test_evidence_mass_counts_weights_not_rows(self):
        eps = [ScoredEpisode(0.5, D, 0.30), ScoredEpisode(0.5, D, 0.40),
               ScoredEpisode(0.5, D, 0.30)]
        assert scored_prediction_count(eps, D) == pytest.approx(1.0)

    def test_legacy_unweighted_entries_unchanged(self):
        eps = [ScoredEpisode(0.5, D), ScoredEpisode(0.7, D)]
        assert scored_prediction_count(eps, D) == pytest.approx(2)
        assert episode_weighted_average(eps, D) == pytest.approx(0.6)
