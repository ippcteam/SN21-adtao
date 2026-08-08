"""Subnet-wide constants for SN21."""

# Subnet registration
SUBNET_NETUID = 21
SUBNET_NAME = "hope-impact-prediction"

# Schema versions
EPISODE_SCHEMA_VERSION = "v1.9"
PREDICTION_SCHEMA_VERSION = "1.0"

# Scoring formula version — mirrors the `**Version**` row in
# docs/SN21_REWARD_MECHANISM.md. Bump in lockstep with the spec when the
# launch formula changes. Carried into every leaderboard report's
# `scoring_formula_version` field; paired with the git commit SHA so any
# reader can pin both the human-readable version and the exact code.
SCORING_FORMULA_VERSION = "1.2.1"

# Horizons
# WEEKLY ERA. The daily stream predicts 7, 14 AND 28 days — see
# hope/scoring/daily_score_flow.DAILY_STREAM_HORIZON_WEIGHTS. Leaving this
# unmarked cost an engineer a day in 2026-08: it reads as the current horizon
# set and is not.
HORIZONS = [7, 14]  # weekly only; 28-day is live in the daily stream

# Measurement resolution tiers
RESOLUTION_HIGH = "high"
RESOLUTION_MEDIUM = "medium"
RESOLUTION_LOW = "low"

# Scoring component weight ranges (published, validators must stay within)
WEIGHT_RANGES = {
    "quantile_accuracy": (0.45, 0.55),
    "calibration": (0.15, 0.25),
    "directional": (0.10, 0.20),
    "goal_accuracy": (0.10, 0.20),
}

# Default scoring weights for launch
# WEEKLY ERA component weights. The daily stream uses its own, and they are
# NOT the same: coverage is 0.10 there, not 0.20, and they sum to 0.90 rather
# than 1.0 (settle_day_flow.W_QUANTILE / W_COVERAGE / W_DIRECTION / W_GOAL).
# `goal_accuracy` here is a Brier score on goal_miss_probability; the daily
# `goal` component is accuracy on the account's OWN goal metric (CPA or ROAS),
# which is a different measurement of a different field.
DEFAULT_WEIGHTS = {
    "quantile_accuracy": 0.50,
    "calibration": 0.20,
    "directional": 0.15,
    "goal_accuracy": 0.15,
}

# Launch action types (Phase 1 Epoch 1)
LAUNCH_ACTION_TYPES = [
    "BUDGET_CHANGE",
    "BID_STRATEGY_CHANGE",
    "TARGET_VALUE_CHANGE",
    "CAMPAIGN_PAUSE",
]

# Epoch-type classification table — per SN21_REWARD_MECHANISM.md
# §"Component 3 — Epoch type multiplier". Each row maps a release's
# (campaign_type, action_scope) onto the public-facing
# (epoch_type, epoch_subtype, multiplier) triple used in leaderboard
# reports. Phase 1 ships only the SEARCH/CAMPAIGN row; the other rows are
# spec-defined for forward compatibility but not yet active in scope.
EPOCH_TYPE_TABLE: tuple[tuple[str, str | None, str, str | None, float], ...] = (
    # (campaign_type,  action_scope,    epoch_type,      epoch_subtype,    multiplier)
    ("SEARCH",         "CAMPAIGN",      "Search",        "campaign-level", 1.0),
    ("SEARCH",         "SUB_CAMPAIGN",  "Search",        "sub-campaign",   1.2),
    ("PMAX",           None,            "PMax",          None,             1.5),
    ("SHOPPING",       None,            "Shopping",      None,             1.3),
    ("CONSOLIDATION",  None,            "Consolidation", None,             2.0),
    ("CHAMPIONSHIP",   None,            "Championship",  None,             3.0),
)

# Horizon weights by measurement resolution
# WEEKLY ERA. The published blend table (7/14/28) is
# daily_score_flow.DAILY_STREAM_HORIZON_WEIGHTS and it MATCHES the docs.
# This one does not, and is not what a daily miner is scored on.
HORIZON_WEIGHTS = {
    "high": {"7": 0.40, "14": 0.60},      # Launch: only 7+14
    "medium": {"7": 0.35, "14": 0.65},
    "low": {"7": 0.30, "14": 0.70},
}

# Null penalty parameters
NULL_PENALTY_RAMP_START = 0.40
NULL_PENALTY_RAMP_END = 0.85
NULL_PENALTY_MAX = 0.60
# Null-detector thresholds expressed in the same fractional units as
# outcome deltas (e.g. 0.02 means 2 percentage points). Miner predictions
# must use matching fractional units (`p50 = -0.05` means -5%). The
# specific values are governance-tuned via the spec doc — see
# SN21_REWARD_MECHANISM.md §4.3.
NEAR_ZERO_THRESHOLD = 0.02
MIN_INTERVAL_WIDTH = 0.03

# Calibration parameters
CALIBRATION_WIDTH_EXPONENT = 1.3
CALIBRATION_MISS_MULTIPLIER = 2.5
CALIBRATION_LOW_RES_REDUCTION = 0.50

# Directional accuracy
DIRECTIONAL_NEAR_ZERO_THRESHOLD = 0.01

# Epoch timing — weekly cadence:
#   Mining open:    Monday 12:00 noon EST (17:00 UTC) → Sunday 23:59 EST (Monday 04:59 UTC)
#   Validation:     Monday 00:00 EST (05:00 UTC) → Monday 12:00 noon EST (17:00 UTC)
#   Total mining:   ~6.5 days
#   Total scoring:  ~12 hours
EPOCH_DURATION_DAYS = 7
MINING_OPEN_DAY = "monday"
MINING_OPEN_HOUR_UTC = 17     # Monday noon EST = 17:00 UTC
MINING_CLOSE_HOUR_UTC = 5     # Sunday midnight EST = Monday 05:00 UTC
SCORING_CLOSE_HOUR_UTC = 17   # Monday noon EST = 17:00 UTC (next epoch starts)
PREDICTION_DEADLINE_HOURS = 156  # ~6.5 days (Mon 17:00 UTC → next Mon 05:00 UTC)

# Burn rate — percentage of emissions assigned to UID 0 (subnet owner)
# Start high (95%) to deter exploiters, decrease as the system proves stable
DEFAULT_BURN_FRACTION = 0.95

# Data API
HOPE_API_VERSION = "v1"
