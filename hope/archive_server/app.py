"""FastAPI app for SN21 archive servers (Tier-2 operator shadow + Tier-3 miner self).

Endpoints:

  POST /archive/{epoch_id}/{miner_identity}/{sha256_hex}
    Body: raw AES_ct bytes (Content-Type: application/octet-stream).
    Verifies the body's SHA-256 equals `sha256_hex`.
    Optional auth: X-Miner-Hotkey + X-Miner-Signature (covers the digest).
    Returns 201 Created on store, 200 OK if the digest already exists,
    400 on body/digest mismatch, 401 on auth fail (when enforced),
    413 on body too large.

  GET /archive/{epoch_id}/{miner_identity}/{sha256_hex}
    Returns the bytes if present, 404 otherwise.
    Open access — verifiers + validators must be able to fetch without
    credentials. The on-chain SHA-256 commit is the integrity anchor.

  GET /healthz
    Returns 200 with a small JSON body for liveness checks.

Path components are validated to forbid `/` and `..`. Body size is capped
at `max_body_bytes` (default 1 MiB) — a real AES_ct prediction is a few
hundred bytes; the cap is purely DoS hardening.

Auth model:
  - When `require_signed_uploads=False` (default Tier-3 miner self):
    open POST. Anyone can upload, but a digest mismatch gets 400.
  - When `require_signed_uploads=True` (Tier-2 operator shadow):
    POST must include X-Miner-Hotkey (SS58 of the miner) and
    X-Miner-Signature = ed25519_sign(digest, miner_hotkey_privkey)
    where digest = SHA-256(b"sn21-archive-v1:" + epoch_id_utf8 +
                            b":" + sha256_hex_ascii).
    The server verifies the signature against the SS58's underlying
    ed25519 public key. This binds the upload to a specific miner so a
    third party can't fill another miner's slot.

Run via:
    from hope.archive_server.app import build_app
    from hope.archive_server.store import FilesystemStore
    app = build_app(store=FilesystemStore("/var/lib/sn21-archive"),
                    require_signed_uploads=True)
    # then: uvicorn app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hashlib
import logging
import time

from fastapi import FastAPI, Header, HTTPException, Request, Response

from hope.archive_server.metrics import (
    build_registry,
    render_text,
    status_group,
)
from hope.archive_server.store import ArchiveStore

logger = logging.getLogger("archive_server")


DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
ARCHIVE_AUTH_DOMAIN = b"sn21-archive-v1"


def _expected_signed_message(epoch_id: str, sha256_hex: str) -> bytes:
    """Domain-separated message a miner must sign to upload to Tier-2."""
    return (
        ARCHIVE_AUTH_DOMAIN
        + b":"
        + epoch_id.encode("utf-8")
        + b":"
        + sha256_hex.lower().encode("ascii")
    )


def _verify_miner_signature(
    *,
    miner_hotkey_ss58: str,
    signature_hex: str,
    epoch_id: str,
    sha256_hex: str,
) -> bool:
    """Verify the SS58-derived ed25519 key signed our domain-separated message.

    Uses Bittensor's `Keypair.verify` which handles SS58 → public-key
    derivation. Returns False on any error so callers can map cleanly to a
    401 response.
    """
    try:
        from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
    except Exception:
        try:
            from substrateinterface import Keypair  # type: ignore
        except Exception as e:
            logger.error("no Keypair available for signature verification: %s", e)
            return False

    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    try:
        kp = Keypair(ss58_address=miner_hotkey_ss58)
        msg = _expected_signed_message(epoch_id, sha256_hex)
        return bool(kp.verify(msg, sig))
    except Exception:
        return False


def build_app(
    *,
    store: ArchiveStore,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    require_signed_uploads: bool = False,
) -> FastAPI:
    """Construct a FastAPI app for an archive server.

    Args:
        store: pluggable storage backend (InMemoryStore or FilesystemStore).
        max_body_bytes: per-request body cap.
        require_signed_uploads: when True, POSTs must carry a valid
            X-Miner-Hotkey + X-Miner-Signature pair binding the upload to
            the named miner. Tier-2 (operator shadow) sets this; Tier-3 self
            archives typically leave it False.

    Returns:
        FastAPI app instance ready to mount under uvicorn or hypercorn.
    """
    app = FastAPI(title="SN21 Archive", version="1.0")
    registry, metrics = build_registry()
    app.state.metrics_registry = registry
    app.state.metrics = metrics

    @app.get("/")
    async def root():
        return {"ok": True, "service": "sn21-archive",
                "endpoints": {"health": "/healthz",
                              "objects": "/archive/{epoch_id}/{miner_identity}/{sha256_hex}",
                              "metrics": "/metrics"}}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": "sn21-archive"}

    @app.get("/health")
    async def health():
        # Alias for /healthz so a miner probing the conventional /health
        # path doesn't get a 404 and assume the archive is down.
        return {"ok": True, "service": "sn21-archive"}

    @app.get("/metrics")
    async def metrics_endpoint():
        body, content_type = render_text(app.state.metrics_registry)
        return Response(content=body, media_type=content_type)

    @app.post("/archive/{epoch_id}/{miner_identity}/{sha256_hex}")
    async def upload(
        request: Request,
        epoch_id: str,
        miner_identity: str,
        sha256_hex: str,
        x_miner_hotkey: str | None = Header(None, alias="X-Miner-Hotkey"),
        x_miner_signature: str | None = Header(None, alias="X-Miner-Signature"),
    ):
        start = time.monotonic()
        outcome = "ok"
        status_code = 201
        body_len = 0
        try:
            if "/" in epoch_id or ".." in epoch_id:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="invalid epoch_id")
            if "/" in miner_identity or ".." in miner_identity:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="invalid miner_identity")
            if len(sha256_hex) != 64:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="sha256_hex must be 64 hex chars")
            try:
                expected_digest = bytes.fromhex(sha256_hex.lower())
            except ValueError as e:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="sha256_hex is not valid hex") from e

            body = await request.body()
            body_len = len(body)
            if body_len > max_body_bytes:
                outcome = "body_too_large"
                status_code = 413
                raise HTTPException(
                    status_code=413,
                    detail=f"body too large: {len(body)} > {max_body_bytes}",
                )
            actual = hashlib.sha256(body).digest()
            if actual != expected_digest:
                outcome = "sha_mismatch"
                status_code = 400
                metrics["sha_mismatch_total"].inc()
                raise HTTPException(status_code=400, detail="body sha256 does not match path")

            if require_signed_uploads:
                if not x_miner_hotkey or not x_miner_signature:
                    outcome = "auth_fail"
                    status_code = 401
                    raise HTTPException(
                        status_code=401,
                        detail="X-Miner-Hotkey + X-Miner-Signature required",
                    )
                if not _verify_miner_signature(
                    miner_hotkey_ss58=x_miner_hotkey,
                    signature_hex=x_miner_signature,
                    epoch_id=epoch_id,
                    sha256_hex=sha256_hex.lower(),
                ):
                    outcome = "auth_fail"
                    status_code = 401
                    raise HTTPException(status_code=401, detail="signature verification failed")
                if miner_identity != x_miner_hotkey:
                    outcome = "auth_fail"
                    status_code = 403
                    raise HTTPException(
                        status_code=403,
                        detail="miner_identity must equal X-Miner-Hotkey for Tier-2",
                    )

            if store.exists(
                epoch_id=epoch_id,
                miner_identity=miner_identity,
                sha256=expected_digest,
            ):
                status_code = 200
                return Response(status_code=200, content=b"")
            store.put(
                epoch_id=epoch_id,
                miner_identity=miner_identity,
                sha256=expected_digest,
                data=body,
            )
            metrics["store_objects"].labels(kind="put_new").inc()
            status_code = 201
            return Response(status_code=201, content=b"")
        finally:
            elapsed = time.monotonic() - start
            metrics["requests_total"].labels(
                method="POST", outcome=outcome,
                status_group=status_group(status_code),
            ).inc()
            metrics["request_seconds"].labels(method="POST", outcome=outcome).observe(elapsed)
            if body_len:
                metrics["request_bytes"].labels(method="POST").observe(body_len)

    @app.get("/archive/{epoch_id}/{miner_identity}/{sha256_hex}")
    async def fetch(epoch_id: str, miner_identity: str, sha256_hex: str):
        start = time.monotonic()
        outcome = "ok"
        status_code = 200
        try:
            if "/" in epoch_id or ".." in epoch_id:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="invalid epoch_id")
            if "/" in miner_identity or ".." in miner_identity:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="invalid miner_identity")
            if len(sha256_hex) != 64:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="sha256_hex must be 64 hex chars")
            try:
                expected_digest = bytes.fromhex(sha256_hex.lower())
            except ValueError as e:
                outcome = "other"
                status_code = 400
                raise HTTPException(status_code=400, detail="sha256_hex is not valid hex") from e

            body = store.get(
                epoch_id=epoch_id,
                miner_identity=miner_identity,
                sha256=expected_digest,
            )
            if body is None:
                outcome = "not_found"
                status_code = 404
                raise HTTPException(status_code=404, detail="not found")
            return Response(content=body, media_type="application/octet-stream")
        finally:
            elapsed = time.monotonic() - start
            metrics["requests_total"].labels(
                method="GET", outcome=outcome,
                status_group=status_group(status_code),
            ).inc()
            metrics["request_seconds"].labels(method="GET", outcome=outcome).observe(elapsed)

    return app
