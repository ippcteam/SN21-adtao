"""Training data endpoint — miners fetch historical episodes with known outcomes.

Serves from the bundled training dataset (data/training/training_episodes.json),
NOT from the current epoch's outcomes (which are deferred until after deadline).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger(__name__)

# Load bundled training data once at import time
_training_data = []
_training_path = Path(__file__).parent.parent.parent.parent / "data" / "training" / "training_episodes.json"
try:
    if _training_path.exists():
        with open(_training_path) as f:
            _training_data = json.load(f)
        logger.info(f"Loaded {len(_training_data)} training examples from {_training_path}")
except Exception as e:
    logger.warning(f"Failed to load training data: {e}")


@router.get("/training/episodes")
async def get_training_episodes():
    """Get historical episodes with known outcomes for model training.

    Serves from the bundled training dataset — not from the current epoch.
    This ensures training data is always available regardless of epoch state.

    Each training example contains:
    - input: the full episode payload (same format as live epochs)
    - outcome: the actual t7/t14 deltas (what really happened)
    - scoring_metadata: goal type, resolution, coverage status
    """
    return {
        "training_episodes": _training_data,
        "count": len(_training_data),
        "schema_version": "v1.9",
        "source": "bundled (data/training/training_episodes.json)",
        "description": (
            "Each example has 'input' (episode payload) and 'outcome' (actual deltas). "
            "Train your model to predict outcome from input. "
            "See docs/miner_quickstart.md for scoring details."
        ),
    }


@router.get("/training/summary")
async def get_training_summary():
    """Get summary stats about available training data."""
    action_types: dict[str, int] = {}
    with_t7 = 0
    with_t14 = 0

    for ex in _training_data:
        outcome = ex.get("outcome", {})
        if outcome.get("t7"):
            with_t7 += 1
        if outcome.get("t14"):
            with_t14 += 1

        actions = ex.get("input", {}).get("action_bundle", {}).get("actions", [])
        if actions:
            at = actions[0].get("type", "unknown")
            action_types[at] = action_types.get(at, 0) + 1

    return {
        "total_episodes": len(_training_data),
        "with_t7_outcomes": with_t7,
        "with_t14_outcomes": with_t14,
        "action_type_distribution": action_types,
        "schema_version": "v1.9",
    }
