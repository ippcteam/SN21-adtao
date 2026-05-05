"""Canonical CBOR encoding (RFC 8949 §4.2.1) + AES-GCM AAD construction.

Per protocol spec v1 §0.2: canonical CBOR uses definite-length, lexicographically
sorted keys, smallest-form integers, no indefinite-length items, no extension types.

The cbor2 library's `canonical=True` mode implements exactly this. We wrap it for
clarity and to centralize the encoding choice for the whole codebase.
"""

from __future__ import annotations

import cbor2

# AAD prefix for AES-GCM encryption of miner predictions
_AAD_PREFIX_PREDICTION = "sn21-prediction-v1"


def canonical_cbor_dumps(obj: object) -> bytes:
    """Encode an object to canonical CBOR per RFC 8949 §4.2.1.

    Definite-length, sorted keys, smallest-form integers, no indefinite items.
    The encoding is byte-deterministic across Python versions and library versions
    that respect canonical=True.
    """
    return cbor2.dumps(obj, canonical=True)


def canonical_cbor_loads(data: bytes) -> object:
    """Decode canonical CBOR bytes back to a Python object.

    Re-encoding the result must yield the original bytes; if not, the input
    was not canonically encoded and should be rejected.
    """
    return cbor2.loads(data)


def aes_gcm_aad(epoch_id: str) -> bytes:
    """Construct AES-GCM Associated Data for a miner prediction commit.

    AAD = b"sn21-prediction-v1:{epoch_id}" as exact UTF-8 bytes.

    The epoch_id binding prevents cross-epoch ciphertext replay (T-16 fix).
    the operator's release_key format is constrained to [A-Z0-9-]{1,80}, so no
    special-character ambiguity is possible.
    """
    return f"{_AAD_PREFIX_PREDICTION}:{epoch_id}".encode("utf-8")


def is_canonically_encoded(data: bytes) -> bool:
    """Round-trip test: decode then re-encode; if bytes match, input was canonical.

    Used by verifier to reject non-canonical CBOR inputs (scoreability rule check #5).
    """
    try:
        obj = canonical_cbor_loads(data)
        re_encoded = canonical_cbor_dumps(obj)
        return data == re_encoded
    except Exception:
        return False
