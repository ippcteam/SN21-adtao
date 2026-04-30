"""Prediction endpoints — miners submit predictions here.

Security (per Tensora review):
- Submission window is enforced (closed after deadline)
- Rate limited per miner hotkey
- Predictions must be signed (TODO: full signature verification)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction
from hope.validator.api.auth import MinerIdentity, verify_miner

router = APIRouter()

# Rate limiting: max submissions per miner per epoch
# Per Tensora: "Why would a miner need to call any endpoint more than 5/epoch?"
RATE_LIMIT_PER_EPOCH = 5
_submission_count: dict[str, int] = defaultdict(int)
_current_epoch_id: str = ""


def _check_rate_limit(hotkey: str, epoch_id: str):
    """Enforce rate limit per miner per epoch."""
    global _current_epoch_id

    # Reset counters on new epoch
    if epoch_id != _current_epoch_id:
        _submission_count.clear()
        _current_epoch_id = epoch_id

    if _submission_count[hotkey] >= RATE_LIMIT_PER_EPOCH:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({RATE_LIMIT_PER_EPOCH} submissions per epoch)",
        )
    _submission_count[hotkey] += 1


class PredictionSubmission(BaseModel):
    """Incoming prediction from a miner."""

    episode_id: str
    horizons: dict[str, dict]


class BatchPredictionSubmission(BaseModel):
    """Batch of predictions from a miner."""

    predictions: list[PredictionSubmission]


@router.post("/{epoch_id}/predictions")
async def submit_predictions(
    epoch_id: str,
    submission: BatchPredictionSubmission,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Submit predictions for one or more episodes.

    Enforces:
    - Submission window (rejects after deadline)
    - Rate limiting per miner
    - Prediction validation (quantile ordering, ranges)
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    # Check submission window is open
    # LiveState computes this dynamically; plain dicts default to True for testing
    submission_open = state.get("submission_open", True)
    if not submission_open:
        raise HTTPException(
            status_code=403,
            detail="Submission window is closed. Predictions are no longer accepted.",
        )

    # Check deadline explicitly
    deadline_str = state.get("deadline")
    if deadline_str:
        deadline = datetime.fromisoformat(deadline_str)
        if datetime.now(timezone.utc) > deadline:
            raise HTTPException(status_code=403, detail="Prediction deadline has passed")

    # Rate limit (5 submissions per epoch per miner)
    _check_rate_limit(miner.hotkey, epoch_id)

    # Get valid episode IDs
    episodes = state.get("episodes", [])
    valid_ids = {ep.episode_metadata.episode_id for ep in episodes}

    # Get predictions dict
    predictions = state.get("predictions", {})
    if miner.hotkey not in predictions:
        predictions[miner.hotkey] = []

    accepted = 0
    rejected = 0
    errors = []

    for sub in submission.predictions:
        if sub.episode_id not in valid_ids:
            rejected += 1
            errors.append(f"{sub.episode_id}: unknown episode")
            continue

        try:
            horizons = {}
            for h_key, h_data in sub.horizons.items():
                if h_key not in ("7", "14"):
                    continue
                horizons[h_key] = HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(**h_data["cost_delta_pct"]),
                    conversions_delta_pct=QuantilePrediction(**h_data["conversions_delta_pct"]),
                    efficiency_delta_pct=QuantilePrediction(**h_data["efficiency_delta_pct"]),
                    goal_miss_probability=h_data.get("goal_miss_probability", 0.0),
                    instability_risk=h_data.get("instability_risk", 0.0),
                )

            if not horizons:
                rejected += 1
                errors.append(f"{sub.episode_id}: no valid horizons")
                continue

            prediction = Prediction(
                episode_id=sub.episode_id,
                miner_id=miner.hotkey,
                submitted_at=datetime.now(timezone.utc),
                horizons=horizons,
            )

            predictions[miner.hotkey].append(prediction)
            accepted += 1

        except Exception as e:
            rejected += 1
            errors.append(f"{sub.episode_id}: {str(e)[:100]}")

    return {
        "epoch_id": epoch_id,
        "miner_hotkey": miner.hotkey[:16] + "...",
        "accepted": accepted,
        "rejected": rejected,
        "errors": errors if errors else None,
        "total_predictions": len(predictions.get(miner.hotkey, [])),
    }
