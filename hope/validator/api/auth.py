"""Hotkey authentication middleware for the validator HTTP API.

Miners authenticate by signing requests with their Bittensor hotkey.
For the simplified launch version, we accept a hotkey header and
verify it's registered on the subnet metagraph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)


@dataclass
class MinerIdentity:
    """Authenticated miner identity."""

    hotkey: str
    uid: Optional[int] = None


async def verify_miner(
    request: Request,
    x_miner_hotkey: str = Header(..., alias="X-Miner-Hotkey"),
) -> MinerIdentity:
    """FastAPI dependency that verifies the miner's hotkey.

    For launch (simplified): accepts the hotkey header and checks
    it against registered miners in the validator state.

    Future: full ed25519 signature verification with nonce.
    """
    if not x_miner_hotkey:
        raise HTTPException(status_code=401, detail="Missing X-Miner-Hotkey header")

    # Check against registered miners in validator state
    validator_state = request.app.state.validator
    registered_miners = validator_state.get("registered_miners", set())

    # For launch: if no metagraph loaded, accept any non-empty hotkey
    if registered_miners and x_miner_hotkey not in registered_miners:
        raise HTTPException(
            status_code=403,
            detail=f"Hotkey {x_miner_hotkey[:16]}... not registered on subnet",
        )

    return MinerIdentity(hotkey=x_miner_hotkey)
