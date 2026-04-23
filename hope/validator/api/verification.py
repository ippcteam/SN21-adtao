"""Verification endpoints — post-scoring reveal for transparency.

After scoring is complete, miners and observers can:
1. Download revealed outcomes
2. See the scoring weights used
3. Verify outcomes match the pre-committed hash
4. Check individual episode scores
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

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
        "verification_instructions": (
            "To verify: compute SHA256(outcomes_json + salt + weights_json) "
            "and compare with commitment_hash. They must match."
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
                "final_score": s.final_score,
                "episodes_scored": s.episodes_scored,
            }
            for miner_id, s in scores.items()
        },
    }
