"""Episode endpoints — miners fetch challenge episodes here."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from hope.validator.api.auth import MinerIdentity, verify_miner

router = APIRouter()


@router.get("/{epoch_id}/episodes")
async def list_episodes(
    epoch_id: str,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """List all episodes for an epoch. Returns episode metadata without full payloads."""
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    episodes = state.get("episodes", [])
    return {
        "epoch_id": epoch_id,
        "episode_count": len(episodes),
        "episodes": [
            {
                "episode_id": ep.episode_metadata.episode_id,
                "schema_version": ep.episode_metadata.schema_version,
                "measurement_resolution": ep.episode_metadata.measurement_resolution,
                "coverage_status": ep.episode_metadata.coverage_status,
                "action_type": ep.action_bundle.actions[0].type if ep.action_bundle.actions else None,
            }
            for ep in episodes
        ],
    }


@router.get("/{epoch_id}/episodes/{episode_id}")
async def get_episode(
    epoch_id: str,
    episode_id: str,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Fetch a single episode's full payload."""
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    episodes = state.get("episodes", [])
    for ep in episodes:
        if ep.episode_metadata.episode_id == episode_id:
            return {"episode_id": episode_id, "payload": ep.model_dump(mode="json")}

    raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")


@router.get("/{epoch_id}/episodes_batch")
async def get_episodes_batch(
    epoch_id: str,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Fetch all episode payloads in one request. More efficient for miners."""
    state = request.app.state.validator
    current_epoch = state.get("current_epoch_id")

    if epoch_id != current_epoch:
        raise HTTPException(status_code=404, detail=f"Epoch {epoch_id} not found")

    episodes = state.get("episodes", [])
    return {
        "epoch_id": epoch_id,
        "episode_count": len(episodes),
        "episodes": [
            {
                "episode_id": ep.episode_metadata.episode_id,
                "payload": ep.model_dump(mode="json"),
            }
            for ep in episodes
        ],
    }
