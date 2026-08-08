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

    WEEKLY ERA — NOT THE DAILY STREAM.

    Nothing in this module decides daily pay. It is kept because the final
    weekly scoring run still uses this path. If you are building a daily
    miner, the code that scores you is:

        hope/scoring/settle_day_flow.py    one (episode, horizon) -> score
        hope/scoring/daily_score_flow.py   horizon blend weights (7/14/28)
        hope/scoring/episode_average.py    standing
        hope/scoring/weight_curve.py       standing -> weight

    Reading this file instead of those gives the wrong numbers, and the two
    were indistinguishable until 2026-08-08, when an engineer following the
    published docs landed here and reported the mismatch.
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

        A prediction is NOT near-zero only if at least one metric has BOTH:
        - |p50| > NEAR_ZERO_THRESHOLD (meaningful point estimate)
        - interval width (p90-p10) > MIN_INTERVAL_WIDTH (meaningful uncertainty range)

        Both conditions must hold — a wide interval centered on zero is still
        low-information, and a strong p50 with a tiny interval lacks calibration.
        """
        for horizon_pred in prediction.horizons.values():
            metrics = [
                horizon_pred.cost_delta_pct,
                horizon_pred.conversions_delta_pct,
                horizon_pred.efficiency_delta_pct,
            ]
            for q in metrics:
                has_directional_signal = abs(q.p50) > NEAR_ZERO_THRESHOLD
                has_meaningful_width = (q.p90 - q.p10) > MIN_INTERVAL_WIDTH
                # Must have BOTH a meaningful p50 AND meaningful interval
                if has_directional_signal and has_meaningful_width:
                    return False
        return True

    def compute_near_zero_fraction(
        self,
        predictions: list[Prediction],
        total_episodes: int | None = None,
        no_action_episode_ids: set[str] | None = None,
    ) -> float:
        """Fraction of episodes that are near-zero or skipped.

        If total_episodes is provided, skipped episodes count as near-zero.
        This prevents cherry-picking: submitting only for easy episodes.

        ``no_action_episode_ids`` are episodes whose action_bundle is empty
        (control episodes; true outcome is ~0 by definition). They're
        exempted from both numerator and denominator so an honest p50≈0
        prediction on a no-action episode doesn't count against the
        miner. Without this exemption, miners are systematically
        incentivised to lie on control episodes.
        """
        no_action = no_action_episode_ids or set()
        relevant_preds = [p for p in predictions if p.episode_id not in no_action]

        if total_episodes is not None and total_episodes > 0:
            adjusted_total = max(0, total_episodes - len(no_action))
            if adjusted_total == 0:
                return 0.0
            near_zero_submitted = sum(1 for p in relevant_preds if self.is_near_zero(p))
            skipped = max(0, adjusted_total - len(relevant_preds))
            return (near_zero_submitted + skipped) / adjusted_total

        # Fallback: count over submitted (non-control) predictions only
        if not relevant_preds:
            return 0.0
        near_zero_count = sum(1 for p in relevant_preds if self.is_near_zero(p))
        return near_zero_count / len(relevant_preds)

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
