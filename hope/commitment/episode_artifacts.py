"""Per-episode prediction artifacts (Phase E, Layer 9.B.1).

Phase B/C committed only the aggregated horizon CBOR (one entry per horizon
across the whole portfolio). That kept the chain plaintext under the 768-byte
TLE budget but lost per-episode granularity — the scorer could only judge
miners on aggregated quantiles, not on which specific episodes they got
right or wrong.

Phase E adds an OFF-CHAIN per-episode bundle whose IMT root is committed
on-chain inside the existing aggregated plaintext. The chain still carries
≤ 768 bytes; the bundle goes to the same Tier-2 / Tier-3 archives, indexed
by epoch_id + miner_hotkey. After K reveals, anyone can fetch the bundle,
verify its root against the on-chain commit, decode per-episode quantiles,
and run the project's `EpochScorer` on it.

Schema (off-chain bundle, canonical CBOR):

    {
      "v": 1,
      "epoch_id": tstr,
      "miner_hotkey": bstr(32),
      "submitted_round": uint,
      "entries": [
        {
          "ep": tstr,                       # episode_id
          "h":  "7" | "14",
          "cost_q":  [int, int, int],       # P10/P50/P90, deci-percent
          "conv_q":  [int, int, int],
          "eff_q":   [int, int, int],
          "miss_p":  uint,                  # ppm
          "instab_p": uint,
        },
        ...
      ],
      "inner_sig": bstr(64),
    }

The IMT root over (key=blake2b_256(ep_id||horizon), value=blake2b_256(entry_cbor))
is the chain-anchored binding. A verifier fetching the bundle:

  1. Decodes CBOR.
  2. Verifies inner_sig against the miner's chain-anchored hotkey.
  3. Recomputes the IMT root and compares against `episodes_root` field
     in the aggregated chain plaintext.
  4. Optionally proves inclusion of any single (episode, horizon) entry.

Total chain footprint stays the same (~352B AES_ct + 32B sha + 32B K +
URL); the bundle is just additional bytes uploaded to archives. A typical
N=20 episode × 2 horizon bundle is ~3-4 KB CBOR — well within archive
body caps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.imt import (
    InclusionProof,
    NonInclusionProof,
    imt_proof_inclusion,
    imt_proof_non_inclusion,
    imt_root,
    make_leaves_with_pointers,
    verify_inclusion,
    verify_non_inclusion,
)
from hope.commitment.inner_sig import add_inner_sig, verify_inner_sig
from hope.commitment.prediction_payload import (
    ALLOWED_HORIZONS,
    PROBABILITY_SCALE,
    QUANTILE_SCALE,
    _is_valid_epoch_id,
    _scale_probability,
    _scale_quantile,
    _validate_quantile_triple,
)


EPISODE_BUNDLE_VERSION = 1


@dataclass(frozen=True)
class PerEpisodeEntry:
    """One (episode_id × horizon) prediction in the off-chain bundle."""

    episode_id: str
    horizon: str
    cost_q: tuple[float, float, float]
    conv_q: tuple[float, float, float]
    eff_q: tuple[float, float, float]
    goal_miss_probability: float
    instability_risk: float


def build_per_episode_entry(entry: PerEpisodeEntry) -> dict[str, Any]:
    """Validate + encode one per-episode entry into canonical CBOR-safe types."""
    if entry.horizon not in ALLOWED_HORIZONS:
        raise ValueError(
            f"horizon must be one of {ALLOWED_HORIZONS}, got {entry.horizon!r}"
        )
    if not entry.episode_id or "/" in entry.episode_id:
        raise ValueError(f"invalid episode_id: {entry.episode_id!r}")
    return {
        "ep": entry.episode_id,
        "h": entry.horizon,
        "cost_q": _validate_quantile_triple(entry.cost_q, "cost_q"),
        "conv_q": _validate_quantile_triple(entry.conv_q, "conv_q"),
        "eff_q":  _validate_quantile_triple(entry.eff_q,  "eff_q"),
        "miss_p":  _scale_probability(entry.goal_miss_probability),
        "instab_p": _scale_probability(entry.instability_risk),
    }


def _entry_imt_key(episode_id: str, horizon: str) -> bytes:
    """IMT key = blake2b_256(episode_id || ':' || horizon).

    Hashing keeps keys 32 bytes regardless of episode_id length and avoids
    sort collisions between distinct (ep, h) pairs.
    """
    return hashlib.blake2b(
        f"{episode_id}:{horizon}".encode("utf-8"), digest_size=32
    ).digest()


def _entry_imt_value(entry_dict: dict[str, Any]) -> bytes:
    """IMT value = blake2b_256(canonical_cbor(entry_dict))."""
    return hashlib.blake2b(canonical_cbor_dumps(entry_dict), digest_size=32).digest()


def compute_episodes_imt_root(entries: list[dict[str, Any]]) -> bytes:
    """IMT root over the per-episode entry list.

    Stable under reordering — sorts by IMT key internally. Empty list → the
    standard empty-IMT root from `imt_root([])`.
    """
    if not entries:
        return imt_root([])

    pairs: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for e in entries:
        key = _entry_imt_key(e["ep"], e["h"])
        if key in seen:
            raise ValueError(
                f"duplicate (episode, horizon) pair: ({e['ep']!r}, {e['h']!r})"
            )
        seen.add(key)
        pairs.append((key, _entry_imt_value(e)))
    leaves = make_leaves_with_pointers(pairs)
    return imt_root(leaves)


def build_per_episode_bundle(
    *,
    epoch_id: str,
    miner_hotkey: bytes,
    submitted_round: int,
    entries: list[PerEpisodeEntry],
    miner_signing_key: Ed25519PrivateKey,
    version: int = EPISODE_BUNDLE_VERSION,
) -> bytes:
    """Build the off-chain per-episode CBOR bundle with inner_sig.

    Returns:
        Canonical CBOR bytes ready to upload to archive endpoints. The
        bundle's IMT root (over `entries`) is what the validator commits
        on-chain inside the aggregated plaintext.
    """
    if not _is_valid_epoch_id(epoch_id):
        raise ValueError(f"invalid epoch_id: {epoch_id!r}")
    if len(miner_hotkey) != 32:
        raise ValueError(f"miner_hotkey must be 32 bytes, got {len(miner_hotkey)}")
    if submitted_round <= 0:
        raise ValueError(f"submitted_round must be positive, got {submitted_round}")
    if not entries:
        raise ValueError("entries must be non-empty")

    encoded_entries = [build_per_episode_entry(e) for e in entries]
    plaintext: dict[str, Any] = {
        "v": int(version),
        "epoch_id": epoch_id,
        "miner_hotkey": miner_hotkey,
        "submitted_round": int(submitted_round),
        "entries": encoded_entries,
    }
    add_inner_sig(plaintext, miner_signing_key, hotkey_field="miner_hotkey")
    return canonical_cbor_dumps(plaintext)


def parse_per_episode_bundle(bundle_bytes: bytes) -> dict[str, Any]:
    """Decode a per-episode bundle CBOR; raise on malformed input."""
    obj = canonical_cbor_loads(bundle_bytes)
    if not isinstance(obj, dict):
        raise ValueError(f"bundle is not a CBOR map: {type(obj).__name__}")
    if "inner_sig" not in obj:
        raise ValueError("bundle missing inner_sig")
    if "entries" not in obj or not isinstance(obj["entries"], list):
        raise ValueError("bundle missing entries list")
    return obj


def verify_bundle_inner_sig(
    bundle: dict[str, Any],
    expected_miner_hotkey: bytes,
) -> bool:
    """Verify the bundle's miner inner_sig (delegates to inner_sig module)."""
    return verify_inner_sig(
        bundle, expected_miner_hotkey, hotkey_field="miner_hotkey"
    )


