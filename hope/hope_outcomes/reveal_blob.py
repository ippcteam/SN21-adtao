"""Layer 9.A.2 — outcome reveal blob (post-deadline).

After the miner deadline passes, the operator measures actual outcomes and publishes
a JSON reveal blob containing:

  - `release_commit_plaintext_sha256`: anchors back to the 9.A.1 commit.
  - `episode_outcomes`: per-episode measured deltas across each horizon.
  - `salts`: per-episode random salts (used to derandomize any predicted-
    away patterns; not relevant for SN21 v1 but reserved).

The blob's SHA-256 is committed on chain via
`submit_outcome_reveal_hash_layer_9a2`. ONLY AFTER that commit is finalized
does the operator serve the blob via HTTPS — the commit-then-serve gate (CL-9 in
v0.7) prevents the operator from selectively serving different blobs to different
validators.

A third party verifies as follows:
  1. Read 9.A.1 release_commit_digest from chain.
  2. Read 9.A.2 reveal_blob_sha256 from chain (must be a later block).
  3. Fetch reveal blob via the operator HTTPS.
  4. SHA-256 the bytes; compare to chain-anchored hash.
  5. BLAKE2b-256 the embedded release_commit_plaintext; compare to 9.A.1 chain hash.
  6. Re-sign + verify outcome_signer's inner_sig against
     reveal_blob.outcome_signer_hotkey.

Layout:

    {
      "v": 1,
      "epoch_id": "EPOCH-…",
      "epoch_idx": 42,
      "release_commit_plaintext_sha256_hex": "<64 hex>",
      "outcome_signer_hotkey_hex": "<64 hex>",
      "deadline_round": 12345700,
      "measured_at_round": 12346000,
      "horizons": ["7", "14"],
      "episodes": [
        {
          "episode_id": "EP-001",
          "salt_hex": "<32 hex>",
          "outcomes": {
            "7": {
              "cost_delta_pct": -3.7,
              "conversions_delta_pct": 12.5,
              "efficiency_delta_pct": 8.2,
              "goal_miss": 0
            },
            "14": { ... }
          }
        }
      ],
      "inner_sig_hex": "<128 hex, ed25519 over canonical-CBOR of payload sans inner_sig_hex>"
    }

JSON (not CBOR) because the blob is human-readable + served via HTTPS to
auditors. The on-chain anchor is its SHA-256, so encoding format is
irrelevant to chain interaction. Inner sig is computed over CANONICAL CBOR
of the equivalent dict (sans inner_sig_hex) to keep digest construction
cross-format-stable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from hope.commitment.canonical import canonical_cbor_dumps

REVEAL_BLOB_VERSION = 1


@dataclass(frozen=True)
class HorizonOutcomeMeasured:
    """One horizon's measured outcome for one episode."""

    horizon: str  # e.g. "7" or "14"
    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: float
    goal_miss: int  # 0 or 1


@dataclass(frozen=True)
class EpisodeOutcome:
    episode_id: str
    salt: bytes  # 16 or 32 random bytes per episode
    outcomes: list[HorizonOutcomeMeasured]


