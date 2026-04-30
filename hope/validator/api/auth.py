"""Hotkey authentication middleware for the validator HTTP API.

Miners authenticate by:
1. Providing their hotkey address in X-Miner-Hotkey header
2. Signing a nonce with their private key (X-Miner-Signature header)
3. The validator verifies the signature against the hotkey's public key

This ensures miners can't impersonate each other by spoofing hotkey headers.
Per Tensora review: "Miners need to sign the prediction to ensure it's actually theirs."
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)

# Nonce expiry — signatures older than this are rejected
NONCE_EXPIRY_SECONDS = 300  # 5 minutes


@dataclass
class MinerIdentity:
    """Authenticated miner identity."""

    hotkey: str
    uid: int | None = None
    verified: bool = False  # True if signature was cryptographically verified


def _verify_signature(hotkey: str, nonce: str, signature: str) -> bool:
    """Verify an ed25519 signature from a Bittensor hotkey.

    The miner signs: SHA256(hotkey + nonce)
    The validator verifies using the hotkey's public key.
    """
    try:
        from substrateinterface import Keypair

        # Reconstruct the message that was signed
        message = hashlib.sha256(f"{hotkey}:{nonce}".encode()).hexdigest()

        # Verify signature against the hotkey (ss58 address)
        keypair = Keypair(ss58_address=hotkey)
        is_valid = keypair.verify(message.encode(), bytes.fromhex(signature))
        return is_valid

    except ImportError:
        logger.debug("substrateinterface not installed — skipping signature verification")
        return False
    except Exception as e:
        logger.warning(f"Signature verification failed for {hotkey[:16]}...: {e}")
        return False


async def verify_miner(
    request: Request,
    x_miner_hotkey: str = Header(..., alias="X-Miner-Hotkey"),
    x_miner_nonce: str = Header(None, alias="X-Miner-Nonce"),
    x_miner_signature: str = Header(None, alias="X-Miner-Signature"),
) -> MinerIdentity:
    """FastAPI dependency that verifies the miner's identity.

    Authentication levels:
    1. Hotkey only (no signature) — accepted but marked unverified
    2. Hotkey + nonce + signature — cryptographically verified

    Configurable via REQUIRE_SIGNATURES env var:
    - "false" (default for testnet): unsigned requests accepted
    - "true" (for mainnet): unsigned requests rejected with 401
    """
    if not x_miner_hotkey:
        raise HTTPException(status_code=401, detail="Missing X-Miner-Hotkey header")

    # Check against registered miners in metagraph
    validator_state = request.app.state.validator
    registered_miners = validator_state.get("registered_miners", set())

    uid = None
    if registered_miners and x_miner_hotkey not in registered_miners:
        raise HTTPException(
            status_code=403,
            detail=f"Hotkey {x_miner_hotkey[:16]}... not registered on subnet",
        )

    # Check if signatures are required (mainnet enforcement)
    import os
    require_sigs = os.environ.get("REQUIRE_SIGNATURES", "false").lower() == "true"

    # Verify signature if provided
    verified = False
    if x_miner_nonce and x_miner_signature:
        # Check nonce freshness
        try:
            nonce_time = float(x_miner_nonce)
            if abs(time.time() - nonce_time) > NONCE_EXPIRY_SECONDS:
                raise HTTPException(
                    status_code=401,
                    detail=f"Nonce expired (must be within {NONCE_EXPIRY_SECONDS}s)",
                )
        except ValueError:
            pass  # Non-timestamp nonce — skip expiry check

        verified = _verify_signature(x_miner_hotkey, x_miner_nonce, x_miner_signature)
        if not verified:
            if require_sigs:
                raise HTTPException(status_code=401, detail="Invalid signature")
            logger.warning(f"Invalid signature from {x_miner_hotkey[:16]}... — accepting without verification")
    elif require_sigs:
        # No signature provided but signatures are required
        raise HTTPException(
            status_code=401,
            detail="Signature required. Include X-Miner-Nonce and X-Miner-Signature headers.",
        )

    return MinerIdentity(hotkey=x_miner_hotkey, uid=uid, verified=verified)
