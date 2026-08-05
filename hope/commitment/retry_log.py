"""Layer 9.C.6 — retry log blob builder.

When the validator excludes a miner with `excluded_reason == 'plaintext_unavailable'`
(the archive-fetch path failed at every tier), it must publish a JSON
retry-log blob describing the attempts so a third party can reproduce the
fetch decision. The blob's SHA-256 is committed on chain via
`submit_retry_log_attestation_layer_9c6`.

This is a JSON blob (not CBOR) because:
  - It's served via HTTPS to humans + tooling, not chain-side parsed.
  - Fields are sparse strings (URLs, status codes, error messages); JSON's
    text-friendly encoding is more debuggable.
  - The on-chain anchor is a SHA-256, so the encoding format only affects
    the hash, not the chain interaction.

JSON layout (one object per validator-epoch):

    {
      "v": 1,
      "validator_hotkey": "<hex 32B>",
      "epoch_id": "...",
      "epoch_idx": 42,
      "generated_at_unix": 1714694400,
      "miners": [
        {
          "miner_hotkey": "<hex 32B>",
          "miner_uid": 17,
          "expected_sha256_ct": "<hex 32B>",
          "attempts": [
            {
              "tier": 1, "name": "validator-7",
              "url": "https://...", "ok": false,
              "status_code": 502, "sha256_match": null,
              "elapsed_ms": 1280,
              "error": "http 502: bad gateway"
            },
            ...
          ]
        }
      ]
    }

The validator must include EVERY miner it excluded for plaintext_unavailable.
Other exclusions (scoreability failures) are listed in 9.C.1's
`excluded_miners_hash` and don't need a retry log entry.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from hope.commitment.archives import FetchResult

RETRY_LOG_VERSION = 1


@dataclass(frozen=True)
class RetryLogAttempt:
    """One archive-fetch attempt for one miner."""

    tier: int
    name: str
    url: str
    ok: bool
    status_code: int | None
    sha256_match: bool | None
    elapsed_ms: int
    error: str | None


@dataclass(frozen=True)
class RetryLogMinerEntry:
    """All archive attempts for one miner that ended in plaintext_unavailable."""

    miner_hotkey: bytes  # 32 bytes raw
    miner_uid: int
    expected_sha256_ct: bytes  # 32 bytes
    attempts: list[RetryLogAttempt]


def attempt_from_fetch_result(result: FetchResult) -> RetryLogAttempt:
    """Convert an archive-client FetchResult into a retry-log attempt."""
    ep = result.endpoint
    return RetryLogAttempt(
        tier=ep.tier,
        name=ep.name or ep.base_url,
        url=ep.base_url,
        ok=result.ok,
        status_code=result.status_code,
        sha256_match=result.sha256_match,
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )


def build_retry_log_blob(
    *,
    validator_hotkey: bytes,
    epoch_id: str,
    epoch_idx: int,
    miner_entries: list[RetryLogMinerEntry],
    generated_at_unix: int | None = None,
) -> bytes:
    """Build the canonical JSON-encoded retry-log blob.

    Returns UTF-8 encoded JSON bytes whose SHA-256 is the chain commit value.
    Encoded with sort_keys=True and no whitespace separators so that re-runs
    by the verifier produce byte-identical bytes.
    """
    if len(validator_hotkey) != 32:
        raise ValueError(
            f"validator_hotkey must be 32 bytes, got {len(validator_hotkey)}"
        )

    if generated_at_unix is None:
        generated_at_unix = int(time.time())

    miners_payload = []
    for m in miner_entries:
        if len(m.miner_hotkey) != 32:
            raise ValueError(
                f"miner_hotkey must be 32 bytes, got {len(m.miner_hotkey)}"
            )
        if len(m.expected_sha256_ct) != 32:
            raise ValueError(
                f"expected_sha256_ct must be 32 bytes, got {len(m.expected_sha256_ct)}"
            )
        miners_payload.append({
            "miner_hotkey": m.miner_hotkey.hex(),
            "miner_uid": int(m.miner_uid),
            "expected_sha256_ct": m.expected_sha256_ct.hex(),
            "attempts": [
                {
                    "tier": int(a.tier),
                    "name": a.name,
                    "url": a.url,
                    "ok": bool(a.ok),
                    "status_code": a.status_code,
                    "sha256_match": a.sha256_match,
                    "elapsed_ms": int(a.elapsed_ms),
                    "error": a.error,
                }
                for a in m.attempts
            ],
        })

    miners_payload.sort(key=lambda x: x["miner_hotkey"])

    blob = {
        "v": RETRY_LOG_VERSION,
        "validator_hotkey": validator_hotkey.hex(),
        "epoch_id": epoch_id,
        "epoch_idx": int(epoch_idx),
        "generated_at_unix": int(generated_at_unix),
        "miners": miners_payload,
    }
    return json.dumps(blob, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_retry_log_sha256(blob: bytes) -> bytes:
    """SHA-256 of the retry-log blob — chain commit value for 9.C.6."""
    return hashlib.sha256(blob).digest()
