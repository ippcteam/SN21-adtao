"""Pydantic v2 models for the public leaderboard report payload.

The payload posted to `cms.adtao.io/api/sn21-epoch-reports` is defined
**exactly** by these models. Two structural invariants hold by
construction:

  1. **No per-UID, per-hotkey, or per-miner field can appear.** Every
     model declares `extra="forbid"`, so the type system refuses any
     field name not explicitly declared here. The aggregator's return
     type is `EpochReportPayload`; it is structurally impossible to
     return UIDs.

  2. **`epoch_id` is a string** (the canonical SN21 release key, e.g.
     `WR-2026-W18-PUB-E1`) — not an integer. Per Rob's reply (Q9),
     the website schema, idempotency key, and URL adopt this.

`aggregator_version` is the integer wire-shape version of the
aggregator that produced the payload. Bump it whenever the aggregator
produces output that differs from a prior version on identical input.
Per Q15, the website pins each published row to its aggregator_version
and treats correction reposts as new versions, not in-place edits.

The contract §4 example reads:

```json
{
  "epoch_id": "WR-2026-W18-PUB-E1",
  "epoch_type": "Search",
  "epoch_subtype": "campaign-level",
  "block_range_start": 4823091,
  "block_range_end": 4830290,
  "scoring_formula_version": "1.2.1",
  "scoring_formula_commit": "a1b2c3d4e5f6...",
  "horizon_set": ["7d", "14d"],
  "epoch_type_multiplier": 1.0,
  "pool_size": 47,
  "total_registered_uids": 64,
  "pool_size_below_distribution_floor": false,
  "baseline_beat_rate": 0.766,
  "score_distribution": { ... } | null,
  "tier_distribution": { ... },
  "tier_split_active": true,
  "emergency_intervention": { "triggered": false },
  "validator_output_snapshot_timestamp": "2026-05-12T14:32:18Z",
  "chain_fetch_timestamp": "2026-05-12T14:33:02Z",
  "commentary_markdown": null,
  "aggregator_version": 1
}
```
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Tier emission shares are the policy constants per
# SN21_REWARD_MECHANISM.md §"Component 2 — Tiered emission bands".
# These never change at runtime; the aggregator embeds them verbatim
# in every payload for self-describing transparency.
ELITE_EMISSION_SHARE = 0.60
COMPETITIVE_EMISSION_SHARE = 0.30
PARTICIPATING_EMISSION_SHARE = 0.10

# Pool-size threshold below which the tier split collapses to a single
# pool and the histogram is omitted (per Rob's contract §3.2 +
# `hope/validator/tiered_weights.py:MIN_MINERS_FOR_TIER_SPLIT`).
POOL_SIZE_DISTRIBUTION_FLOOR = 15


class ScoreSummary(BaseModel):
    """Five-number summary for the qualifying pool's miner scores."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mean: float
    median: float
    p25: float
    p75: float
    max: float


class ScoreDistribution(BaseModel):
    """Histogram of `miner_score` over the qualifying pool.

    `bin_edges` has length `n+1`; `bin_counts` has length `n`.
    `bin_counts[i]` is the count of miners whose `miner_score` falls
    in `[bin_edges[i], bin_edges[i+1])`. K-anonymity invariant: every
    `bin_counts[i]` is `0` or `>= 5` after merging — enforced by the
    aggregator via the deterministic left-merge rule.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bin_edges: list[float]
    bin_counts: list[int]
    summary: ScoreSummary


class TierSlice(BaseModel):
    """One tier's headcount + share metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=0)
    share_of_pool: float = Field(ge=0.0, le=1.0)
    share_of_emissions: float = Field(ge=0.0, le=1.0)


class TierDistribution(BaseModel):
    """All three tier slices + elite-floor outcome.

    When the pool collapses to a single undivided group (i.e.
    `tier_split_active == False`), all three `count` values are 0;
    `share_of_emissions` is still set to the policy constants so the
    payload remains self-describing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    elite: TierSlice
    competitive: TierSlice
    participating: TierSlice
    elite_floor_met: bool


class EmergencyIntervention(BaseModel):
    """Emergency-intervention state for this epoch.

    Per Q19 the v1 default is `{ "triggered": false }`. When trigger
    state machines land in `SN21_REWARD_MECHANISM.md`, the aggregator
    populates `type`, `outcome`, and `summary_text` accordingly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    triggered: bool
    type: Optional[Literal["collusion", "dataset_misalignment"]] = None
    outcome: Optional[Literal[
        "rescored",
        "episodes_dropped",
        "epoch_cancelled",
        "no_action_after_review",
    ]] = None
    summary_text: Optional[str] = Field(default=None, max_length=280)


class EpochReportPayload(BaseModel):
    """Public per-epoch payload posted to the leaderboard CMS.

    Structurally aggregate-only — no UID, hotkey, or per-miner field
    can appear (`extra="forbid"`). The aggregator's return type is
    this model; downstream transport never sees private data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Identity & classification
    epoch_id: str
    epoch_type: str
    epoch_subtype: Optional[str]

    # Chain footprint
    block_range_start: int = Field(ge=0)
    block_range_end: int = Field(ge=0)

    # Provenance — pinpoints both the policy and the executable
    scoring_formula_version: str
    scoring_formula_commit: str
    horizon_set: list[str]
    epoch_type_multiplier: float = Field(gt=0.0)

    # Pool stats
    pool_size: int = Field(ge=0)
    total_registered_uids: int = Field(ge=0)
    pool_size_below_distribution_floor: bool
    baseline_beat_rate: float = Field(ge=0.0, le=1.0)

    # Distribution (None when pool is below the floor)
    score_distribution: Optional[ScoreDistribution]

    # Tiers
    tier_distribution: TierDistribution
    tier_split_active: bool

    # Emergency state
    emergency_intervention: EmergencyIntervention

    # Snapshot timestamps (ISO8601 UTC)
    validator_output_snapshot_timestamp: str
    chain_fetch_timestamp: str

    # Optional human commentary; null in routine epochs.
    commentary_markdown: Optional[str] = None

    # Aggregator wire-shape version. Bump when output changes for the
    # same input. The CMS pins each published row to this number.
    aggregator_version: int = Field(default=1, ge=1)
