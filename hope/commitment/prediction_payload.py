"""Layer 9.B miner prediction payload — canonical CBOR + AES-GCM hybrid envelope.

A miner prediction commit splits cleanly across the chain limits:
  - On chain (TimelockEncrypted, ≤768 plaintext): the 32-byte AES-GCM key K
    plus its drand reveal_round, encrypted to a future round.
  - On chain (Sha256, 32 bytes): SHA-256 of AES_ct (binds chain-side ciphertext
    integrity to a chain-anchored, hotkey-bound storage slot).
  - On chain (Raw{N}, ≤128 bytes): self_archive_url for Tier-3 lookup.
  - Off chain (three-tier archive): AES_ct itself, the variable-size payload.

This module owns the OFF-CHAIN portion: building the prediction CBOR with
inner_sig, then AES-GCM encrypting it with a fresh K and the
epoch-bound AAD. The ON-CHAIN side (publishing K, Sha256(AES_ct), URL) is in
hope/miner/onchain_submitter.py.

AAD construction (per protocol spec v1 §2.4):
    AAD = b"sn21-prediction-v1:{epoch_id}"

This binds the ciphertext to a specific epoch — replaying AES_ct from epoch X
under epoch Y's K (even if K were the same) fails AES-GCM tag verification.

CBOR layout (canonical, RFC 8949 §4.2.1):
    {
      "v": 1,                               # protocol version
      "epoch_id": tstr,                     # release_key, [A-Z0-9-]{1,80}
      "miner_hotkey": bstr (32),            # raw ed25519 public key
      "submitted_round": uint,              # drand round at submission
      "horizons": [
        {
          "h": "7" | "14",
          "cost_q":  [int, int, int],       # P10/P50/P90, deci-percent (×10)
          "conv_q":  [int, int, int],
          "eff_q":   [int, int, int],
          "miss_p":  uint,                  # ppm, 0..1_000_000
          "instab_p": uint,                 # ppm, 0..1_000_000
        },
        ...
      ],
      "inner_sig": bstr (64),               # ed25519 over digest WITHOUT this field
    }

Quantiles are encoded as integers in deci-percent (×10) so a P50 of -3.7%
becomes -37. This avoids float non-determinism while keeping the range wide
enough for any plausible delta. Probabilities are uint ppm in [0, 1_000_000].
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import (
    aes_gcm_aad,
    canonical_cbor_dumps,
    canonical_cbor_loads,
)
from hope.commitment.inner_sig import add_inner_sig, verify_inner_sig


# Protocol version embedded in every payload.
PREDICTION_PAYLOAD_VERSION = 1

# AES-GCM constants (NIST SP 800-38D).
AES_KEY_BYTES = 32          # AES-256
AES_NONCE_BYTES = 12        # 96-bit IV (recommended)
AES_TAG_BYTES = 16          # 128-bit tag (default for AESGCM)

# Quantile scale: ×10 so −3.7% encodes as −37 (deci-percent).
QUANTILE_SCALE = 10

# Probability scale: ppm so 0.523 encodes as 523_000 (parts-per-million).
PROBABILITY_SCALE = 1_000_000

# Allowed horizon labels.
ALLOWED_HORIZONS: tuple[str, ...] = ("7", "14")


@dataclass(frozen=True)
class EncryptedPrediction:
    """Result of encrypting a miner prediction CBOR with a fresh AES-GCM key.

    `aes_ct` already includes the 12-byte nonce prefix and 16-byte GCM tag
    suffix; layout is `nonce || ciphertext || tag` so a single field round-trips
    cleanly through HTTP and back.
    """

    aes_key: bytes        # 32-byte K to be timelock-encrypted on chain
    aes_ct: bytes         # nonce(12) || ciphertext || tag(16)
    plaintext_cbor: bytes # canonical CBOR plaintext (kept for self-tests; do not publish)


def _scale_quantile(value: float) -> int:
    """Convert a percent value into the integer deci-percent encoding.

    -3.7% → -37; +12.5% → 125. Rounded half-to-even (Python default).
    """
    return int(round(value * QUANTILE_SCALE))


def _scale_probability(value: float) -> int:
    """Convert a [0, 1] probability into a uint ppm encoding.

    Validates the input range and rounds half-to-even.
    """
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"probability out of range [0, 1]: {value}")
    return int(round(value * PROBABILITY_SCALE))


def _validate_quantile_triple(q: tuple[float, float, float], name: str) -> list[int]:
    p10, p50, p90 = q
    if not (p10 <= p50 <= p90):
        raise ValueError(
            f"{name} quantiles not monotone: p10={p10} <= p50={p50} <= p90={p90}"
        )
    return [_scale_quantile(p10), _scale_quantile(p50), _scale_quantile(p90)]


def build_horizon_entry(
    horizon: str,
    cost_q: tuple[float, float, float],
    conv_q: tuple[float, float, float],
    eff_q: tuple[float, float, float],
    goal_miss_probability: float,
    instability_risk: float,
) -> dict[str, Any]:
    """Construct one horizon entry for the prediction CBOR.

    Validates horizon label, quantile monotonicity, and probability ranges.
    All numeric outputs are integers per the canonical-encoding rule.
    """
    if horizon not in ALLOWED_HORIZONS:
        raise ValueError(f"horizon must be one of {ALLOWED_HORIZONS}, got {horizon!r}")
    return {
        "h": horizon,
        "cost_q": _validate_quantile_triple(cost_q, "cost_q"),
        "conv_q": _validate_quantile_triple(conv_q, "conv_q"),
        "eff_q":  _validate_quantile_triple(eff_q,  "eff_q"),
        "miss_p":  _scale_probability(goal_miss_probability),
        "instab_p": _scale_probability(instability_risk),
    }


def build_prediction_plaintext(
    *,
    epoch_id: str,
    miner_hotkey: bytes,
    submitted_round: int,
    horizons: list[dict[str, Any]],
    miner_signing_key: Ed25519PrivateKey,
    version: int = PREDICTION_PAYLOAD_VERSION,
    episodes_root: bytes | None = None,
) -> dict[str, Any]:
    """Build the full prediction plaintext dict and attach inner_sig.

    Args:
        epoch_id: the operator release_key, must match [A-Z0-9-]{1,80}.
        miner_hotkey: 32-byte raw ed25519 public key matching miner_signing_key.
        submitted_round: drand round at submission time (binds replay).
        horizons: list of dicts as produced by `build_horizon_entry`.
        miner_signing_key: ed25519 private key for the miner hotkey.
        version: protocol version; default 1.
        episodes_root: optional 32-byte IMT root over the per-episode bundle
            (Phase E). When supplied, it is bound by the aggregated plaintext's
            inner_sig and travels on-chain via AES_ct, anchoring the off-chain
            bundle. Pre-Phase-E miners may omit it and the field is absent.

    Returns:
        dict ready to be canonical-CBOR-encoded.
    """
    if not _is_valid_epoch_id(epoch_id):
        raise ValueError(
            f"epoch_id must match [A-Z0-9-]{{1,80}}, got {epoch_id!r}"
        )
    if len(miner_hotkey) != 32:
        raise ValueError(f"miner_hotkey must be 32 bytes, got {len(miner_hotkey)}")
    if submitted_round <= 0:
        raise ValueError(f"submitted_round must be positive, got {submitted_round}")
    if not horizons:
        raise ValueError("horizons must be non-empty")
    if episodes_root is not None and len(episodes_root) != 32:
        raise ValueError(
            f"episodes_root must be 32 bytes, got {len(episodes_root)}"
        )

    seen = set()
    for h in horizons:
        label = h.get("h")
        if label in seen:
            raise ValueError(f"duplicate horizon: {label!r}")
        seen.add(label)

    plaintext: dict[str, Any] = {
        "v": int(version),
        "epoch_id": epoch_id,
        "miner_hotkey": miner_hotkey,
        "submitted_round": int(submitted_round),
        "horizons": horizons,
    }
    if episodes_root is not None:
        plaintext["episodes_root"] = episodes_root
    return add_inner_sig(plaintext, miner_signing_key, hotkey_field="miner_hotkey")


def encrypt_prediction(
    plaintext_dict: dict[str, Any],
    *,
    epoch_id: str,
    aes_key: bytes | None = None,
    nonce: bytes | None = None,
) -> EncryptedPrediction:
    """Canonical-CBOR-encode and AES-GCM encrypt a prediction plaintext.

    Args:
        plaintext_dict: as produced by `build_prediction_plaintext`. Must include
            `epoch_id` and `inner_sig`.
        epoch_id: must equal plaintext_dict["epoch_id"]; binds AAD to the epoch.
        aes_key: 32 bytes; if None, a fresh key is generated via os.urandom.
        nonce: 12 bytes; if None, a fresh nonce is generated via os.urandom.
            Production callers should leave this None — supplying a fixed nonce
            is ONLY for migration / replay (where determinism matters more
            than nonce-reuse risk; same key+nonce+plaintext yields the same
            ciphertext, which is the goal there).

    Returns:
        EncryptedPrediction with key, ciphertext (nonce||ct||tag), and the
        original plaintext CBOR for self-test.

    Raises:
        ValueError: epoch mismatch, missing inner_sig, or bad key/nonce length.
    """
    if "inner_sig" not in plaintext_dict:
        raise ValueError("plaintext_dict missing inner_sig; call build_prediction_plaintext first")
    if plaintext_dict.get("epoch_id") != epoch_id:
        raise ValueError(
            f"epoch_id mismatch: plaintext={plaintext_dict.get('epoch_id')!r}, arg={epoch_id!r}"
        )

    if aes_key is None:
        aes_key = os.urandom(AES_KEY_BYTES)
    elif len(aes_key) != AES_KEY_BYTES:
        raise ValueError(f"aes_key must be {AES_KEY_BYTES} bytes, got {len(aes_key)}")

    if nonce is None:
        nonce = os.urandom(AES_NONCE_BYTES)
    elif len(nonce) != AES_NONCE_BYTES:
        raise ValueError(f"nonce must be {AES_NONCE_BYTES} bytes, got {len(nonce)}")
    aad = aes_gcm_aad(epoch_id)

    plaintext_cbor = canonical_cbor_dumps(plaintext_dict)

    cipher = AESGCM(aes_key)
    ct_with_tag = cipher.encrypt(nonce, plaintext_cbor, aad)

    aes_ct = nonce + ct_with_tag
    return EncryptedPrediction(
        aes_key=aes_key,
        aes_ct=aes_ct,
        plaintext_cbor=plaintext_cbor,
    )


def decrypt_prediction(
    aes_ct: bytes,
    aes_key: bytes,
    *,
    epoch_id: str,
) -> dict[str, Any]:
    """AES-GCM decrypt + canonical-CBOR decode a miner prediction.

    Used by validators (after K reveals on chain) and by the public verifier.

    Args:
        aes_ct: nonce(12) || ciphertext || tag(16).
        aes_key: 32-byte AES-GCM key (chain-revealed K).
        epoch_id: must match the AAD used during encryption.

    Returns:
        Decoded prediction dict. Caller is responsible for verifying inner_sig
        against the on-chain miner hotkey.

    Raises:
        ValueError: malformed input lengths.
        cryptography.exceptions.InvalidTag: AAD mismatch, key mismatch, or
            tampered ciphertext.
    """
    if len(aes_key) != AES_KEY_BYTES:
        raise ValueError(f"aes_key must be {AES_KEY_BYTES} bytes, got {len(aes_key)}")
    if len(aes_ct) < AES_NONCE_BYTES + AES_TAG_BYTES:
        raise ValueError(
            f"aes_ct too short: {len(aes_ct)} bytes, "
            f"min={AES_NONCE_BYTES + AES_TAG_BYTES}"
        )

    nonce = aes_ct[:AES_NONCE_BYTES]
    ct_with_tag = aes_ct[AES_NONCE_BYTES:]
    aad = aes_gcm_aad(epoch_id)

    cipher = AESGCM(aes_key)
    plaintext_cbor = cipher.decrypt(nonce, ct_with_tag, aad)
    obj = canonical_cbor_loads(plaintext_cbor)
    if not isinstance(obj, dict):
        raise ValueError(f"decoded payload is not a CBOR map: {type(obj).__name__}")
    return obj


def verify_prediction_inner_sig(
    plaintext_dict: dict[str, Any],
    expected_miner_hotkey: bytes,
) -> bool:
    """Verify the miner inner_sig against an expected hotkey from chain storage."""
    return verify_inner_sig(
        plaintext_dict, expected_miner_hotkey, hotkey_field="miner_hotkey"
    )


def _is_valid_epoch_id(s: str) -> bool:
    """epoch_id must match [A-Z0-9-]{1,80} per the operator release_key constraint."""
    if not isinstance(s, str) or not (1 <= len(s) <= 80):
        return False
    return all(("A" <= c <= "Z") or ("0" <= c <= "9") or c == "-" for c in s)
