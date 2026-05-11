"""Outcome schema — ground truth held by validators, withheld from miners."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class HorizonOutcome(BaseModel):
    """Measured outcome for a single time horizon.

    ``efficiency_delta_pct`` is the goal-relative efficiency change (CPA or
    ROAS depending on goal_metric). It is undefined for episodes where
    measured conversions or cost are zero, and the operator data API emits
    ``null`` in that case. When None, scoring components skip this metric
    and average over the remaining (cost, conversions) deltas. Miner
    predictions still include the efficiency quantile — it just doesn't
    contribute to the score for that horizon.
    """

    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: Optional[float] = None
    goal_miss: Literal[0, 1]  # 1 = goal was missed during this horizon


class ConditionalPriorBaseline(BaseModel):
    """Conditional-prior baseline values for one episode.

    Published in the release artifact and pinned by the 9.A.1 commit hash.
    Each value is the historical mean delta for the same
    ``(campaign_type, action_type)`` over a rolling training window.
    Miners must beat this baseline to clear the participation gate.
    """

    campaign_type: str
    action_type: str
    t7_cost_delta_pct: float = 0.0
    t7_conversions_delta_pct: float = 0.0
    t7_efficiency_delta_pct: float = 0.0
    t14_cost_delta_pct: float = 0.0
    t14_conversions_delta_pct: float = 0.0
    t14_efficiency_delta_pct: float = 0.0


class ScoringMetadata(BaseModel):
    """Context for how this episode should be scored."""

    goal_metric: str = "CPA"
    measurement_resolution: Literal["high", "medium", "low"] = "high"
    reliability_weight: float = 1.0  # Episode reliability for weighting
    baseline_type: Literal[
        "system_estimate", "predict_zero", "conditional_prior"
    ] = "conditional_prior"
    coverage_status: Literal["trust_enriched", "baseline"] = "baseline"
    conditional_prior: Optional[ConditionalPriorBaseline] = None


class Outcome(BaseModel):
    """Complete ground truth for one episode across all horizons."""

    episode_id: str
    t7: Optional[HorizonOutcome] = None
    t14: Optional[HorizonOutcome] = None
    scoring_metadata: ScoringMetadata = ScoringMetadata()