def build_reveal_blob(
    *,
    outcome_signer_hotkey: bytes,
    outcome_signer_signing_key: Ed25519PrivateKey,
    epoch_id: str,
    epoch_idx: int,
    release_commit_plaintext_sha256: bytes,
    deadline_round: int,
    measured_at_round: int,
    horizons: list[str],
    episodes: list[EpisodeOutcome],
) -> bytes:
    """Build the canonical JSON reveal blob with embedded inner_sig.

    Returns:
        UTF-8 encoded JSON bytes whose SHA-256 is the chain commit value.
    """
    if len(outcome_signer_hotkey) != 32:
        raise ValueError(
            f"outcome_signer_hotkey must be 32 bytes, got {len(outcome_signer_hotkey)}"
        )
    if len(release_commit_plaintext_sha256) != 32:
        raise ValueError(
            f"release_commit_plaintext_sha256 must be 32 bytes, "
            f"got {len(release_commit_plaintext_sha256)}"
        )
    if measured_at_round < deadline_round:
        raise ValueError(
            f"measured_at_round ({measured_at_round}) must be >= "
            f"deadline_round ({deadline_round})"
        )

    expected_pub = outcome_signer_signing_key.public_key().public_bytes_raw()
    if expected_pub != outcome_signer_hotkey:
        raise ValueError(
            "outcome_signer_hotkey does not match the signing private key"
        )

    seen: set[str] = set()
    episodes_payload = []
    for ep in episodes:
        if ep.episode_id in seen:
            raise ValueError(f"duplicate episode_id in reveal blob: {ep.episode_id!r}")
        seen.add(ep.episode_id)
        if len(ep.salt) not in (16, 32):
            raise ValueError(
                f"episode {ep.episode_id!r}: salt must be 16 or 32 bytes, "
                f"got {len(ep.salt)}"
            )
        outcomes_map: dict[str, dict[str, Any]] = {}
        for o in ep.outcomes:
            if o.horizon not in horizons:
                raise ValueError(
                    f"episode {ep.episode_id!r}: horizon {o.horizon!r} not in {horizons}"
                )
            if o.goal_miss not in (0, 1):
                raise ValueError(
                    f"episode {ep.episode_id!r}: goal_miss must be 0 or 1, got {o.goal_miss}"
                )
            outcomes_map[o.horizon] = {
                "cost_delta_pct": float(o.cost_delta_pct),
                "conversions_delta_pct": float(o.conversions_delta_pct),
                "efficiency_delta_pct": float(o.efficiency_delta_pct),
                "goal_miss": int(o.goal_miss),
            }
        episodes_payload.append({
            "episode_id": ep.episode_id,
            "salt_hex": ep.salt.hex(),
            "outcomes": outcomes_map,
        })

    # Sort by episode_id so encoding is deterministic across signer iteration.
    episodes_payload.sort(key=lambda x: x["episode_id"])

    payload_no_sig: dict[str, Any] = {
        "v": REVEAL_BLOB_VERSION,
        "epoch_id": epoch_id,
        "epoch_idx": int(epoch_idx),
        "release_commit_plaintext_sha256_hex": release_commit_plaintext_sha256.hex(),
        "outcome_signer_hotkey_hex": outcome_signer_hotkey.hex(),
        "deadline_round": int(deadline_round),
        "measured_at_round": int(measured_at_round),
        "horizons": list(horizons),
        "episodes": episodes_payload,
    }

    digest = hashlib.blake2b(
        canonical_cbor_dumps(payload_no_sig), digest_size=32
    ).digest()
    inner_sig = outcome_signer_signing_key.sign(digest)

    payload_with_sig = dict(payload_no_sig)
    payload_with_sig["inner_sig_hex"] = inner_sig.hex()
    return json.dumps(payload_with_sig, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_reveal_blob(blob: bytes) -> bool:
    """Verify a reveal blob's inner_sig against its declared outcome_signer_hotkey.

    Returns True iff the embedded inner_sig verifies. Caller must additionally
    check that `outcome_signer_hotkey_hex` matches the chain storage account
    that wrote the 9.A.1 release_commit (Substrate already ties storage to
    hotkey, so this is a chain-side check).
    """
    try:
        decoded = json.loads(blob.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(decoded, dict):
        return False
    if "inner_sig_hex" not in decoded:
        return False
    sig_hex = decoded.pop("inner_sig_hex")
    try:
        sig = bytes.fromhex(sig_hex)
        signer_hex = decoded["outcome_signer_hotkey_hex"]
        signer_pk = bytes.fromhex(signer_hex)
    except (ValueError, KeyError):
        return False
    if len(sig) != 64 or len(signer_pk) != 32:
        return False
    digest = hashlib.blake2b(
        canonical_cbor_dumps(decoded), digest_size=32
    ).digest()
    try:
        Ed25519PublicKey.from_public_bytes(signer_pk).verify(sig, digest)
        return True
    except Exception:
        return False


def compute_reveal_blob_sha256(blob: bytes) -> bytes:
    """SHA-256 of the reveal blob — chain commit value for 9.A.2."""
    return hashlib.sha256(blob).digest()
