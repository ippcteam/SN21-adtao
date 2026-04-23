"""Episode schema — the miner-facing payload for a single prediction challenge."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class Goal(BaseModel):
    type: str  # CPA, ROAS, CONVERSIONS, COST, cost_per_conversion, etc.
    target: float
    deviation: float
    tolerance: float


class HealthStatus(BaseModel):
    compliance_score: int
    compliance_trend: str | int = "stable"
    status_counts: dict[str, int] = {}
    issues_count: int = 0


class Archetype(BaseModel):
    archetype_id: str
    primary_q: str = "Q1"
    severity_score: int
    confidence: float
    blast_radius: str = "medium"
    base_risk: str | int = "medium"


class Guardrail(BaseModel):
    type: str
    severity: int = 0
    active: bool = True


class PortfolioContext(BaseModel):
    constraint_level: str = "UNCONSTRAINED"
    campaigns_in_shared_budget: int = 0
    impression_share_lost_budget: float = 0.0
    redistribution_likelihood: float = 0.0


class AccountState(BaseModel):
    customer_id_hash: str
    currency_code: str = "USD"
    account_type: str = "lead"
    spend_bucket: str = "mid"
    tracking_reliability: str = "high"
    goal: Optional[Goal] = None
    health: Optional[HealthStatus] = None
    archetypes: Optional[list[Archetype]] = None
    guardrails: Optional[list[Guardrail]] = None
    portfolio_context: Optional[PortfolioContext] = None


class EnvironmentalContext(BaseModel):
    seasonality_index: float = 1.0
    auction_pressure_trend: float = 0.0
    spend_volatility_cv: float = 0.0
    week_over_week_delta: float = 0.0


class EpisodeMetadata(BaseModel):
    episode_id: str
    release_key: str = ""
    schema_version: str = "v1.9"
    phase: int = 1
    epoch: int = 1
    coverage_status: Literal["trust_enriched", "baseline"] = "baseline"
    measurement_resolution: Literal["high", "medium", "low"] = "high"
    action_window_start: Optional[str] = None
    action_window_end: Optional[str] = None
    outcome_horizons_days: list[int] = [7, 14]
    environmental_context: EnvironmentalContext = EnvironmentalContext()


class BlastRadius(BaseModel):
    tier: Literal["single", "batch", "significant", "parent_equivalent"] = "parent_equivalent"
    impact_ratio: float = 1.0
    spend_contribution: float = 1.0
    conversion_contribution: float = 1.0
    impression_contribution: float = 1.0
    entity_count: int = 1
    is_default: bool = False


class Action(BaseModel):
    type: str
    scope: str = "campaign"
    entity_level: str = "campaign"
    entity_id_hash: str = ""
    campaign_id_hash: str = ""
    blast_radius: BlastRadius = BlastRadius()
    impact_class: str = "unknown"
    risk_tier: str = "medium"
    reversibility: str = "unknown"
    magnitude: Optional[dict] = None
    source: str = "system"
    timestamp: Optional[str] = None


class BundleSummary(BaseModel):
    action_count: int = 0
    action_count_by_scope: dict[str, int] = {}
    max_blast_radius_tier: str = "parent_equivalent"
    weighted_impact_ratio: float = 1.0
    dominant_scope: str = "campaign"
    has_destructive: bool = False
    has_improvement: bool = False
    max_risk_score: float = 0.0
    net_efficiency_delta: float = 0.0
    net_capacity_delta: float = 0.0
    source_mix: dict[str, int] = {}


class ActionBundle(BaseModel):
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    actions: list[Action] = []
    bundle_summary: BundleSummary = BundleSummary()


class CampaignTimeSeries(BaseModel):
    campaign_type: str = "SEARCH"
    bid_strategy_type: str = "UNKNOWN"
    impressions: list[int] = []
    clicks: list[int] = []
    cost_micros: list[int] = []
    conversions: list[float] = []
    conversion_value_micros: list[int] = []
    impression_share: list[float] = []


class AccountAggregates(BaseModel):
    avg_daily_spend_micros: int = 0
    avg_daily_conversions: float = 0.0
    avg_cpa_micros: int = 0
    avg_roas: float = 0.0
    spend_cv: float = 0.0
    conversion_cv: float = 0.0
    impression_share_trend: float = 0.0
    cpc_trend: float = 0.0


class PreWindow(BaseModel):
    campaigns: dict[str, CampaignTimeSeries] = {}
    account_aggregates: AccountAggregates = AccountAggregates()


class CampaignMetadata(BaseModel):
    campaign_id_hash: str
    campaign_type: str = "SEARCH"
    bid_strategy_type: str = "UNKNOWN"
    status: str = "ENABLED"


class Episode(BaseModel):
    """Complete v1.9 episode payload as received by miners."""

    episode_metadata: EpisodeMetadata
    account_state: AccountState
    date_index: list[str] = []
    pre_window: PreWindow = PreWindow()
    action_bundle: ActionBundle = ActionBundle()
    campaign_metadata: list[CampaignMetadata] = []
