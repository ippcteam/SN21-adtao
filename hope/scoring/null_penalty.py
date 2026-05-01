"""Null Penalty — graduated penalty for near-zero and low-information predictions.

Prevents miners from gaming the system by:
1. Predicting near-zero for every episode
2. Cherry-picking only easy episodes (skipped episodes count as near-zero)
3. Setting p50 just above threshold with zero-centered intervals

Detection criteria (ALL must be true for a prediction to NOT be near-zero):
- |p50| > NEAR_ZERO_THRESHOLD for at least one metric
- Interval width (p90-p10) > MIN_INTERVAL_WIDTH for at least one metric

near_zero_fraction = (near_zero_submitted + skipped_episodes) / total_episodes
penalty = max(0, (near_zero_fraction - RAMP_START) / (RAMP_END - RAMP_START)) * MAX_PENALTY
final_score *= (1.0 - penalty)

Ramp: 40% near-zero is free, 85%+ gets maximum 60% penalty.
"""

from __future__ import annotations

from hope.constants import (
    MIN_INTERVAL_WIDTH,
    NEAR_ZERO_THRESHOLD,
    NULL_PENALTY_MAX,
    NULL_PENALTY_RAMP_END,
    NULL_PENALTY_RAMP_START,
)
from hope.protocol.prediction import Prediction


class NullPenalty:
    """Graduated near-zero prediction penalty."""

    def is_near_zero(self, prediction: Prediction) -> bool:
        """Check if a prediction is near-zero or low-information.

        A prediction is considered near-zero if ALL metrics across ALL horizons
        have both:
        - |p50| < NEAR_ZERO_THRESHOLD (point estimate near zero)
        - interval width (p90-p10) < MIN_INTERVAL_WIDTH (narrow, uninformative)
        """
        for horizon_pred in prediction.horizons.values():
            metrics = [
                horizon_pred.cost_delta_pct,
                horizon_pred.conversions_delta_pct,
                horizon_pred.efficiency_delta_pct,
            ]
            for q in metrics:
                # If any metric has a meaningful p50 AND meaningful interval width,
                # the prediction is NOT near-zero
                if abs(q.p50) > NEAR_ZERO_THRESHOLD:
                    return False
                if (q.p90 - q.p10) > MIN_INTERVAL_WIDTH:
                    return False
        return True

    def compute_near_zero_fraction(
        self,
        predictions: list[Prediction],
        total_episodes: int | None = None,
    ) -> float:
        """Fraction of episodes that are near-zero or skipped.

        If total_episodes is provided, skipped episodes count as near-zero.
        This prevents cherry-picking: submitting only for easy episodes.
        """
        if total_episodes is not None and total_episodes > 0:
            near_zero_submitted = sum(1 for p in predictions if self.is_near_zero(p))
            skipped = max(0, total_episodes - len(predictions))
            return (near_zero_submitted + skipped) / total_episodes

        # Fallback: count over submitted predictions only
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
