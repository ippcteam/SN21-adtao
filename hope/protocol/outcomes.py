"""Outcome schema — ground truth held by validators, withheld from miners."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class HorizonOutcome(BaseModel):
    """Measured outcome for a single time horizon."""

    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: float  # CPA or ROAS delta depending on goal
    goal_miss: Literal[0, 1]  # 1 = goal was missed during this horizon


class ScoringMetadata(BaseModel):
    """Context for how this episode should be scored."""

    goal_metric: str = "CPA"
    measurement_resolution: Literal["high", "medium", "low"] = "high"
    reliability_weight: float = 1.0  # Episode reliability for weighting
    baseline_type: Literal["system_estimate", "predict_zero"] = "predict_zero"
    coverage_status: Literal["trust_enriched", "baseline"] = "baseline"


class Outcome(BaseModel):
    """Complete ground truth for one episode across all horizons."""

    episode_id: str
    t7: Optional[HorizonOutcome] = None
    t14: Optional[HorizonOutcome] = None
    scoring_metadata: ScoringMetadata = ScoringMetadata()
