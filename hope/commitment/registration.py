"""Hotkey ↔ ed25519 key binding registration (Phase D).

Background
----------
The Bittensor SS58 hotkey is the storage account that writes commits on chain
— Substrate enforces this at the runtime level. SS58 keys are typically
sr25519 (default) but can be ed25519. Our `inner_sig` primitive uses ed25519
because it's:

  - the scheme drand quicknet uses (BLS-on-BLS12-381 for the pulses, but the
    application keys we control are ed25519);
  - the only scheme cleanly available in `cryptography` (Schnorrkel needs
    a third-party crate);
  - cryptographically equivalent for our needs (32-byte pubkey, 64-byte sig,
    deterministic, well-supported).

If a miner's Bittensor hotkey is sr25519, we still need a verifier-trusted
ed25519 public key bound to the SAME entity. This module publishes that
binding ONCE per role (miner / validator / outcome_signer) via an on-chain
`Raw{N}` commit that:

  1. Carries `b"sn21-reg-v1:" + role + b":" + ed25519_pk_hex` as payload.
  2. Is written from the Bittensor hotkey itself — Substrate locks storage
     to the writer, so the SS58 ↔ payload binding is provable from chain
     state alone.
  3. Is co-signed inside the payload by the ed25519 key, so a holder of the
     SS58 cannot bind a key they don't own AND a holder of the ed25519 key
     cannot bind a hotkey they don't own.

Layout (Raw{N} bytes, N≤128):

    REG_V1_PREFIX || role_byte || ed25519_pk(32) || ed25519_sig(64)

with `REG_V1_PREFIX = b"sn21-reg-v1:"` (12 bytes) and `role_byte` one of
`b"M"`, `b"V"`, `b"O"`. Total = 12 + 1 + 32 + 64 = 109 bytes — fits in Raw128.

The `ed25519_sig` covers `domain_separator(role) || ss58_decoded_pubkey ||
ed25519_pk` so a signature for the wrong (hotkey, role, ed25519_pk) tuple
verifies false.

Verifier flow
-------------
A third party reading chain finds the latest `Raw{N}` commit at (netuid,
hotkey_account). If the bytes start with `REG_V1_PREFIX`, they parse the
binding, verify the embedded sig, and treat the resulting ed25519 public key
as the canonical inner_sig key for that hotkey. Subsequent inner_sig
verifications use this key instead of decoding the SS58 directly.

If no registration is found, the verifier falls back to treating the SS58 as
ed25519 (works only when the hotkey was created with `crypto_type=Ed25519`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


REG_V1_PREFIX = b"sn21-reg-v1:"


class RegistrationRole(str, Enum):
    """Which role's binding this registration represents."""

    MINER = "M"
    VALIDATOR = "V"
    OUTCOME_SIGNER = "O"


@dataclass(frozen=True)
class RegistrationPayload:
    """Decoded registration payload from a chain `Raw{N}` commit."""

    role: RegistrationRole
    ed25519_pk: bytes  # 32 bytes
    ed25519_sig: bytes  # 64 bytes

    @property
    def total_bytes(self) -> int:
        return len(REG_V1_PREFIX) + 1 + 32 + 64  # 109


def _domain_message(role: RegistrationRole, ss58_pubkey: bytes, ed25519_pk: bytes) -> bytes:
    """Domain-separated message that the ed25519 key signs.

    The substrate hotkey writes the chain commit (proves SS58 ownership);
    the ed25519 key signs the same (role, hotkey, ed25519_pk) tuple to prove
    it consents to being bound.
    """
    if len(ss58_pubkey) != 32:
        raise ValueError(f"ss58_pubkey must be 32 bytes, got {len(ss58_pubkey)}")
    if len(ed25519_pk) != 32:
        raise ValueError(f"ed25519_pk must be 32 bytes, got {len(ed25519_pk)}")
    return (
        REG_V1_PREFIX
        + role.value.encode("ascii")
        + b":"
        + ss58_pubkey
        + ed25519_pk
    )


