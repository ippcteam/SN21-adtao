"""Subnet-wide constants for HOPE SN21."""

import os

# Subnet registration
SUBNET_NETUID = 21
SUBNET_NAME = "hope-impact-prediction"

# Schema versions
EPISODE_SCHEMA_VERSION = "v1.9"
PREDICTION_SCHEMA_VERSION = "1.0"

# Horizons
HORIZONS = [7, 14]  # Launch: 7 + 14 only. 28-day added post-launch.

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

# Horizon weights by measurement resolution
HORIZON_WEIGHTS = {
    "high": {"7": 0.40, "14": 0.60},      # Launch: only 7+14
    "medium": {"7": 0.35, "14": 0.65},
    "low": {"7": 0.30, "14": 0.70},
}

# Null penalty parameters
NULL_PENALTY_RAMP_START = 0.40
NULL_PENALTY_RAMP_END = 0.85
NULL_PENALTY_MAX = 0.60
NEAR_ZERO_THRESHOLD = 1.0

# Calibration parameters
CALIBRATION_WIDTH_EXPONENT = 1.3
CALIBRATION_MISS_MULTIPLIER = 2.5
CALIBRATION_LOW_RES_REDUCTION = 0.50

# Directional accuracy
DIRECTIONAL_NEAR_ZERO_THRESHOLD = 1.0

# Epoch timing — Rob's weekly cadence:
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
# Start high (95%) to deter exploiters, decrease in 5% chunks as testing progresses
# Per Tensora: "start with 95% burn on mainnet, lower as good miners show up"
DEFAULT_BURN_FRACTION = 0.95

# HOPE Data API — must be set via environment variables
HOPE_API_BASE_URL = os.environ.get("HOPE_API_URL", "")
HOPE_API_VERSION = "v1"
