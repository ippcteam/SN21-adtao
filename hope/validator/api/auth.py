"""Hotkey authentication middleware for the validator HTTP API.

Miners authenticate by:
1. Providing their hotkey address in X-Miner-Hotkey header
2. Providing a numeric nonce (unix timestamp) in X-Miner-Nonce header
3. Signing SHA256(hotkey:nonce:METHOD:path:body_hash) with their hotkey private key
4. Sending the signature in X-Miner-Signature header

The signature binds to the full request — method, path, and body — preventing
replay attacks across endpoints. Nonces are tracked to prevent replay within
the expiry window.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)

# Nonce expiry — signatures older than this are rejected
NONCE_EXPIRY_SECONDS = 300  # 5 minutes

# Max tracked nonces (bounded to prevent memory leak)
_MAX_SEEN_NONCES = 50_000

# Seen nonces: (hotkey, nonce) -> timestamp. Prevents replay within expiry window.
_seen_nonces: OrderedDict[tuple[str, str], float] = OrderedDict()

# Keypair source: bittensor_wallet (modern, ships with bittensor>=10) is
# preferred. Fall back to substrateinterface for older deployments. If
# neither is available, signature verification is disabled and the
# validator fails closed (see verify_signature).
try:
    from bittensor_wallet.bittensor_wallet import Keypair
    _HAS_CRYPTO = True
except ImportError:
    try:
        from substrateinterface import Keypair  # type: ignore
        _HAS_CRYPTO = True
    except ImportError:
        _HAS_CRYPTO = False
        logger.critical(
            "Neither bittensor_wallet nor substrateinterface available — "
            "signature verification DISABLED."
        )


@dataclass
class MinerIdentity:
    """Authenticated miner identity."""

    hotkey: str
    uid: int | None = None
    verified: bool = False


def _evict_expired_nonces() -> None:
    """Remove expired nonces from the seen set."""
    now = time.time()
    cutoff = now - NONCE_EXPIRY_SECONDS - 10  # small buffer
    while _seen_nonces:
        key, ts = next(iter(_seen_nonces.items()))
        if ts < cutoff:
            _seen_nonces.pop(key)
        else:
            break


def _verify_signature(
    hotkey: str,
    nonce: str,
    signature: str,
    method: str,
    path: str,
    body_hash: str,
) -> bool:
    """Verify an ed25519 signature from a Bittensor hotkey.

    The miner signs: SHA256(hotkey:nonce:METHOD:path:body_hash)
    This binds the signature to the exact request, preventing replay.
    """
    if not _HAS_CRYPTO:
        return False

    try:
        # Reconstruct the message that was signed (bound to full request)
        message = hashlib.sha256(
            f"{hotkey}:{nonce}:{method}:{path}:{body_hash}".encode()
        ).hexdigest()

        keypair = Keypair(ss58_address=hotkey)
        return keypair.verify(message.encode(), bytes.fromhex(signature))

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

    Authentication is always required. Miners MUST sign requests.
    REQUIRE_SIGNATURES can be set to "false" ONLY for local development/testing.

    The signature covers the full request: method, path, and body hash.
    Nonces are tracked to prevent replay attacks.
    """
    if not x_miner_hotkey:
        raise HTTPException(status_code=401, detail="Missing X-Miner-Hotkey header")

    # Check against registered miners in metagraph
    validator_state = request.app.state.validator
    registered_miners = validator_state.get("registered_miners", set())

    # Fail closed: if metagraph not synced, reject all requests
    if not registered_miners:
        raise HTTPException(
            status_code=503,
            detail="Validator not ready — metagraph not synced",
        )

    if x_miner_hotkey not in registered_miners:
        raise HTTPException(
            status_code=403,
            detail=f"Hotkey {x_miner_hotkey[:16]}... not registered on subnet",
        )

    # Look up UID
    uid_map = validator_state.get("uid_map", {})
    uid = uid_map.get(x_miner_hotkey)

    # Check if signatures are required (default: true for production safety)
    require_sigs = os.environ.get("REQUIRE_SIGNATURES", "true").lower() != "false"

    if not x_miner_nonce or not x_miner_signature:
        if require_sigs:
            raise HTTPException(
                status_code=401,
                detail="Signature required. Include X-Miner-Nonce and X-Miner-Signature headers.",
            )
        # Dev mode only — no signature, marked unverified
        logger.warning(f"Unsigned request from {x_miner_hotkey[:16]}... (REQUIRE_SIGNATURES=false)")
        return MinerIdentity(hotkey=x_miner_hotkey, uid=uid, verified=False)

    # --- Signature provided: validate fully ---

    # B3: Reject non-numeric nonces
    try:
        nonce_time = float(x_miner_nonce)
    except ValueError:
        raise HTTPException(status_code=401, detail="Nonce must be a numeric timestamp")

    # Check nonce freshness
    if abs(time.time() - nonce_time) > NONCE_EXPIRY_SECONDS:
        raise HTTPException(
            status_code=401,
            detail=f"Nonce expired (must be within {NONCE_EXPIRY_SECONDS}s)",
        )

    # B2: Reject replayed nonces
    nonce_key = (x_miner_hotkey, x_miner_nonce)
    if nonce_key in _seen_nonces:
        raise HTTPException(status_code=401, detail="Nonce already used")

    # B1: Compute body hash and verify signature bound to full request
    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest() if body else hashlib.sha256(b"").hexdigest()

    verified = _verify_signature(
        hotkey=x_miner_hotkey,
        nonce=x_miner_nonce,
        signature=x_miner_signature,
        method=request.method,
        path=request.url.path,
        body_hash=body_hash,
    )

    if not verified:
        # B5: Fail closed — if a signature is provided it MUST validate
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Track nonce as used (after successful verification)
    _seen_nonces[nonce_key] = time.time()
    if len(_seen_nonces) > _MAX_SEEN_NONCES:
        _evict_expired_nonces()
    # Hard cap if eviction didn't free enough
    while len(_seen_nonces) > _MAX_SEEN_NONCES:
        _seen_nonces.popitem(last=False)

    return MinerIdentity(hotkey=x_miner_hotkey, uid=uid, verified=True)
