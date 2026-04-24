"""Prediction endpoints — miners submit predictions here."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction
from hope.validator.api.auth import MinerIdentity, verify_miner

router = APIRouter()


class PredictionSubmission(BaseModel):
    """Incoming prediction from a miner."""

    episode_id: str
    horizons: dict[str, dict]  # Raw horizon data


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

    Validates prediction format and stores for scoring after deadline.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    # Check deadline
    deadline_str = state.get("deadline")
    if deadline_str:
        deadline = datetime.fromisoformat(deadline_str)
        if datetime.now(timezone.utc) > deadline:
            raise HTTPException(status_code=400, detail="Prediction deadline has passed")

    # Get valid episode IDs
    episodes = state.get("episodes", [])
    valid_ids = {ep.episode_metadata.episode_id for ep in episodes}

    # Initialize predictions storage
    if "predictions" not in state:
        state["predictions"] = {}
    if miner.hotkey not in state["predictions"]:
        state["predictions"][miner.hotkey] = []

    accepted = 0
    rejected = 0
    errors = []

    for sub in submission.predictions:
        if sub.episode_id not in valid_ids:
            rejected += 1
            errors.append(f"{sub.episode_id}: unknown episode")
            continue

        try:
            # Parse and validate prediction
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

            state["predictions"][miner.hotkey].append(prediction)
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
        "total_predictions": len(state["predictions"].get(miner.hotkey, [])),
    }
