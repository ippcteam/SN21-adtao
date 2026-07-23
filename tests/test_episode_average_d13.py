"""D13 episode-weighted average — pins the semantics the amendment names.

Key guards:
- exact half-life (weight halves at H days; 7 daily-equivalent folds ≠ used)
- episode weighting: thin days self-discount proportionally
- the REJECTED day-fold form provably differs on a thin-day fixture
- window drop, no-standing-vs-zero distinction, cold-start floors
"""
from datetime import date, timedelta

import pytest

from hope.scoring.episode_average import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_WINDOW_DAYS,
    PLACEMENT_FLOOR_PREDICTIONS,
    ScoredEpisode,
    episode_weight,
    episode_weighted_average,
    scored_prediction_count,
    standing,
)

TODAY = date(2026, 8, 1)


def _eps(day_scores):
    """day_scores: list of (days_ago, [scores])"""
    out = []
    for days_ago, scores in day_scores:
        d = TODAY - timedelta(days=days_ago)
        out += [ScoredEpisode(score=s, scored_on=d) for s in scores]
    return out


class TestWeight:
    def test_age_zero_is_full_weight(self):
        assert episode_weight(0) == 1.0

    def test_half_life_exact(self):
        assert episode_weight(DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.5)

    def test_two_half_lives_quarter(self):
        assert episode_weight(2 * DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.25)

    def test_negative_age_rejected(self):
        with pytest.raises(ValueError):
            episode_weight(-1)


class TestAverage:
    def test_single_day_average_is_plain_mean(self):
        eps = _eps([(0, [0.2, 0.4, 0.9])])
        assert episode_weighted_average(eps, TODAY) == pytest.approx(0.5)

    def test_thin_day_self_discounts_proportionally(self):
        # 250 episodes score 0.8 yesterday; 40 episodes score 0.2 today.
        # Episode weighting: today's 40 shift the average only ~40/(40+250*decay).
        eps = _eps([(1, [0.8] * 250), (0, [0.2] * 40)])
        avg = episode_weighted_average(eps, TODAY)
        w1 = episode_weight(1)
        expected = (40 * 0.2 + 250 * w1 * 0.8) / (40 + 250 * w1)
        assert avg == pytest.approx(expected)
        assert avg > 0.7  # the thin bad day barely moves it

    def test_rejected_day_fold_form_differs(self):
        # Day-fold (average per day, then weight days equally) would give
        # ~(0.2 + 0.8)/2 = 0.5-ish; episode weighting stays near 0.8.
        eps = _eps([(1, [0.8] * 250), (0, [0.2] * 40)])
        episode_form = episode_weighted_average(eps, TODAY)
        w1 = episode_weight(1)
        day_fold_form = (0.2 * 1 + 0.8 * w1) / (1 + w1)
        assert abs(episode_form - day_fold_form) > 0.2, \
            "episode weighting must be materially different from day-folding on thin days"

    def test_window_drop(self):
        eps = _eps([(DEFAULT_WINDOW_DAYS + 1, [1.0] * 500), (0, [0.4])])
        assert episode_weighted_average(eps, TODAY) == pytest.approx(0.4)

    def test_no_episodes_in_window_returns_none_not_zero(self):
        eps = _eps([(DEFAULT_WINDOW_DAYS + 5, [0.9])])
        assert episode_weighted_average(eps, TODAY) is None

    def test_future_scored_episodes_ignored(self):
        eps = [ScoredEpisode(score=1.0, scored_on=TODAY + timedelta(days=1)),
               ScoredEpisode(score=0.3, scored_on=TODAY)]
        assert episode_weighted_average(eps, TODAY) == pytest.approx(0.3)

    def test_single_strong_day_moves_average_under_promotion_margin(self):
        # GAP-2 v2 §3.2: at ~250/day steady state, one perfect day moves the
        # average by less than the 5% D8 promotion margin.
        steady = [(d, [0.5] * 250) for d in range(1, 29)]
        base = episode_weighted_average(_eps(steady), TODAY)
        spiked = episode_weighted_average(_eps(steady + [(0, [1.0] * 250)]), TODAY)
        assert spiked - base < 0.05


class TestColdStart:
    def test_placement_floor(self):
        eps = _eps([(0, [0.6] * (PLACEMENT_FLOOR_PREDICTIONS - 1))])
        st = standing(eps, TODAY)
        assert st["placement_eligible"] is False
        st2 = standing(_eps([(0, [0.6] * PLACEMENT_FLOOR_PREDICTIONS)]), TODAY)
        assert st2["placement_eligible"] is True

    def test_full_standing_floor(self):
        st = standing(_eps([(0, [0.6] * 999)]), TODAY)
        assert st["full_standing"] is False
        st2 = standing(_eps([(1, [0.6] * 600), (0, [0.6] * 400)]), TODAY)
        assert st2["full_standing"] is True

    def test_count_respects_window(self):
        eps = _eps([(DEFAULT_WINDOW_DAYS + 1, [0.9] * 300), (0, [0.9] * 10)])
        assert scored_prediction_count(eps, TODAY) == 10
