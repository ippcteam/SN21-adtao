"""Unit tests for hope/commitment/registration.py — hotkey ↔ ed25519 binding."""

from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.on_chain import RAW_FIELD_MAX_BYTES
from hope.commitment.registration import (
    REG_V1_PREFIX,
    RegistrationPayload,
    RegistrationRole,
    build_registration_payload,
    parse_registration_payload,
    verify_registration,
)


@pytest.fixture
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def ss58_pk():
    return os.urandom(32)


class TestBuildAndParse:
    def test_round_trip(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        parsed = parse_registration_payload(raw)
        assert parsed is not None
        assert parsed.role == RegistrationRole.MINER
        assert parsed.ed25519_pk == signing_key.public_key().public_bytes_raw()
        assert len(parsed.ed25519_sig) == 64

    def test_payload_fits_raw128(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.VALIDATOR,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        assert len(raw) == 109
        assert len(raw) <= RAW_FIELD_MAX_BYTES

    def test_starts_with_prefix(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.OUTCOME_SIGNER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        assert raw.startswith(REG_V1_PREFIX)

    def test_each_role_has_distinct_byte(self, signing_key, ss58_pk):
        m = build_registration_payload(role=RegistrationRole.MINER,
                                       ss58_pubkey=ss58_pk,
                                       ed25519_signing_key=signing_key)
        v = build_registration_payload(role=RegistrationRole.VALIDATOR,
                                       ss58_pubkey=ss58_pk,
                                       ed25519_signing_key=signing_key)
        o = build_registration_payload(role=RegistrationRole.OUTCOME_SIGNER,
                                       ss58_pubkey=ss58_pk,
                                       ed25519_signing_key=signing_key)
        # Role byte sits right after the prefix.
        offset = len(REG_V1_PREFIX)
        assert m[offset:offset + 1] == b"M"
        assert v[offset:offset + 1] == b"V"
        assert o[offset:offset + 1] == b"O"

    def test_parse_rejects_wrong_prefix(self):
        bad = b"different-prefix:" + b"\x00" * 100
        assert parse_registration_payload(bad) is None

    def test_parse_rejects_wrong_length(self):
        short = REG_V1_PREFIX + b"M" + b"\x00" * 50  # too short
        assert parse_registration_payload(short) is None

    def test_parse_rejects_unknown_role(self):
        raw = REG_V1_PREFIX + b"X" + b"\x00" * 32 + b"\x00" * 64
        assert parse_registration_payload(raw) is None


class TestVerifyRegistration:
    def test_correct_signature_verifies(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        parsed = parse_registration_payload(raw)
        assert verify_registration(parsed, ss58_pubkey=ss58_pk)

    def test_wrong_ss58_fails(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        parsed = parse_registration_payload(raw)
        wrong_ss58 = os.urandom(32)
        assert not verify_registration(parsed, ss58_pubkey=wrong_ss58)

    def test_role_mismatch_rejected(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        parsed = parse_registration_payload(raw)
        assert verify_registration(parsed, ss58_pubkey=ss58_pk,
                                   expected_role=RegistrationRole.MINER)
        assert not verify_registration(parsed, ss58_pubkey=ss58_pk,
                                       expected_role=RegistrationRole.VALIDATOR)

    def test_tampered_pk_fails(self, signing_key, ss58_pk):
        raw = bytearray(build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        ))
        # Flip a bit in ed25519_pk — sig was over the original pk; verify must fail
        raw[len(REG_V1_PREFIX) + 1] ^= 0x01
        parsed = parse_registration_payload(bytes(raw))
        # The tampered pk is what verify will use; it won't match the sig.
        assert not verify_registration(parsed, ss58_pubkey=ss58_pk)

    def test_tampered_sig_fails(self, signing_key, ss58_pk):
        raw = bytearray(build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        ))
        # Flip a bit in the signature
        raw[-1] ^= 0x01
        parsed = parse_registration_payload(bytes(raw))
        assert not verify_registration(parsed, ss58_pubkey=ss58_pk)

    def test_wrong_ss58_length(self, signing_key, ss58_pk):
        raw = build_registration_payload(
            role=RegistrationRole.MINER,
            ss58_pubkey=ss58_pk,
            ed25519_signing_key=signing_key,
        )
        parsed = parse_registration_payload(raw)
        assert not verify_registration(parsed, ss58_pubkey=b"\x00" * 31)

    def test_attacker_cannot_forge_binding(self):
        """Attacker holds SS58 but not the ed25519 private key.

        They can write to the chain slot (because they hold the SS58) but
        cannot produce a valid sig for an ed25519 pubkey they don't control.
        """
        attacker_ss58 = os.urandom(32)
        victim_key = Ed25519PrivateKey.generate()
        victim_pk = victim_key.public_key().public_bytes_raw()

        # Attacker tries to claim victim's ed25519 pk by writing it under their hotkey.
        # They produce a forged payload (random sig) since they don't have victim's privkey.
        forged = REG_V1_PREFIX + b"M" + victim_pk + os.urandom(64)
        parsed = parse_registration_payload(forged)
        assert parsed is not None
        # Verifier rejects: sig is invalid for (M, attacker_ss58, victim_pk).
        assert not verify_registration(parsed, ss58_pubkey=attacker_ss58)
