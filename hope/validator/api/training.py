"""Training data endpoint — miners fetch historical episodes with known outcomes.

Serves from the bundled training dataset (data/training/training_episodes.json),
NOT from the current epoch's outcomes (which are deferred until after deadline).

Requires miner authentication to prevent abuse.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from hope.validator.api.auth import MinerIdentity, verify_miner

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

# Pagination defaults
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


@router.get("/training/episodes")
async def get_training_episodes(
    miner: MinerIdentity = Depends(verify_miner),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Items per page"),
):
    """Get historical episodes with known outcomes for model training.

    Paginated to prevent large response abuse. Requires miner authentication.
    """
    start = (page - 1) * page_size
    end = start + page_size
    page_data = _training_data[start:end]
    total_pages = (len(_training_data) + page_size - 1) // page_size if _training_data else 0

    return {
        "training_episodes": page_data,
        "count": len(page_data),
        "total": len(_training_data),
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "schema_version": "v1.9",
    }


@router.get("/training/summary")
async def get_training_summary(
    miner: MinerIdentity = Depends(verify_miner),
):
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
