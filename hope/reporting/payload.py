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
  "top_n_scores": [0.94, 0.91, 0.88, 0.85, 0.82, 0.80, 0.77, 0.74, 0.71, 0.68],
  "aggregator_version": 2
}
```
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class MinerResult(BaseModel):
    """Per-miner row for the Cacheon-style leaderboard table.

    Per the v3 contract change: the dashboard now publishes per-UID
    identity (UID + full hotkey + score + status + tier). Supersedes
    the previous IA D-05 stance that forbade per-UID data.

    ``hotkey`` carries the full SS58. Display truncation (e.g.
    ``5CVCZw...TBq8``) is a browser-side concern — the wire payload
    has the full string.

    Extra per-miner metrics can be added in the future via additional
    fields here (e.g. ``submission_count``, ``validator_agreement``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: int = Field(ge=0, le=255)
    hotkey: str
    score: float = Field(ge=0.0, le=1.0)
    status: Literal[
        "scored",
        "disqualified_below_threshold",
        "disqualified_missing_snapshot",
        "disqualified_invalid_commit",
        "disqualified_plaintext_unavailable",
        # v4 additions (CMS Disqualifier panel surfaces these with their own
        # heading + explainer, so miners can self-diagnose):
        "disqualified_not_registered",      # uid wasn't registered on netuid at epoch close
        "disqualified_late_submission",     # bundle landed after the close-window cutoff
        "disqualified_not_in_epoch",        # submitted, but not part of this epoch's eligible cohort (e.g. a re-run scoped to the original participants)
        "disqualified_other",
    ]
    tier: Optional[Literal["elite", "competitive", "participating"]] = None

    @field_validator("hotkey")
    @classmethod
    def _hotkey_is_ss58(cls, v: str) -> str:
        """Reject anything that isn't a chain SS58 address.

        The CMS enforces the same rule server-side ("starts with 5, 47–48
        chars"); mirroring it here fails fast at payload-build time so a
        regression (e.g. publishing the 64-char raw-pubkey hex instead of
        the SS58) surfaces in tests rather than as a 400 from a live cron.
        """
        if not (v.startswith("5") and 47 <= len(v) <= 48):
            raise ValueError(
                f"hotkey must be a chain SS58 address (starts with 5, "
                f"47-48 chars); got {len(v)}-char {v[:8]!r}..."
            )
        return v


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

    # Top-N anonymized scores in descending order. Lets miners with chain-
    # side knowledge of their own score self-locate against ranked peers
    # without us republishing UIDs (IA D-05 compliant). Capped at 20 by
    # design — the dashboard's rank-stack widget shows the top of the
    # qualifying pool, not the full leaderboard. None when pool is below
    # the distribution floor (matches score_distribution's gating).
    top_n_scores: Optional[list[float]] = Field(default=None, max_length=20)

    # Optional pointer to a published epoch this row corrects. Per the
    # CMS contract (IA D-13): published rows are frozen; mutations land
    # as NEW rows with a `-COR-N` suffix in their epoch_id and a
    # `supersedes` pointer back to the original. The dashboard renders
    # the most recent correction as the canonical view of an epoch slot;
    # the original stays at its permanent URL with a "corrected by"
    # banner.
    supersedes: Optional[str] = None

    # Per-miner result rows (Cacheon-style leaderboard). New in v3 — the
    # dashboard now renders a sortable per-UID table when this field is
    # present. Optional + forward-compatible: when None, the dashboard
    # falls back to the aggregate-only view from v1/v2.
    miner_results: Optional[list[MinerResult]] = None

    # Aggregator wire-shape version. Bump when output changes for the
    # same input. The CMS pins each published row to this number.
    aggregator_version: int = Field(default=4, ge=1)