def build_registration_payload(
    *,
    role: RegistrationRole,
    ss58_pubkey: bytes,
    ed25519_signing_key: Ed25519PrivateKey,
) -> bytes:
    """Build the `Raw{N}` payload bytes a hotkey will commit on chain.

    Args:
        role: which role this registers.
        ss58_pubkey: 32-byte raw public key derived from the SS58 address. The
            chain extrinsic must be signed by this hotkey; the ss58_pubkey is
            embedded in the signed message so an attacker swapping their
            hotkey out cannot reuse a sig from elsewhere.
        ed25519_signing_key: the ed25519 PRIVATE key to be bound. Its public
            key is embedded in the payload.

    Returns:
        Bytes ready to publish via `submit_raw_commit_layer_d_registration`.
        Length is exactly 109 bytes — well within `RAW_FIELD_MAX_BYTES=128`.
    """
    ed25519_pk = ed25519_signing_key.public_key().public_bytes_raw()
    msg = _domain_message(role, ss58_pubkey, ed25519_pk)
    sig = ed25519_signing_key.sign(msg)
    if len(sig) != 64:
        raise RuntimeError(f"unexpected ed25519 sig length: {len(sig)}")

    return REG_V1_PREFIX + role.value.encode("ascii") + ed25519_pk + sig


def parse_registration_payload(raw: bytes) -> Optional[RegistrationPayload]:
    """Parse a chain `Raw{N}` payload into a RegistrationPayload, or None.

    Returns None if `raw` is malformed (wrong prefix, wrong length, unknown
    role byte). The verifier MUST still run `verify_registration` to check
    the ed25519 signature.
    """
    if not raw.startswith(REG_V1_PREFIX):
        return None
    expected_total = len(REG_V1_PREFIX) + 1 + 32 + 64
    if len(raw) != expected_total:
        return None
    role_byte = raw[len(REG_V1_PREFIX) : len(REG_V1_PREFIX) + 1]
    try:
        role = RegistrationRole(role_byte.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    ed25519_pk = raw[len(REG_V1_PREFIX) + 1 : len(REG_V1_PREFIX) + 1 + 32]
    ed25519_sig = raw[len(REG_V1_PREFIX) + 1 + 32 :]
    return RegistrationPayload(
        role=role, ed25519_pk=ed25519_pk, ed25519_sig=ed25519_sig
    )


def verify_registration(
    payload: RegistrationPayload,
    *,
    ss58_pubkey: bytes,
    expected_role: Optional[RegistrationRole] = None,
) -> bool:
    """Verify a parsed RegistrationPayload's ed25519 signature.

    Args:
        payload: parsed RegistrationPayload (from parse_registration_payload).
        ss58_pubkey: 32-byte raw public key of the chain hotkey writer.
            Substrate guarantees this is the writer; the verifier obtains it
            from the chain storage key, NOT from the payload itself.
        expected_role: if provided, also check `payload.role == expected_role`.

    Returns:
        True iff the ed25519 sig over (role, ss58_pubkey, ed25519_pk) verifies
        against `payload.ed25519_pk` AND the role matches (when provided).
    """
    if expected_role is not None and payload.role != expected_role:
        return False
    if len(ss58_pubkey) != 32:
        return False
    msg = _domain_message(payload.role, ss58_pubkey, payload.ed25519_pk)
    try:
        Ed25519PublicKey.from_public_bytes(payload.ed25519_pk).verify(
            payload.ed25519_sig, msg
        )
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def submit_registration_commit(
    subtensor,
    wallet,
    netuid: int,
    *,
    role: RegistrationRole,
    ss58_pubkey: bytes,
    ed25519_signing_key: Ed25519PrivateKey,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
):
    """One-shot helper: build the payload + publish via `Raw{N}` commit.

    Wraps `submit_raw_url_commit_layer_9b` — the SDK helper accepts arbitrary
    `Raw{N}` bytes (the function name says "url" but the underlying chain
    variant is generic). For SN21 we publish 109 bytes; the chain caps Raw at
    128, so this fits comfortably.

    Returns the same `CommitResult` the underlying helper returns.
    """
    from hope.commitment.on_chain import _to_commit_result
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic

    payload = build_registration_payload(
        role=role,
        ss58_pubkey=ss58_pubkey,
        ed25519_signing_key=ed25519_signing_key,
    )
    response = publish_metadata_extrinsic(
        subtensor=subtensor,
        wallet=wallet,
        netuid=netuid,
        data_type=f"Raw{len(payload)}",
        data=payload,
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )
    return _to_commit_result(response, reveal_round=None)
