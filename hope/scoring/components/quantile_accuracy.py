"""Quantile Accuracy — pinball loss + CRPS approximation (50% weight).

Measures how well the P10/P50/P90 quantile predictions match the actual
outcome across cost_delta_pct, conversions_delta_pct, and efficiency_delta_pct.

Formula:
    L_tau(actual, predicted) = tau * max(actual - predicted, 0)
                              + (1-tau) * max(predicted - actual, 0)

    normalized_CRPS = (1/3) * sum_metric[(2/3) * (L_0.10 + L_0.50 + L_0.90) / scale_metric]
    quantile_accuracy = 1.0 - normalized_CRPS
"""

from __future__ import annotations

from hope.protocol.prediction import HorizonPrediction, QuantilePrediction
from hope.protocol.outcomes import HorizonOutcome
from hope.scoring.components.base import EpisodeContext, ScoringComponent


class QuantileAccuracy(ScoringComponent):
    """Pinball loss + CRPS scoring for quantile predictions."""

    def pinball_loss(self, actual: float, predicted: float, tau: float) -> float:
        """Compute pinball loss for a single quantile."""
        diff = actual - predicted
        if diff >= 0:
            return tau * diff
        return (1 - tau) * (-diff)

    def crps_approximation(self, actual: float, p10: float, p50: float, p90: float) -> float:
        """Approximate CRPS from three quantile predictions."""
        l10 = self.pinball_loss(actual, p10, 0.10)
        l50 = self.pinball_loss(actual, p50, 0.50)
        l90 = self.pinball_loss(actual, p90, 0.90)
        scale = max(abs(actual), 1.0)
        return (2.0 / 3.0) * (l10 + l50 + l90) / scale

    def _score_metric(self, actual: float, quantiles: QuantilePrediction) -> float:
        """Score a single metric's quantile prediction."""
        return self.crps_approximation(actual, quantiles.p10, quantiles.p50, quantiles.p90)

    def score(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> float:
        """Score quantile accuracy across all three metrics.

        Returns value in [0.0, 1.0] where 1.0 is perfect.
        """
        crps_cost = self._score_metric(outcome.cost_delta_pct, prediction.cost_delta_pct)
        crps_conv = self._score_metric(outcome.conversions_delta_pct, prediction.conversions_delta_pct)
        crps_eff = self._score_metric(outcome.efficiency_delta_pct, prediction.efficiency_delta_pct)

        normalized_crps = (crps_cost + crps_conv + crps_eff) / 3.0
        return max(0.0, min(1.0, 1.0 - normalized_crps))
