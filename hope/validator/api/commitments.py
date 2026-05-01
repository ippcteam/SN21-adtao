"""Commitment endpoints — public integrity proofs at each phase.

Two commitments are published:
1. Episode commitment — BEFORE miners receive episodes (proves episodes not changed)
2. Full commitment — AFTER scoring (binds episodes + predictions + outcomes + weights)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/{epoch_id}/episode-commitment")
async def get_episode_commitment(epoch_id: str, request: Request):
    """Get the episode commitment hash — published BEFORE miners receive episodes.

    Public endpoint. Miners should fetch this, then fetch episodes, and verify
    that SHA256(episodes_json_sorted) matches the episode_hash returned here.
    This proves episodes were not modified after distribution.
    """
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    ep_commitment = state.get("episode_commitment")
    if not ep_commitment:
        raise HTTPException(status_code=409, detail="Episode commitment not yet published")

    return {
        "epoch_id": epoch_id,
        "episode_hash": ep_commitment.get("episode_hash"),
        "episode_count": ep_commitment.get("episode_count"),
        "committed_at": ep_commitment.get("committed_at"),
        "algorithm": "sha256",
        "how_to_verify": (
            "After fetching episodes, compute "
            "SHA256(json.dumps([ep.model_dump() for ep in episodes], sort_keys=True, default=str)) "
            "and compare with episode_hash."
        ),
    }


@router.get("/{epoch_id}/commitment")
async def get_commitment(epoch_id: str, request: Request):
    """Get the full commitment proof — published AFTER scoring.

    Public endpoint. Binds episodes + predictions + outcomes + weights.
    Use with /verification endpoint to independently verify scores.
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
        "episode_hash": commitment.get("episode_hash"),
        "prediction_merkle_root": commitment.get("prediction_merkle_root"),
        "prediction_count": commitment.get("prediction_count"),
        "committed_at": commitment.get("committed_at"),
        "episode_count": commitment.get("episode_count"),
        "algorithm": "sha256",
        "how_to_verify": (
            "Compute SHA256(episode_hash + prediction_merkle_root + outcomes_json + salt + weights_json) "
            "using the values from /verification. Must match commitment_hash."
        ),
    }