def verify_bundle_root(
    bundle: dict[str, Any],
    *,
    expected_root: bytes,
) -> bool:
    """Recompute the IMT root over the bundle's entries and compare.

    `expected_root` comes from the on-chain aggregated plaintext's
    `episodes_root` field (Phase E adds it). A non-match means the archive
    served a tampered bundle, the on-chain commit was tampered, or the
    miner failed to upload the canonical bundle.
    """
    if len(expected_root) != 32:
        return False
    try:
        return compute_episodes_imt_root(bundle["entries"]) == expected_root
    except Exception:
        return False


def prove_episode_inclusion(
    bundle: dict[str, Any],
    *,
    episode_id: str,
    horizon: str,
) -> InclusionProof:
    """Build an IMT inclusion proof for one (episode_id, horizon) entry.

    The proof can be served separately from the full bundle so a verifier
    can attest a single miner's prediction for one episode without needing
    the whole bundle.
    """
    target_key = _entry_imt_key(episode_id, horizon)
    pairs: list[tuple[bytes, bytes]] = []
    for e in bundle["entries"]:
        pairs.append((_entry_imt_key(e["ep"], e["h"]), _entry_imt_value(e)))
    leaves = make_leaves_with_pointers(pairs)
    return imt_proof_inclusion(leaves, target_key)


def prove_episode_non_inclusion(
    bundle: dict[str, Any],
    *,
    episode_id: str,
    horizon: str,
) -> NonInclusionProof:
    """Build an IMT non-inclusion proof for an entry that's NOT in the bundle.

    Use case: validator claims miner failed to predict episode X in horizon
    "14". Validator can produce a non-inclusion proof against the chain-
    anchored episodes_root.
    """
    target_key = _entry_imt_key(episode_id, horizon)
    pairs: list[tuple[bytes, bytes]] = []
    for e in bundle["entries"]:
        pairs.append((_entry_imt_key(e["ep"], e["h"]), _entry_imt_value(e)))
    leaves = make_leaves_with_pointers(pairs)
    return imt_proof_non_inclusion(leaves, target_key)


def verify_episode_inclusion(
    proof: InclusionProof,
    *,
    expected_root: bytes,
) -> bool:
    """Public verifier entrypoint — wraps imt.verify_inclusion."""
    return verify_inclusion(proof, expected_root)


def verify_episode_non_inclusion(
    proof: NonInclusionProof,
    *,
    expected_root: bytes,
) -> bool:
    """Public verifier entrypoint — wraps imt.verify_non_inclusion."""
    return verify_non_inclusion(proof, expected_root)
