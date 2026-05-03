"""Three-tier archive client for off-chain AES_ct storage and retrieval.

Layer 9.B encrypts the miner prediction with a fresh K, commits K on chain,
and publishes AES_ct off chain to **three independent archive tiers**:

  Tier-1 — Validator archive
      Operator: each validator submitting weights for that epoch.
      Lifetime: at least until 9.C.2 reveal + verifier window (≥ 7 days).
      Authentication: open POST during submission window; signed with miner's
                      hotkey signature (via auth header) to bind upload to UID.
      Retrieval: open GET via path `/archive/{epoch_id}/{miner_uid}`.

  Tier-2 — HOPE shadow archive
      Operator: HOPE Foundation; durable, geographically replicated.
      Lifetime: 90 days minimum (Tier-1 may roll faster).
      Retrieval: open GET via path `/archive/{epoch_id}/{miner_hotkey_ss58}`.

  Tier-3 — Miner self-archive
      Operator: each miner; URL announced in the on-chain Raw commit field.
      Lifetime: best-effort; not required for scoreability if Tier-2 succeeds.
      Retrieval: open GET via the URL as published.

A miner upload is considered durable iff at least Tier-2 confirms storage; the
other tiers raise the bar for malicious-validator detection (Tier-1 lying →
Tier-2/Tier-3 contradicts) but don't block scoring on their own.

A validator fetch is scoreable iff ANY tier returns AES_ct whose SHA-256
matches the on-chain Sha256 commit. Mismatched bytes from any tier are logged
to the retry log (Layer 9.C.6) but do not poison the score — the on-chain
hash is the source of truth.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECS = 30.0
DEFAULT_MAX_AES_CT_BYTES = 1 * 1024 * 1024  # 1 MiB hard ceiling per AES_ct


@dataclass(frozen=True)
class ArchiveEndpoint:
    """One archive endpoint and its tier identity.

    `tier` is 1 / 2 / 3 per the protocol spec; used in retry-log labels and
    archive-precedence ordering. `base_url` is e.g. ``https://archive.hope.example``
    (no trailing slash). Path segments are added by the upload/download helpers.
    """

    tier: int
    base_url: str
    name: str = ""  # human-readable label, e.g. "validator-7" or "hope-shadow"

    def __post_init__(self) -> None:
        if self.tier not in (1, 2, 3):
            raise ValueError(f"tier must be 1, 2, or 3; got {self.tier}")
        if not self.base_url or self.base_url.endswith("/"):
            raise ValueError(
                "base_url must be non-empty and not end with '/'; "
                f"got {self.base_url!r}"
            )


@dataclass
class UploadResult:
    """Per-archive outcome for a single AES_ct upload attempt."""

    endpoint: ArchiveEndpoint
    ok: bool
    status_code: Optional[int]
    error: Optional[str] = None
    elapsed_ms: int = 0


@dataclass
class FetchResult:
    """Per-archive outcome for a single AES_ct download attempt."""

    endpoint: ArchiveEndpoint
    ok: bool
    aes_ct: Optional[bytes]
    sha256_match: Optional[bool]
    status_code: Optional[int]
    error: Optional[str] = None
    elapsed_ms: int = 0


@dataclass
class FetchAggregate:
    """Outcome of fetching across all configured tiers.

    `winner` is the FetchResult that produced the bytes whose SHA-256 matched
    the expected digest, or None if every tier failed. `attempts` records
    every tier in the order they were tried, for the retry log.
    """

    aes_ct: Optional[bytes]
    winner: Optional[FetchResult]
    attempts: list[FetchResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.aes_ct is not None and self.winner is not None


class ArchiveClient:
    """HTTP client for uploading to and fetching from archive tiers.

    The client is stateless except for httpx connection reuse. One instance can
    be shared across a process. All methods are sync; the validator/miner
    runners can wrap calls in a thread executor when needed (the existing
    miner code uses asyncio elsewhere, but archive I/O is rare and short).
    """

    def __init__(
        self,
        timeout_secs: float = DEFAULT_TIMEOUT_SECS,
        max_aes_ct_bytes: int = DEFAULT_MAX_AES_CT_BYTES,
    ) -> None:
        self.timeout_secs = timeout_secs
        self.max_aes_ct_bytes = max_aes_ct_bytes

    def _archive_path(
        self, epoch_id: str, miner_identity: str, sha256_hex: str
    ) -> str:
        """Path under base_url for a given miner's ciphertext at one epoch.

        miner_identity is either the SS58 hotkey (Tier-2 / Tier-3) or the
        miner UID as a string (Tier-1). sha256_hex pins the specific upload —
        same miner re-uploading after on-chain Sha256 commit would race with
        itself; archives store keyed by full path including digest.
        """
        return f"/archive/{epoch_id}/{miner_identity}/{sha256_hex}"

    def upload(
        self,
        endpoint: ArchiveEndpoint,
        *,
        epoch_id: str,
        miner_identity: str,
        aes_ct: bytes,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        """POST AES_ct to one archive endpoint.

        The path includes the SHA-256 hex of aes_ct, so archives can refuse
        bytes whose hash diverges from the path component (defence in depth —
        the on-chain Sha256 commit is still the authoritative bind).

        auth_headers is forwarded as-is; callers typically include a miner
        hotkey signature so the archive can attribute the upload to a UID.
        """
        if len(aes_ct) > self.max_aes_ct_bytes:
            return UploadResult(
                endpoint=endpoint,
                ok=False,
                status_code=None,
                error=f"aes_ct too large: {len(aes_ct)} > {self.max_aes_ct_bytes}",
            )

        digest = hashlib.sha256(aes_ct).hexdigest()
        path = self._archive_path(epoch_id, miner_identity, digest)
        url = f"{endpoint.base_url}{path}"
        headers = {"Content-Type": "application/octet-stream"}
        if auth_headers:
            headers.update(auth_headers)

        import time

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_secs) as client:
                resp = client.post(url, content=aes_ct, headers=headers)
        except httpx.HTTPError as e:
            return UploadResult(
                endpoint=endpoint,
                ok=False,
                status_code=None,
                error=f"http error: {type(e).__name__}: {e}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        ok = 200 <= resp.status_code < 300
        return UploadResult(
            endpoint=endpoint,
            ok=ok,
            status_code=resp.status_code,
            error=None if ok else f"http {resp.status_code}: {resp.text[:200]}",
            elapsed_ms=elapsed_ms,
        )

    def upload_to_all(
        self,
        endpoints: list[ArchiveEndpoint],
        *,
        epoch_id: str,
        miner_identity: str,
        aes_ct: bytes,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> list[UploadResult]:
        """Best-effort upload to every endpoint; returns per-tier results.

        Caller decides what counts as success (Tier-2 mandatory, others nice
        to have, etc.) based on the per-tier ok flags.
        """
        results: list[UploadResult] = []
        for ep in endpoints:
            res = self.upload(
                ep,
                epoch_id=epoch_id,
                miner_identity=miner_identity,
                aes_ct=aes_ct,
                auth_headers=auth_headers,
            )
            results.append(res)
            logger.info(
                "archive upload tier=%d name=%s ok=%s status=%s elapsed_ms=%d",
                ep.tier, ep.name or ep.base_url, res.ok, res.status_code, res.elapsed_ms,
            )
        return results

    def fetch(
        self,
        endpoint: ArchiveEndpoint,
        *,
        epoch_id: str,
        miner_identity: str,
        expected_sha256: bytes,
    ) -> FetchResult:
        """GET AES_ct from one endpoint and verify the SHA-256 digest.

        Returns the bytes only if the actual digest matches expected_sha256
        exactly. A length cap (max_aes_ct_bytes) prevents byte-flood DoS by
        a malicious archive — a real AES_ct is bounded by the chosen schema
        (a few hundred bytes for a typical prediction).
        """
        if len(expected_sha256) != 32:
            raise ValueError(
                f"expected_sha256 must be 32 bytes, got {len(expected_sha256)}"
            )

        digest_hex = expected_sha256.hex()
        path = self._archive_path(epoch_id, miner_identity, digest_hex)
        url = f"{endpoint.base_url}{path}"

        import time

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_secs) as client:
                resp = client.get(url)
        except httpx.HTTPError as e:
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                aes_ct=None,
                sha256_match=None,
                status_code=None,
                error=f"http error: {type(e).__name__}: {e}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                aes_ct=None,
                sha256_match=None,
                status_code=resp.status_code,
                error=f"http {resp.status_code}",
                elapsed_ms=elapsed_ms,
            )

        body = resp.content
        if len(body) > self.max_aes_ct_bytes:
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                aes_ct=None,
                sha256_match=None,
                status_code=resp.status_code,
                error=f"body too large: {len(body)} > {self.max_aes_ct_bytes}",
                elapsed_ms=elapsed_ms,
            )

        actual = hashlib.sha256(body).digest()
        match = actual == expected_sha256
        return FetchResult(
            endpoint=endpoint,
            ok=match,
            aes_ct=body if match else None,
            sha256_match=match,
            status_code=resp.status_code,
            error=None if match else "sha256 mismatch",
            elapsed_ms=elapsed_ms,
        )

    def fetch_first_match(
        self,
        endpoints: list[ArchiveEndpoint],
        *,
        epoch_id: str,
        miner_identity: str,
        expected_sha256: bytes,
    ) -> FetchAggregate:
        """Try endpoints in order; return on the first byte-perfect match.

        Tiers should be ordered by preference (Tier-1 fastest, Tier-2 most
        durable, Tier-3 fallback). Every attempt is recorded so the validator
        can publish a retry log when no tier matches.
        """
        attempts: list[FetchResult] = []
        for ep in endpoints:
            res = self.fetch(
                ep,
                epoch_id=epoch_id,
                miner_identity=miner_identity,
                expected_sha256=expected_sha256,
            )
            attempts.append(res)
            logger.info(
                "archive fetch tier=%d name=%s ok=%s match=%s status=%s elapsed_ms=%d",
                ep.tier, ep.name or ep.base_url, res.ok, res.sha256_match,
                res.status_code, res.elapsed_ms,
            )
            if res.ok and res.aes_ct is not None:
                return FetchAggregate(
                    aes_ct=res.aes_ct, winner=res, attempts=attempts
                )
        return FetchAggregate(aes_ct=None, winner=None, attempts=attempts)
