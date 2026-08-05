"""Prediction schema — what miners submit for each episode."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator


class QuantilePrediction(BaseModel):
    """P10/P50/P90 quantile prediction. Must be monotonically ordered."""

    p10: float
    p50: float
    p90: float

    @model_validator(mode="after")
    def check_ordering(self) -> QuantilePrediction:
        if not (self.p10 <= self.p50 <= self.p90):
            raise ValueError(f"Quantiles must be ordered: p10={self.p10} <= p50={self.p50} <= p90={self.p90}")
        return self


class HorizonPrediction(BaseModel):
    """Prediction for a single time horizon (7 or 14 days)."""

    cost_delta_pct: QuantilePrediction
    conversions_delta_pct: QuantilePrediction
    efficiency_delta_pct: QuantilePrediction
    goal_miss_probability: float  # [0, 1] — probability the goal is missed
    instability_risk: float  # [0, 1] — probability of high-variance outcome

    @model_validator(mode="after")
    def check_probabilities(self) -> HorizonPrediction:
        if not 0.0 <= self.goal_miss_probability <= 1.0:
            raise ValueError(f"goal_miss_probability must be in [0, 1], got {self.goal_miss_probability}")
        if not 0.0 <= self.instability_risk <= 1.0:
            raise ValueError(f"instability_risk must be in [0, 1], got {self.instability_risk}")
        return self


class Prediction(BaseModel):
    """Complete miner prediction for one episode across all horizons."""

    episode_id: str
    miner_id: str
    submitted_at: datetime
    horizons: dict[Literal["7", "14"], HorizonPrediction]
