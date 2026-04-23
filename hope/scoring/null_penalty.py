"""Null Penalty — graduated penalty for near-zero predictions.

Prevents miners from gaming the system by predicting near-zero for
every episode (which would be directionally correct ~50% of the time).

near_zero_fraction = count(|p50| < 1.0 for all metrics) / total_episodes
penalty = max(0, (near_zero_fraction - 0.40) / 0.45) * 0.60
final_score *= (1.0 - penalty)

Ramp: 40% near-zero is free, 85%+ gets maximum 60% penalty.
"""

from __future__ import annotations

from hope.constants import (
    NEAR_ZERO_THRESHOLD,
    NULL_PENALTY_MAX,
    NULL_PENALTY_RAMP_END,
    NULL_PENALTY_RAMP_START,
)
from hope.protocol.prediction import Prediction


class NullPenalty:
    """Graduated near-zero prediction penalty."""

    def is_near_zero(self, prediction: Prediction) -> bool:
        """Check if a prediction is near-zero across all metrics and horizons."""
        for horizon_pred in prediction.horizons.values():
            if abs(horizon_pred.cost_delta_pct.p50) >= NEAR_ZERO_THRESHOLD:
                return False
            if abs(horizon_pred.conversions_delta_pct.p50) >= NEAR_ZERO_THRESHOLD:
                return False
            if abs(horizon_pred.efficiency_delta_pct.p50) >= NEAR_ZERO_THRESHOLD:
                return False
        return True

    def compute_near_zero_fraction(self, predictions: list[Prediction]) -> float:
        """Fraction of predictions that are near-zero."""
        if not predictions:
            return 0.0
        near_zero_count = sum(1 for p in predictions if self.is_near_zero(p))
        return near_zero_count / len(predictions)

    def compute_penalty(self, near_zero_fraction: float) -> float:
        """Compute the penalty multiplier from near-zero fraction.

        Returns a value in [0.0, MAX_PENALTY]. The final score is multiplied
        by (1.0 - penalty).
        """
        if near_zero_fraction <= NULL_PENALTY_RAMP_START:
            return 0.0
        ramp_range = NULL_PENALTY_RAMP_END - NULL_PENALTY_RAMP_START
        ramp_position = min((near_zero_fraction - NULL_PENALTY_RAMP_START) / ramp_range, 1.0)
        return ramp_position * NULL_PENALTY_MAX
