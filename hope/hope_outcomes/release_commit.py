"""Layer 9.A.1 — the operator release_commit at T=0.

At each epoch boundary T=0, the outcome signer publishes a
`release_commit_digest` on chain (Sha256 variant). The digest is the
BLAKE2b-256 of the canonical-CBOR encoding of the release_commit map.

Purpose: pin the rules of the epoch BEFORE miners see anything that could
be predicted away. After T=0, the digest is immutable; the operator cannot retro-
actively change which episodes are in scope, what their queries are, or
which horizons get measured.

CBOR layout (canonical, RFC 8949 §4.2.1):

    {
      "v": 1,                                  # protocol version
      "epoch_id": tstr,                        # release_key, [A-Z0-9-]{1,80}
      "epoch_idx": uint,                       # monotonic per outcome_signer
      "release_round": uint,                   # drand round at T=0
      "deadline_round": uint,                  # drand round at miner deadline
      "horizons": [tstr],                      # e.g. ["7", "14"]
      "episodes_root": bstr(32),               # IMT root over (episode_id → query_canonical_hash)
      "scoring_metadata_hash": bstr(32),       # SHA-256 of canonical scoring config
      "outcome_signer_hotkey": bstr(32),       # raw ed25519 pubkey (Substrate writer)
      "inner_sig": bstr(64),                   # ed25519 over digest WITHOUT this field
    }

The `episodes_root` IMT lets miners (and the verifier) prove an episode IS
or IS NOT in scope: leaf key = episode_id_bytes (left-padded to 32),
leaf value = blake2b_256(canonical-CBOR of the episode query). After
release, the operator serves the full episode list off-chain but ANY divergence
between served data and `episodes_root` is provably a the operator fault.

`scoring_metadata_hash` covers the goal_metric, measurement_resolution,
reliability_weight, baseline_type, etc. — anything that affects how an
outcome is scored. Locking it at T=0 prevents the operator from retroactively
relaxing scoring rules to favor specific miners.

The plaintext is committed off-chain via the operator's HTTPS endpoints AND its
SHA-256 / IMT roots are anchored on chain. A third party can fetch the
plaintext, re-hash, and verify equality.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps
from hope.commitment.imt import imt_root, make_leaves_with_pointers
from hope.commitment.inner_sig import add_inner_sig


RELEASE_COMMIT_VERSION = 1


@dataclass(frozen=True)
class EpisodeRef:
    """One episode in scope for an epoch.

    `episode_id` is the human-readable ID; `episode_id_bytes` is its 32-byte
    left-padded SHA-256 used as the IMT key (so episodes can have arbitrary
    string IDs without changing the tree shape).

    `query_cbor` is the canonical-CBOR encoding of the full episode query
    (ad account, campaign, goal, geo, time window, etc.) — anything a miner
    needs to predict outcomes. The IMT leaf value commits its hash so any
    post-release tampering is detectable.
    """

    episode_id: str
    query_cbor: bytes  # canonical CBOR of the query payload


def _episode_id_to_imt_key(episode_id: str) -> bytes:
    return hashlib.sha256(episode_id.encode("utf-8")).digest()


def compute_episodes_root(episodes: list[EpisodeRef]) -> bytes:
    """Compute the IMT root over per-episode (id_hash → query_hash) pairs."""
    pairs: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for ep in episodes:
        key = _episode_id_to_imt_key(ep.episode_id)
        if key in seen:
            raise ValueError(f"duplicate episode_id: {ep.episode_id!r}")
        seen.add(key)
        value = hashlib.blake2b(ep.query_cbor, digest_size=32).digest()
        pairs.append((key, value))
    if not pairs:
        return imt_root([])
    return imt_root(make_leaves_with_pointers(pairs))


def build_release_commit_plaintext(
    *,
    outcome_signer_hotkey: bytes,
    outcome_signer_signing_key: Ed25519PrivateKey,
    epoch_id: str,
    epoch_idx: int,
    release_round: int,
    deadline_round: int,
    horizons: list[str],
    episodes: list[EpisodeRef],
    scoring_metadata_hash: bytes,
) -> bytes:
    """Build the canonical-CBOR release_commit plaintext (off-chain blob).

    The on-chain commit value is `compute_release_commit_digest(plaintext)`.

    Returns:
        Canonical CBOR bytes of the plaintext (the full off-chain blob).
    """
    if len(outcome_signer_hotkey) != 32:
        raise ValueError(
            f"outcome_signer_hotkey must be 32 bytes, got {len(outcome_signer_hotkey)}"
        )
    if len(scoring_metadata_hash) != 32:
        raise ValueError(
            f"scoring_metadata_hash must be 32 bytes, got {len(scoring_metadata_hash)}"
        )
    if not horizons:
        raise ValueError("horizons must be non-empty")
    if release_round <= 0 or deadline_round <= release_round:
        raise ValueError(
            f"deadline_round ({deadline_round}) must exceed release_round ({release_round})"
        )

    plaintext: dict[str, Any] = {
        "v": RELEASE_COMMIT_VERSION,
        "epoch_id": epoch_id,
        "epoch_idx": int(epoch_idx),
        "release_round": int(release_round),
        "deadline_round": int(deadline_round),
        "horizons": list(horizons),
        "episodes_root": compute_episodes_root(episodes),
        "scoring_metadata_hash": scoring_metadata_hash,
        "outcome_signer_hotkey": outcome_signer_hotkey,
    }
    add_inner_sig(
        plaintext,
        outcome_signer_signing_key,
        hotkey_field="outcome_signer_hotkey",
    )
    return canonical_cbor_dumps(plaintext)


def compute_release_commit_digest(release_commit_plaintext: bytes) -> bytes:
    """BLAKE2b-256 of the off-chain release_commit plaintext.

    This is the value submitted on chain via `submit_release_commit_layer_9a1`.
    """
    return hashlib.blake2b(release_commit_plaintext, digest_size=32).digest()
