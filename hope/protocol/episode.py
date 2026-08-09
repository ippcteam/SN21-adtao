"""Episode schema — the miner-facing payload for a single prediction challenge."""

from __future__ import annotations

from typing import Literal

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
    """One detection from the account's most recent diagnostic sweep.

    Every field except the id is OPTIONAL and may be null. That is deliberate
    and it is a correction: severity_score and confidence used to be required,
    so the packager filled them with 50 and 0.5 whenever the source was
    missing — which it almost always was. 98.8% of every severity ever
    shipped was that literal default, presented as a measurement.

    A null here means "not measured". Do not read it as zero, and do not read
    a present value as a guess: if a number is here, it came from the sweep.
    """

    archetype_id: str
    primary_q: str | None = None
    severity_score: float | None = None
    confidence: float | None = None
    blast_radius: str | None = None
    base_risk: str | int | None = None

    # Which entity the detection fired on. `entity_id_hash` is hashed with the
    # same function as campaign_metadata, so a campaign-scoped finding can be
    # joined to the campaign it belongs to.
    entity_scope: str | None = None
    entity_id_hash: str | None = None

    # Short machine-readable reason from the detector.
    why_code: str | None = None

    # How many underlying findings rolled up into this row. A sweep writes one
    # row per finding, so a single archetype can repeat many times for one
    # entity — 34 erroring landing pages is one detection with occurrences=34,
    # not 34 detections.
    occurrences: int = 1


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
    # How much conversion evidence this account generates: `sparse`,
    # `moderate`, `dense`, or null when it had no spending days to judge from.
    # Thin accounts are never down-weighted or excluded — this exists so a
    # forecast can be calibrated to the evidence behind it, not so anyone can
    # be steered toward large accounts.
    signal_class: str | None = None
    currency_code: str = "USD"
    account_type: str = "lead"
    spend_bucket: str = "mid"
    tracking_reliability: str = "high"
    goal: Goal | None = None
    health: HealthStatus | None = None
    archetypes: list[Archetype] | None = None
    guardrails: list[Guardrail] | None = None
    portfolio_context: PortfolioContext | None = None


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
    action_window_start: str | None = None
    action_window_end: str | None = None
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
    magnitude: dict | None = None
    source: str = "system"
    timestamp: str | None = None


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
    window_start: str | None = None
    window_end: str | None = None
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
