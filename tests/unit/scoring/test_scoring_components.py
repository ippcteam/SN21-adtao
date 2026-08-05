"""Tests for individual scoring components."""

from hope.protocol.outcomes import HorizonOutcome
from hope.protocol.prediction import HorizonPrediction, QuantilePrediction
from hope.scoring.components.base import EpisodeContext
from hope.scoring.components.calibration import Calibration
from hope.scoring.components.directional import DirectionalAccuracy
from hope.scoring.components.goal_accuracy import GoalAccuracy
from hope.scoring.components.quantile_accuracy import QuantileAccuracy


def _make_prediction(cost=0.0, conv=0.0, eff=0.0, spread=5.0, goal_miss=0.0):
    """Helper to create a HorizonPrediction with symmetric spread."""
    return HorizonPrediction(
        cost_delta_pct=QuantilePrediction(p10=cost - spread, p50=cost, p90=cost + spread),
        conversions_delta_pct=QuantilePrediction(p10=conv - spread, p50=conv, p90=conv + spread),
        efficiency_delta_pct=QuantilePrediction(p10=eff - spread, p50=eff, p90=eff + spread),
        goal_miss_probability=goal_miss,
        instability_risk=0.0,
    )


def _make_outcome(cost=0.0, conv=0.0, eff=0.0, goal_miss=0):
    return HorizonOutcome(
        cost_delta_pct=cost,
        conversions_delta_pct=conv,
        efficiency_delta_pct=eff,
        goal_miss=goal_miss,
    )


CTX = EpisodeContext()


class TestQuantileAccuracy:
    def test_perfect_prediction(self):
        pred = _make_prediction(cost=5.0, conv=-3.0, eff=2.0, spread=1.0)
        outcome = _make_outcome(cost=5.0, conv=-3.0, eff=2.0)
        score = QuantileAccuracy().score(pred, outcome, CTX)
        assert score > 0.9, f"Perfect prediction should score > 0.9, got {score}"

    def test_bad_prediction(self):
        pred = _make_prediction(cost=50.0, conv=-50.0, eff=50.0, spread=1.0)
        outcome = _make_outcome(cost=-50.0, conv=50.0, eff=-50.0)
        score = QuantileAccuracy().score(pred, outcome, CTX)
        assert score < 0.3, f"Bad prediction should score low, got {score}"

    def test_zero_prediction_zero_outcome(self):
        pred = _make_prediction(cost=0.0, conv=0.0, eff=0.0, spread=0.5)
        outcome = _make_outcome(cost=0.0, conv=0.0, eff=0.0)
        score = QuantileAccuracy().score(pred, outcome, CTX)
        assert score > 0.8, f"Zero-zero should score well, got {score}"


class TestCalibration:
    def test_narrow_interval_beats_wide(self):
        """Narrower interval should score better than wider when both capture actual."""
        narrow = _make_prediction(cost=5.0, spread=2.0)
        wide = _make_prediction(cost=5.0, spread=20.0)
        outcome = _make_outcome(cost=5.0)
        narrow_score = Calibration().score(narrow, outcome, CTX)
        wide_score = Calibration().score(wide, outcome, CTX)
        assert narrow_score >= wide_score, f"Narrow ({narrow_score}) should beat wide ({wide_score})"

    def test_outcome_outside_interval(self):
        pred = _make_prediction(cost=5.0, spread=1.0)
        outcome = _make_outcome(cost=50.0)
        score = Calibration().score(pred, outcome, CTX)
        assert score < 0.5, f"Outcome far outside should score < 0.5, got {score}"


class TestDirectional:
    def test_correct_direction(self):
        pred = _make_prediction(cost=10.0, conv=5.0, eff=-3.0)
        outcome = _make_outcome(cost=8.0, conv=3.0, eff=-2.0)
        score = DirectionalAccuracy().score(pred, outcome, CTX)
        assert score == 1.0, f"All directions correct should score 1.0, got {score}"

    def test_wrong_direction(self):
        pred = _make_prediction(cost=10.0, conv=5.0, eff=3.0)
        outcome = _make_outcome(cost=-8.0, conv=-3.0, eff=-2.0)
        score = DirectionalAccuracy().score(pred, outcome, CTX)
        assert score == 0.0, f"All directions wrong should score 0.0, got {score}"

    def test_near_zero_outcome(self):
        pred = _make_prediction(cost=10.0)
        outcome = _make_outcome(cost=0.5)  # Near-zero
        score = DirectionalAccuracy().score(pred, outcome, CTX)
        assert 0.3 <= score <= 0.7, f"Near-zero outcome should be ambiguous, got {score}"


class TestGoalAccuracy:
    def test_correct_no_miss(self):
        pred = _make_prediction(goal_miss=0.1)
        outcome = _make_outcome(goal_miss=0)
        score = GoalAccuracy().score(pred, outcome, CTX)
        assert score > 0.95, f"Low miss prob + no miss should score > 0.95, got {score}"

    def test_correct_miss(self):
        pred = _make_prediction(goal_miss=0.9)
        outcome = _make_outcome(goal_miss=1)
        score = GoalAccuracy().score(pred, outcome, CTX)
        assert score > 0.95, f"High miss prob + miss should score > 0.95, got {score}"

    def test_wrong_miss(self):
        pred = _make_prediction(goal_miss=0.0)
        outcome = _make_outcome(goal_miss=1)
        score = GoalAccuracy().score(pred, outcome, CTX)
        assert score == 0.0, f"Zero miss prob + miss should score 0.0, got {score}"
