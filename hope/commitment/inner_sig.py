"""Inner-signature procedure for SN21 commit-reveal payloads (CL-9 fix).

Both miner predictions (Layer 9.B) and validator scoring commits
(Layers 9.C.1, 9.C.2) carry an `inner_sig` field whose digest covers the
canonical CBOR encoding of the plaintext WITHOUT the signature itself. The
signature binds the plaintext content to a specific hotkey, defeating:

- SH-5 (rubber-stamp attack): a shadow validator cannot copy the primary's
  TLE ciphertext bytes and pass them off as its own — the inner_sig
  recovered after auto-decrypt would not verify against the shadow's hotkey.
- Validator-rewrite attack on miner predictions: a malicious validator that
  somehow obtained AES_ct + K cannot edit the prediction CBOR and re-encrypt,
  because doing so would invalidate the miner's inner_sig.

The hotkey field name is configurable via `hotkey_field`:
- Miner predictions  → "miner_hotkey"
- Validator commits  → "validator_hotkey"

Procedure (per protocol spec v1 §6.1):

Sign:
    1. Build plaintext as map WITHOUT inner_sig field, WITH the hotkey field.
    2. plaintext_bytes_no_sig = canonical_cbor(plaintext)
    3. digest = blake2b_256(plaintext_bytes_no_sig)
    4. inner_sig = ed25519_sign(digest, signer_private_key)
    5. Add inner_sig field; return.

Verify:
    1. Extract inner_sig field; remove from map.
    2. plaintext_bytes_no_sig = canonical_cbor(plaintext)
    3. digest = blake2b_256(plaintext_bytes_no_sig)
    4. ed25519_verify(inner_sig, digest, public_key=plaintext[hotkey_field])
    5. Check plaintext[hotkey_field] == storage_key.account
"""

from __future__ import annotations

import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from hope.commitment.canonical import canonical_cbor_dumps


def add_inner_sig(
    plaintext: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    hotkey_field: str = "validator_hotkey",
) -> dict[str, Any]:
    """Compute and add the inner signature to a plaintext dict.

    The dict MUST already contain `hotkey_field` whose value is the 32-byte raw
    ed25519 public key matching `private_key`, and MUST NOT already contain an
    'inner_sig' field. `hotkey_field` is "validator_hotkey" by default; miners
    pass "miner_hotkey".

    Mutates and returns the same dict for convenience.
    """
    if "inner_sig" in plaintext:
        raise ValueError("plaintext already has inner_sig; remove before re-signing")
    if hotkey_field not in plaintext:
        raise ValueError(f"plaintext must contain {hotkey_field} before signing")

    expected_pub = private_key.public_key().public_bytes_raw()
    declared_pub = plaintext[hotkey_field]
    if not isinstance(declared_pub, bytes) or len(declared_pub) != 32:
        raise ValueError(f"{hotkey_field} must be 32 raw bytes")
    if declared_pub != expected_pub:
        raise ValueError(f"{hotkey_field} does not match the signing private key")

    plaintext_bytes = canonical_cbor_dumps(plaintext)
    digest = hashlib.blake2b(plaintext_bytes, digest_size=32).digest()
    sig = private_key.sign(digest)
    if len(sig) != 64:
        raise RuntimeError(f"unexpected ed25519 signature length: {len(sig)}")

    plaintext["inner_sig"] = sig
    return plaintext


def verify_inner_sig(
    plaintext: dict[str, Any],
    expected_hotkey: bytes,
    *,
    hotkey_field: str = "validator_hotkey",
) -> bool:
    """Verify the inner signature of a plaintext dict.

    expected_hotkey: the 32-byte raw ed25519 public key from the chain storage
    key. Substrate enforces that storage at (netuid, account) was written by
    the holder of `account`'s private key, so the plaintext's `hotkey_field`
    MUST equal this. `hotkey_field` is "validator_hotkey" by default; miner
    predictions are verified with hotkey_field="miner_hotkey".

    Returns True iff:
    - plaintext has both 'inner_sig' and `hotkey_field`
    - plaintext[hotkey_field] == expected_hotkey
    - signature verifies against (digest, plaintext[hotkey_field])
    """
    if "inner_sig" not in plaintext:
        return False
    if hotkey_field not in plaintext:
        return False
    if len(expected_hotkey) != 32:
        return False

    declared_pub = plaintext[hotkey_field]
    if not isinstance(declared_pub, bytes) or len(declared_pub) != 32:
        return False
    if declared_pub != expected_hotkey:
        return False

    sig = plaintext["inner_sig"]
    if not isinstance(sig, bytes) or len(sig) != 64:
        return False

    plaintext_no_sig = {k: v for k, v in plaintext.items() if k != "inner_sig"}
    plaintext_bytes = canonical_cbor_dumps(plaintext_no_sig)
    digest = hashlib.blake2b(plaintext_bytes, digest_size=32).digest()

    try:
        pub = Ed25519PublicKey.from_public_bytes(expected_hotkey)
        pub.verify(sig, digest)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False
