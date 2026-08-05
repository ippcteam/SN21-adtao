"""Prediction Engine — abstract base class for miner models.

Miners subclass PredictionEngine and implement predict().
The miner runner handles all Bittensor plumbing, HTTP communication,
and prediction submission.

See hope/miner/models/baseline.py for a reference implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hope.protocol.episode import Episode
from hope.protocol.prediction import Prediction


class PredictionEngine(ABC):
    """Abstract base class for prediction models.

    Subclass this and implement predict() to create a miner.
    """

    @abstractmethod
    def predict(self, episode: Episode) -> Prediction:
        """Given an episode, return predictions for all horizons.

        Must return a valid Prediction with horizons "7" and "14",
        each containing P10/P50/P90 quantile predictions for
        cost_delta_pct, conversions_delta_pct, efficiency_delta_pct,
        plus goal_miss_probability and instability_risk.
        """
        ...

    def on_epoch_start(self, epoch_id: str, episode_count: int) -> None:
        """Optional hook: called when a new epoch starts."""

    def on_epoch_end(self, epoch_id: str, scores: dict | None = None) -> None:
        """Optional hook: called when epoch scores are revealed."""

    @property
    def name(self) -> str:
        """Model name for logging."""
        return self.__class__.__name__
