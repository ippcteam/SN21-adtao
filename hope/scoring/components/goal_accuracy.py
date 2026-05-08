"""Goal Accuracy — Brier score for goal miss probability (15% weight).

Proper scoring rule: the optimal strategy is honest probability reporting.

Formula:
    goal_accuracy = 1.0 - (predicted_goal_miss_prob - actual_goal_miss)^2
"""

from __future__ import annotations

from hope.protocol.prediction import HorizonPrediction
from hope.protocol.outcomes import HorizonOutcome
from hope.scoring.components.base import EpisodeContext, ScoringComponent


class GoalAccuracy(ScoringComponent):
    """Brier score for goal miss probability prediction."""

    def score(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> float:
        """Score goal miss probability prediction.

        Returns value in [0.0, 1.0] where 1.0 is perfect.
        """
        predicted = prediction.goal_miss_probability
        actual = float(outcome.goal_miss)  # 0 or 1

        brier = (predicted - actual) ** 2
        return max(0.0, min(1.0, 1.0 - brier))
