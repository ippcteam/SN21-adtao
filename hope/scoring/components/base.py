"""Abstract base class for scoring components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hope.protocol.prediction import HorizonPrediction
    from hope.protocol.outcomes import HorizonOutcome


@dataclass
class EpisodeContext:
    """Contextual information for scoring a single episode."""

    measurement_resolution: str = "high"
    coverage_status: str = "baseline"
    goal_metric: str = "CPA"
    action_type: str = "BUDGET_CHANGE"
    campaign_type: str = "SEARCH"


class ScoringComponent(ABC):
    """Base class for scoring components.

    Each component scores a single prediction against a single outcome
    for one horizon. Returns a value in [0.0, 1.0] where 1.0 is perfect.
    """

    @abstractmethod
    def score(
        self,
        prediction: HorizonPrediction,
        outcome: HorizonOutcome,
        context: EpisodeContext,
    ) -> float:
        """Score a single horizon prediction against ground truth."""
        ...
