"""Directional Accuracy — binary sign match (15% weight).

Measures whether the miner predicted the correct direction of change.

Formula:
    1.0 if sign(p50) == sign(actual)
    0.0 if sign(p50) != sign(actual) and |actual| > threshold
    0.5 if |actual| <= threshold (near-zero, ambiguous)
"""

from __future__ import annotations


from hope.constants import DIRECTIONAL_NEAR_ZERO_THRESHOLD
from hope.protocol.prediction import HorizonPrediction
from hope.protocol.outcomes import HorizonOutcome
from hope.scoring.components.base import EpisodeContext, ScoringComponent


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


class DirectionalAccuracy(ScoringComponent):
    """Binary sign match with near-zero tolerance."""

    def _score_metric(self, actual: float, p50: float) -> float:
        """Score directional accuracy for a single metric."""
        if abs(actual) <= DIRECTIONAL_NEAR_ZERO_THRESHOLD:
            return 0.5  # Ambiguous — near-zero actual
        if _sign(p50) == _sign(actual):
            return 1.0
        return 0.0

    def score(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> float:
        """Score directional accuracy across all three metrics.

        Returns value in [0.0, 1.0] where 1.0 is perfect.
        """
        d_cost = self._score_metric(outcome.cost_delta_pct, prediction.cost_delta_pct.p50)
        d_conv = self._score_metric(outcome.conversions_delta_pct, prediction.conversions_delta_pct.p50)
        d_eff = self._score_metric(outcome.efficiency_delta_pct, prediction.efficiency_delta_pct.p50)

        return (d_cost + d_conv + d_eff) / 3.0
