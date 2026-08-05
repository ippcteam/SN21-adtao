"""Episode Scorer — scores a single episode across all horizons and metrics."""

from __future__ import annotations

from dataclasses import dataclass

from hope.protocol.episode import Episode
from hope.protocol.outcomes import HorizonOutcome, Outcome
from hope.protocol.prediction import HorizonPrediction, Prediction
from hope.scoring.components.base import EpisodeContext
from hope.scoring.components.calibration import Calibration
from hope.scoring.components.directional import DirectionalAccuracy
from hope.scoring.components.goal_accuracy import GoalAccuracy
from hope.scoring.components.quantile_accuracy import QuantileAccuracy
from hope.scoring.weights import ScoringWeights


@dataclass
class HorizonScore:
    """Scoring result for a single horizon."""

    horizon: str
    quantile_accuracy: float
    calibration: float
    directional: float
    goal_accuracy: float
    weighted_total: float


@dataclass
class EpisodeScore:
    """Scoring result for a complete episode."""

    episode_id: str
    horizon_scores: dict[str, HorizonScore]
    weighted_total: float
    measurement_resolution: str
    coverage_status: str


class EpisodeScorer:
    """Score a single episode across all horizons and metrics."""

    def __init__(self, weights: ScoringWeights):
        self.components = {
            "quantile_accuracy": QuantileAccuracy(),
            "calibration": Calibration(),
            "directional": DirectionalAccuracy(),
            "goal_accuracy": GoalAccuracy(),
        }
        self.weights = weights

    def score_horizon(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> HorizonScore:
        """Score one horizon. Returns component scores + weighted total."""
        qa = self.components["quantile_accuracy"].score(prediction, outcome, context)
        cal = self.components["calibration"].score(prediction, outcome, context)
        dir_ = self.components["directional"].score(prediction, outcome, context)
        goal = self.components["goal_accuracy"].score(prediction, outcome, context)

        weighted = (
            qa * self.weights.quantile_accuracy
            + cal * self.weights.calibration
            + dir_ * self.weights.directional
            + goal * self.weights.goal_accuracy
        )

        return HorizonScore(
            horizon="",
            quantile_accuracy=qa,
            calibration=cal,
            directional=dir_,
            goal_accuracy=goal,
            weighted_total=weighted,
        )

    def score_episode(
        self,
        prediction: Prediction,
        outcome: Outcome,
        episode: Episode,
    ) -> EpisodeScore:
        """Score all horizons, apply horizon weighting, return episode score."""
        resolution = episode.episode_metadata.measurement_resolution
        coverage = episode.episode_metadata.coverage_status

        context = EpisodeContext(
            measurement_resolution=resolution,
            coverage_status=coverage,
            goal_metric=outcome.scoring_metadata.goal_metric,
            action_type=episode.action_bundle.actions[0].type if episode.action_bundle.actions else "UNKNOWN",
            campaign_type="SEARCH",
        )

        horizon_weights = self.weights.get_horizon_weights(resolution)
        horizon_scores: dict[str, HorizonScore] = {}
        weighted_sum = 0.0
        weight_sum = 0.0

        outcome_map = {"7": outcome.t7, "14": outcome.t14}

        for horizon_key, horizon_pred in prediction.horizons.items():
            horizon_outcome = outcome_map.get(horizon_key)
            if horizon_outcome is None:
                continue

            h_score = self.score_horizon(horizon_pred, horizon_outcome, context)
            h_score.horizon = horizon_key
            horizon_scores[horizon_key] = h_score

            h_weight = horizon_weights.get(horizon_key, 0.0)
            weighted_sum += h_score.weighted_total * h_weight
            weight_sum += h_weight

        total = weighted_sum / weight_sum if weight_sum > 0 else 0.0

        return EpisodeScore(
            episode_id=prediction.episode_id,
            horizon_scores=horizon_scores,
            weighted_total=total,
            measurement_resolution=resolution,
            coverage_status=coverage,
        )
