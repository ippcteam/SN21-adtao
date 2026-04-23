"""Commitment endpoints — public proof that outcomes were committed before scoring."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/{epoch_id}/commitment")
async def get_commitment(epoch_id: str, request: Request):
    """Get the commitment proof for an epoch.

    Public endpoint — no auth required. Anyone can verify that
    the validator committed to outcomes before distributing episodes.

    Returns the commitment hash and metadata. The actual outcomes
    are revealed only after the scoring deadline via /verification.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    commitment = state.get("commitment")
    if not commitment:
        raise HTTPException(status_code=409, detail="Commitment not yet published")

    return {
        "epoch_id": epoch_id,
        "commitment_hash": commitment.get("hash"),
        "merkle_root": commitment.get("merkle_root"),
        "committed_at": commitment.get("committed_at"),
        "episode_count": commitment.get("episode_count"),
        "algorithm": "sha256",
        "status": "committed",
    }
