"""T23/M4 — the weight-source switch: which standings drive on-chain weights.

The entire M4 launch event reduces to flipping this selector from 'legacy'
to 'shadow' once cutover_ready holds (>=7 distinct shadow-scored days).
Pure + env-gated; wiring into weight_setter is the one-line change on
Rob's date.
"""

from __future__ import annotations

import os

WEIGHT_SOURCE_ENV = "SN21_WEIGHT_SOURCE"
LEGACY = "legacy"
SHADOW = "shadow"


def weight_source() -> str:
    v = os.environ.get(WEIGHT_SOURCE_ENV, LEGACY).lower()
    return SHADOW if v == SHADOW else LEGACY  # anything unrecognised -> legacy


def select_standings(legacy_standings: dict, shadow_standings: dict,
                     cutover_check: dict | None = None) -> tuple[str, dict]:
    """Pick the standings that drive weights. Shadow requires BOTH the env
    flag AND a passing cutover gate — a flag alone cannot switch weights to
    an unproven shadow corpus (defense against premature/accidental flips)."""
    if weight_source() == SHADOW and cutover_check and cutover_check.get("cutover_possible"):
        return SHADOW, shadow_standings
    return LEGACY, legacy_standings
