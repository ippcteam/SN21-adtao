"""Tests for the inner-signature procedure (CL-9 fix)."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.inner_sig import add_inner_sig, verify_inner_sig


def _make_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """Generate a fresh ed25519 keypair. Returns (private_key, public_key_raw_bytes)."""
    sk = Ed25519PrivateKey.generate()
    pk_bytes = sk.public_key().public_bytes_raw()
    return sk, pk_bytes


class TestAddInnerSig:
    def test_basic_sign(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "epoch_id": "WR-2026-W18", "scoring_hash": b"x" * 32}
        result = add_inner_sig(plaintext, sk)
        assert "inner_sig" in result
        assert isinstance(result["inner_sig"], bytes)
        assert len(result["inner_sig"]) == 64

    def test_rejects_pre_existing_sig(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "inner_sig": b"\x00" * 64}
        with pytest.raises(ValueError, match="already has inner_sig"):
            add_inner_sig(plaintext, sk)

    def test_rejects_missing_validator_hotkey(self):
        sk, _ = _make_keypair()
        plaintext = {"epoch_id": "X"}
        with pytest.raises(ValueError, match="validator_hotkey"):
            add_inner_sig(plaintext, sk)

    def test_rejects_invalid_validator_hotkey_length(self):
        sk, _ = _make_keypair()
        plaintext = {"validator_hotkey": b"\x00" * 31}
        with pytest.raises(ValueError):
            add_inner_sig(plaintext, sk)

    def test_rejects_mismatched_validator_hotkey(self):
        sk, _ = _make_keypair()
        # Use a *different* keypair's public key
        _, other_pk = _make_keypair()
        plaintext = {"validator_hotkey": other_pk}
        with pytest.raises(ValueError, match="does not match"):
            add_inner_sig(plaintext, sk)


class TestVerifyInnerSig:
    def test_valid_signature_verifies(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "epoch_id": "WR-2026-W18"}
        signed = add_inner_sig(plaintext, sk)
        assert verify_inner_sig(signed, pk) is True

    def test_wrong_expected_hotkey_rejects(self):
        sk, pk = _make_keypair()
        _, other_pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "epoch_id": "X"}
        signed = add_inner_sig(plaintext, sk)
        # Verifier expects a different hotkey
        assert verify_inner_sig(signed, other_pk) is False

    def test_tampered_payload_rejects(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "epoch_id": "WR-2026-W18"}
        signed = add_inner_sig(plaintext, sk)
        # Tamper with a non-sig field
        signed["epoch_id"] = "WR-2026-W19"
        assert verify_inner_sig(signed, pk) is False

    def test_tampered_signature_rejects(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "epoch_id": "WR-2026-W18"}
        signed = add_inner_sig(plaintext, sk)
        signed["inner_sig"] = b"\x00" * 64
        assert verify_inner_sig(signed, pk) is False

    def test_missing_inner_sig_rejects(self):
        _, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk}
        assert verify_inner_sig(plaintext, pk) is False

    def test_missing_validator_hotkey_rejects(self):
        sk, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "x": 1}
        signed = add_inner_sig(plaintext, sk)
        del signed["validator_hotkey"]
        assert verify_inner_sig(signed, pk) is False

    def test_validator_hotkey_mismatch_rejects(self):
        # Plaintext claims one hotkey but verifier expects another
        sk, pk = _make_keypair()
        _, other_pk = _make_keypair()
        plaintext = {"validator_hotkey": pk}
        signed = add_inner_sig(plaintext, sk)
        # Now manually set validator_hotkey in plaintext to mismatch
        signed["validator_hotkey"] = other_pk
        assert verify_inner_sig(signed, pk) is False

    def test_invalid_sig_length_rejects(self):
        _, pk = _make_keypair()
        plaintext = {"validator_hotkey": pk, "inner_sig": b"\x00" * 32}  # wrong length
        assert verify_inner_sig(plaintext, pk) is False


class TestRubberStampAttackDefense:
    """Verify the SH-5 rubber-stamp attack is rejected.

    Attack: shadow validator copies primary's TLE ciphertext bytes into their own
    on-chain commit. After drand reveal, both storage keys reveal the same
    plaintext claiming primary's hotkey. With CL-9 inner_sig, shadow's storage
    key (S) doesn't match plaintext.validator_hotkey (P) → rejected.
    """

    def test_shadow_cannot_rubber_stamp_primary_plaintext(self):
        primary_sk, primary_pk = _make_keypair()
        _shadow_sk, shadow_pk = _make_keypair()

        # Primary signs their pre-scoring state
        primary_plaintext = {
            "validator_hotkey": primary_pk,
            "epoch_id": "WR-2026-W18",
            "prediction_inputs_root": b"\xab" * 32,
        }
        signed = add_inner_sig(primary_plaintext, primary_sk)

        # Verifier reading primary's commit at storage_key = primary_pk: should pass
        assert verify_inner_sig(signed, primary_pk) is True

        # Shadow copies primary's plaintext bytes verbatim and submits at their own storage key
        # Verifier reading at storage_key = shadow_pk: should fail (plaintext.hotkey != storage_key)
        assert verify_inner_sig(signed, shadow_pk) is False

    def test_shadow_cannot_forge_primary_signature(self):
        # Shadow wants to publish content claiming primary's hotkey, but they don't
        # have primary's private key → can't sign.
        _primary_sk, primary_pk = _make_keypair()
        shadow_sk, _ = _make_keypair()

        plaintext = {"validator_hotkey": primary_pk, "epoch_id": "X"}
        # Shadow tries to sign with their own key but claim primary's hotkey
        with pytest.raises(ValueError, match="does not match"):
            add_inner_sig(plaintext, shadow_sk)
