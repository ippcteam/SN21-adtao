"""Prediction endpoints — miners submit predictions here.

Security:
- Submission window enforced (closed after deadline)
- Rate limited per miner by total prediction count
- Predictions deduplicated per (miner, episode_id) — last wins
- Batch size capped to prevent DoS
- Quantile values bounded to [-100, 100]
- Predictions must be signed (auth.py enforces)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction
from hope.validator.api.auth import MinerIdentity, verify_miner
from hope.validator.integrity import compute_prediction_hash, create_prediction_receipt

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate limiting: max total predictions per miner per epoch
# NOTE: In-memory state — requires single uvicorn worker. Resets on restart.
MAX_PREDICTIONS_PER_MINER = 500  # ~2x headroom for 200 episodes + resubmissions
MAX_BATCH_SIZE = 250  # Max predictions per single request

_prediction_count: dict[str, int] = defaultdict(int)
_current_epoch_id: str = ""

# Quantile value bounds — percentage deltas outside this range are rejected
QUANTILE_MIN = -100.0
QUANTILE_MAX = 100.0


def _check_rate_limit(hotkey: str, epoch_id: str, batch_size: int):
    """Enforce rate limit by total prediction count per miner per epoch."""
    global _current_epoch_id

    if epoch_id != _current_epoch_id:
        _prediction_count.clear()
        _current_epoch_id = epoch_id

    current = _prediction_count[hotkey]
    if current + batch_size > MAX_PREDICTIONS_PER_MINER:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({MAX_PREDICTIONS_PER_MINER} predictions per epoch, "
                   f"you have {current}, tried to add {batch_size})",
        )


def _validate_quantile_bounds(value: float, field_name: str):
    """Reject extreme quantile values."""
    if value < QUANTILE_MIN or value > QUANTILE_MAX:
        raise ValueError(f"{field_name} must be between {QUANTILE_MIN} and {QUANTILE_MAX}, got {value}")


class PredictionSubmission(BaseModel):
    """Incoming prediction from a miner."""

    episode_id: str
    horizons: dict[str, dict]


class BatchPredictionSubmission(BaseModel):
    """Batch of predictions from a miner. Capped at MAX_BATCH_SIZE."""

    predictions: list[PredictionSubmission]

    @field_validator("predictions")
    @classmethod
    def check_batch_size(cls, v):
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch too large: {len(v)} predictions, max {MAX_BATCH_SIZE}")
        return v


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
    - Rate limiting by total prediction count
    - Batch size cap
    - Quantile value bounds
    - Deduplication: last prediction per episode wins
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    # C8: Default to closed, require explicit True
    submission_open = state.get("submission_open", False)
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

    # C4: Rate limit by prediction count, not request count
    _check_rate_limit(miner.hotkey, epoch_id, len(submission.predictions))

    # Get valid episode IDs
    episodes = state.get("episodes", [])
    valid_ids = {ep.episode_metadata.episode_id for ep in episodes}

    # C1: Predictions stored as dict keyed by (hotkey, episode_id) — last wins
    predictions = state.get("predictions", {})
    if miner.hotkey not in predictions:
        predictions[miner.hotkey] = {}

    accepted = 0
    rejected = 0
    errors = []
    accepted_receipts = []

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

                # C10: Validate quantile bounds
                for metric in ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct"):
                    q_data = h_data[metric]
                    for q_field in ("p10", "p50", "p90"):
                        _validate_quantile_bounds(q_data[q_field], f"{metric}.{q_field}")

                horizons[h_key] = HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(**h_data["cost_delta_pct"]),
                    conversions_delta_pct=QuantilePrediction(**h_data["conversions_delta_pct"]),
                    efficiency_delta_pct=QuantilePrediction(**h_data["efficiency_delta_pct"]),
                    goal_miss_probability=h_data.get("goal_miss_probability", 0.0),
                    instability_risk=h_data.get("instability_risk", 0.0),
                )

            # Require ALL active horizons (7 and 14)
            required_horizons = {"7", "14"}
            if set(horizons.keys()) != required_horizons:
                missing = required_horizons - set(horizons.keys())
                rejected += 1
                errors.append(f"{sub.episode_id}: missing required horizons {missing}")
                continue

            prediction = Prediction(
                episode_id=sub.episode_id,
                miner_id=miner.hotkey,
                submitted_at=datetime.now(timezone.utc),
                horizons=horizons,
            )

            # C1: Deduplicate — last prediction per episode wins
            predictions[miner.hotkey][sub.episode_id] = prediction

            # Create content-binding receipt for this prediction
            pred_hash = compute_prediction_hash(prediction.model_dump(mode="json"))
            receipt = create_prediction_receipt(
                miner_hotkey=miner.hotkey,
                episode_id=sub.episode_id,
                prediction_hash=pred_hash,
                received_at=prediction.submitted_at.isoformat(),
            )

            # Store receipts for later verification by miners
            prediction_receipts = state.get("prediction_receipts", {})
            if miner.hotkey not in prediction_receipts:
                prediction_receipts[miner.hotkey] = {}
            prediction_receipts[miner.hotkey][sub.episode_id] = {
                "prediction_hash": receipt.prediction_hash,
                "receipt_hash": receipt.receipt_hash,
                "received_at": receipt.received_at,
            }

            accepted += 1
            accepted_receipts.append({
                "episode_id": sub.episode_id,
                "prediction_hash": pred_hash,
                "receipt_hash": receipt.receipt_hash,
            })

        except (ValueError, KeyError) as e:
            rejected += 1
            errors.append(f"{sub.episode_id}: {str(e)[:100]}")
        except Exception as e:
            rejected += 1
            errors.append(f"{sub.episode_id}: {str(e)[:100]}")

    # Update rate limit counter
    _prediction_count[miner.hotkey] += accepted

    miner_preds = predictions.get(miner.hotkey, {})

    return {
        "epoch_id": epoch_id,
        "miner_hotkey": miner.hotkey[:16] + "...",
        "accepted": accepted,
        "rejected": rejected,
        "errors": errors if errors else None,
        "total_episodes_covered": len(miner_preds),
        "receipts": accepted_receipts if accepted_receipts else None,
    }
