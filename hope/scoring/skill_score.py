"""Skill Score — compare miner predictions against the configured baseline.

skill_score = 1.0 - (miner_loss / baseline_loss)

The baseline is selected per episode by ``ScoringMetadata.baseline_type``:

* ``conditional_prior`` (launch default): use the historical mean delta
  for the same ``(campaign_type, action_type)`` published in
  ``ScoringMetadata.conditional_prior``.
* ``predict_zero``: all deltas = 0. Used for episodes that have no
  ``conditional_prior`` value yet (e.g. brand-new action types).
* ``system_estimate``: future — derived from platform system estimate
  fields when those become available.

Floored at 0.0 — no negative rewards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hope.protocol.outcomes import ConditionalPriorBaseline
from hope.protocol.prediction import HorizonPrediction, Prediction, QuantilePrediction


class SkillScoreCalculator:
    """Compare miner predictions against the baseline."""

    def compute_baseline_prediction(
        self,
        episode_id: str,
        baseline_type: str = "conditional_prior",
        conditional_prior: ConditionalPriorBaseline | None = None,
    ) -> Prediction:
        """Construct the baseline prediction for ``episode_id``.

        For ``conditional_prior`` baselines we centre P10/P50/P90 on the
        published mean delta with a wide ±2× envelope so the baseline
        carries some calibration uncertainty (otherwise interval scoring
        is undefined).

        For ``predict_zero`` and ``system_estimate`` (until that lands)
        we fall through to the all-zero prediction.
        """
        if baseline_type == "conditional_prior" and conditional_prior is not None:
            return self._conditional_prior_prediction(episode_id, conditional_prior)
        return self._zero_prediction(episode_id)

    @staticmethod
    def _zero_prediction(episode_id: str) -> Prediction:
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

    @staticmethod
    def _conditional_prior_prediction(
        episode_id: str,
        prior: ConditionalPriorBaseline,
    ) -> Prediction:
        def envelope(p50: float) -> QuantilePrediction:
            from hope.constants import MIN_INTERVAL_WIDTH
            half_width = max(abs(p50) * 1.0, MIN_INTERVAL_WIDTH)
            return QuantilePrediction(p10=p50 - half_width, p50=p50, p90=p50 + half_width)

        h7 = HorizonPrediction(
            cost_delta_pct=envelope(prior.t7_cost_delta_pct),
            conversions_delta_pct=envelope(prior.t7_conversions_delta_pct),
            efficiency_delta_pct=envelope(prior.t7_efficiency_delta_pct),
            goal_miss_probability=0.5,
            instability_risk=0.5,
        )
        h14 = HorizonPrediction(
            cost_delta_pct=envelope(prior.t14_cost_delta_pct),
            conversions_delta_pct=envelope(prior.t14_conversions_delta_pct),
            efficiency_delta_pct=envelope(prior.t14_efficiency_delta_pct),
            goal_miss_probability=0.5,
            instability_risk=0.5,
        )
        return Prediction(
            episode_id=episode_id,
            miner_id="system_baseline",
            submitted_at=datetime.now(timezone.utc),
            horizons={"7": h7, "14": h14},
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
