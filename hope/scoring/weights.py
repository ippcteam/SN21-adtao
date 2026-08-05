"""Scoring weight configuration — published ranges validators must respect."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from hope.constants import DEFAULT_WEIGHTS, HORIZON_WEIGHTS, WEIGHT_RANGES


@dataclass
class ScoringWeights:
    """Per-epoch scoring weights. Must fall within published ranges."""

    quantile_accuracy: float = DEFAULT_WEIGHTS["quantile_accuracy"]
    calibration: float = DEFAULT_WEIGHTS["calibration"]
    directional: float = DEFAULT_WEIGHTS["directional"]
    goal_accuracy: float = DEFAULT_WEIGHTS["goal_accuracy"]
    horizon_weights: dict[str, dict[str, float]] = field(default_factory=lambda: dict(HORIZON_WEIGHTS))

    def validate_ranges(self) -> bool:
        """Verify all component weights fall within published ranges."""
        for name, (lo, hi) in WEIGHT_RANGES.items():
            val = getattr(self, name)
            if not (lo <= val <= hi):
                return False
        total = self.quantile_accuracy + self.calibration + self.directional + self.goal_accuracy
        return not abs(total - 1.0) > 0.001

    def get_horizon_weights(self, resolution: str) -> dict[str, float]:
        """Get horizon weights for a given measurement resolution."""
        return self.horizon_weights.get(resolution, self.horizon_weights["high"])

    def to_commitment_json(self) -> str:
        """Deterministic JSON for weight commitment hashing."""
        data = {
            "quantile_accuracy": self.quantile_accuracy,
            "calibration": self.calibration,
            "directional": self.directional,
            "goal_accuracy": self.goal_accuracy,
            "horizon_weights": self.horizon_weights,
        }
        return json.dumps(data, sort_keys=True)
