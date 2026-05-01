"""Skill Score — compare miner predictions against system estimate baseline.

skill_score = 1.0 - (miner_loss / system_estimate_loss)

For episodes without system estimates (predict-zero baseline),
the baseline prediction is all zeros. Miners must beat this to
receive positive skill score.

Floored at 0.0 — no negative rewards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hope.protocol.prediction import HorizonPrediction, Prediction, QuantilePrediction


class SkillScoreCalculator:
    """Compare miner predictions against the baseline."""

    def compute_baseline_prediction(self, episode_id: str, baseline_type: str = "predict_zero") -> Prediction:
        """Construct the baseline prediction.

        For launch: always predict_zero (all deltas = 0).
        Future: system_estimate derived from episode magnitude fields.
        """
        zero_q = QuantilePrediction(p10=0.0, p50=0.0, p90=0.0)
        zero_horizon = HorizonPrediction(
            cost_delta_pct=zero_q,
            conversions_delta_pct=zero_q,
            efficiency_delta_pct=zero_q,
            goal_miss_probability=0.0,
            instability_risk=0.0,
        )

        return Prediction(
            episode_id=episode_id,
            miner_id="system_baseline",
            submitted_at=datetime.now(timezone.utc),
            horizons={"7": zero_horizon, "14": zero_horizon},
        )

    def compute_skill_score(self, miner_score: float, baseline_score: float) -> float:
        """Compute skill score. Floored at 0.0.

        skill_score = 1.0 - (miner_loss / baseline_loss)
        where loss = 1.0 - score

        If baseline is perfect (score=1.0), skill score is 0.0.
        If miner beats baseline, skill score is positive.
        """
        miner_loss = 1.0 - miner_score
        baseline_loss = 1.0 - baseline_score

        if baseline_loss <= 0.0:
            return 0.0

        skill = 1.0 - (miner_loss / baseline_loss)
        return max(0.0, skill)
