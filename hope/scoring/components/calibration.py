"""Calibration — interval score with convex width penalty (20% weight).

Measures whether the P10-P90 interval captures the actual outcome
while penalizing excessively wide intervals.

Formula:
    IS = (p90 - p10)^1.3 + 2.5 * max(p10 - actual, 0) + 2.5 * max(actual - p90, 0)

Width exponent 1.3 penalizes very wide intervals super-linearly.
Miss penalty multiplier 2/alpha where alpha=0.80 gives 2.5x.
For low measurement_resolution episodes: miss penalty reduced 50%.
"""

from __future__ import annotations

from hope.constants import (
    CALIBRATION_LOW_RES_REDUCTION,
    CALIBRATION_MISS_MULTIPLIER,
    CALIBRATION_WIDTH_EXPONENT,
)
from hope.protocol.outcomes import HorizonOutcome
from hope.protocol.prediction import HorizonPrediction, QuantilePrediction
from hope.scoring.components.base import EpisodeContext, ScoringComponent


class Calibration(ScoringComponent):
    """Interval Score with convex width penalty."""

    def interval_score(
        self,
        actual: float,
        p10: float,
        p90: float,
        low_res: bool = False,
    ) -> float:
        """Compute interval score for a single metric."""
        width = max(p90 - p10, 0.0)
        width_penalty = width ** CALIBRATION_WIDTH_EXPONENT

        miss_mult = CALIBRATION_MISS_MULTIPLIER
        if low_res:
            miss_mult *= CALIBRATION_LOW_RES_REDUCTION

        lower_miss = miss_mult * max(p10 - actual, 0.0)
        upper_miss = miss_mult * max(actual - p90, 0.0)

        return width_penalty + lower_miss + upper_miss

    def _score_metric(self, actual: float, quantiles: QuantilePrediction, low_res: bool) -> float:
        """Score calibration for a single metric."""
        raw = self.interval_score(actual, quantiles.p10, quantiles.p90, low_res)
        scale = max(abs(actual), 1.0)
        normalized = raw / scale
        return normalized

    def score(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> float:
        """Score calibration across all three metrics.

        Returns value in [0.0, 1.0] where 1.0 is perfect.
        """
        low_res = context.measurement_resolution == "low"

        metric_scores = [
            self._score_metric(outcome.cost_delta_pct, prediction.cost_delta_pct, low_res),
            self._score_metric(outcome.conversions_delta_pct, prediction.conversions_delta_pct, low_res),
        ]
        if outcome.efficiency_delta_pct is not None:
            metric_scores.append(
                self._score_metric(outcome.efficiency_delta_pct, prediction.efficiency_delta_pct, low_res)
            )

        avg_is = sum(metric_scores) / len(metric_scores)
        return max(0.0, min(1.0, 1.0 - avg_is))
