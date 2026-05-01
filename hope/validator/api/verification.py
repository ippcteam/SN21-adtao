"""Verification endpoints — post-scoring reveal for transparency.

After scoring is complete, miners and observers can:
1. Download revealed outcomes
2. See the scoring weights used
3. Verify outcomes match the pre-committed hash
4. Check individual episode scores
5. Get Merkle inclusion proofs for their predictions
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from hope.validator.api.auth import MinerIdentity, verify_miner

router = APIRouter()


@router.get("/{epoch_id}/verification")
async def get_verification(epoch_id: str, request: Request):
    """Get revealed outcomes and scoring weights after epoch scoring.

    Public endpoint — available after scoring is complete.
    Contains everything needed to independently verify scores.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    reveal = state.get("reveal")
    if not reveal:
        raise HTTPException(
            status_code=409,
            detail="Scoring not yet complete — outcomes not revealed",
        )

    return {
        "epoch_id": epoch_id,
        "status": "revealed",
        "commitment_hash": reveal.get("commitment_hash"),
        "salt": reveal.get("salt"),
        "scoring_weights": reveal.get("scoring_weights"),
        "outcomes": reveal.get("outcomes"),
        "scores": reveal.get("scores"),
        # Integrity proofs
        "episode_hash": reveal.get("episode_hash"),
        "prediction_merkle_root": reveal.get("prediction_merkle_root"),
        "prediction_count": reveal.get("prediction_count"),
        "scoring_hash": reveal.get("scoring_hash"),
        "outcomes_fetched_at": reveal.get("outcomes_fetched_at"),
        "submissions_closed_at": reveal.get("submissions_closed_at"),
        "verification_instructions": (
            "To verify the commitment: compute "
            "SHA256(episode_hash + prediction_merkle_root + outcomes_json + salt + weights_json) "
            "where outcomes_json = json.dumps(outcomes, sort_keys=True, default=str). "
            "The result must match commitment_hash. "
            "To verify your score: re-run the scoring algorithm (hope/scoring/) "
            "with the revealed outcomes and your predictions."
        ),
    }


@router.get("/{epoch_id}/scores")
async def get_scores(epoch_id: str, request: Request):
    """Get per-miner scores for the epoch.

    Public endpoint — available after scoring is complete.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    scores = state.get("miner_scores")
    if not scores:
        raise HTTPException(status_code=409, detail="Scoring not yet complete")

    return {
        "epoch_id": epoch_id,
        "miner_count": len(scores),
        "scores": {
            miner_id: {
                "raw_score": s.raw_score,
                "skill_score": s.skill_score,
                "null_penalty": s.null_penalty,
                "coverage_penalty": s.coverage_penalty,
                "final_score": s.final_score,
                "episodes_scored": s.episodes_scored,
                "episodes_total": s.episodes_total,
                "coverage_fraction": s.coverage_fraction,
            }
            for miner_id, s in scores.items()
        },
    }


@router.get("/{epoch_id}/my-predictions")
async def get_my_prediction_proof(
    epoch_id: str,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Get a miner's own prediction hashes and Merkle inclusion proof.

    Authenticated endpoint — miners can verify their predictions were included
    in the scored set by checking against the prediction_merkle_root.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    reveal = state.get("reveal")
    if not reveal:
        raise HTTPException(status_code=409, detail="Scoring not yet complete")

    # Get this miner's prediction receipts
    prediction_receipts = state.get("prediction_receipts", {})
    miner_receipts = prediction_receipts.get(miner.hotkey, {})

    if not miner_receipts:
        return {
            "epoch_id": epoch_id,
            "miner_hotkey": miner.hotkey[:16] + "...",
            "predictions_found": 0,
            "prediction_merkle_root": reveal.get("prediction_merkle_root"),
        }

    return {
        "epoch_id": epoch_id,
        "miner_hotkey": miner.hotkey[:16] + "...",
        "predictions_found": len(miner_receipts),
        "prediction_merkle_root": reveal.get("prediction_merkle_root"),
        "predictions": miner_receipts,
    }
