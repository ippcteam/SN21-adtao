"""Unit tests for hope/commitment/prediction_payload.py — Layer 9.B miner payload."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.prediction_payload import (
    AES_KEY_BYTES,
    AES_NONCE_BYTES,
    AES_TAG_BYTES,
    ALLOWED_HORIZONS,
    PROBABILITY_SCALE,
    PREDICTION_PAYLOAD_VERSION,
    QUANTILE_SCALE,
    build_horizon_entry,
    build_prediction_plaintext,
    decrypt_prediction,
    encrypt_prediction,
    verify_prediction_inner_sig,
    _is_valid_epoch_id,
    _scale_probability,
    _scale_quantile,
)


@pytest.fixture
def miner_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def horizons():
    return [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
        build_horizon_entry("14", (-7.0, -1.0, 7.0), (-3.0, 0.5, 5.0), (-4.0, 0.0, 5.0), 0.15, 0.08),
    ]


# ---------- _scale_quantile / _scale_probability ----------


class TestScaling:
    def test_quantile_positive(self):
        assert _scale_quantile(12.5) == 125

    def test_quantile_negative(self):
        assert _scale_quantile(-3.7) == -37

    def test_quantile_zero(self):
        assert _scale_quantile(0.0) == 0

    def test_probability_zero(self):
        assert _scale_probability(0.0) == 0

    def test_probability_one(self):
        assert _scale_probability(1.0) == PROBABILITY_SCALE

    def test_probability_half(self):
        assert _scale_probability(0.5) == PROBABILITY_SCALE // 2

    def test_probability_out_of_range_above(self):
        with pytest.raises(ValueError, match="out of range"):
            _scale_probability(1.5)

    def test_probability_out_of_range_below(self):
        with pytest.raises(ValueError, match="out of range"):
            _scale_probability(-0.1)


# ---------- _is_valid_epoch_id ----------


class TestEpochIdValidation:
    def test_uppercase_alnum_dash(self):
        assert _is_valid_epoch_id("EPOCH-2026-05-02-XYZ")

    def test_min_length_one(self):
        assert _is_valid_epoch_id("A")

    def test_max_length_80(self):
        assert _is_valid_epoch_id("A" * 80)

    def test_too_long_81(self):
        assert not _is_valid_epoch_id("A" * 81)

    def test_empty(self):
        assert not _is_valid_epoch_id("")

    def test_lowercase_rejected(self):
        assert not _is_valid_epoch_id("epoch-1")

    def test_special_char_rejected(self):
        assert not _is_valid_epoch_id("EPOCH/1")

    def test_unicode_rejected(self):
        assert not _is_valid_epoch_id("ÉPOCH")


# ---------- build_horizon_entry ----------


class TestBuildHorizonEntry:
    def test_basic(self):
        h = build_horizon_entry("7", (-1.0, 0.5, 2.0), (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), 0.2, 0.1)
        assert h["h"] == "7"
        assert h["cost_q"] == [-10, 5, 20]
        assert h["miss_p"] == 200_000
        assert h["instab_p"] == 100_000

    def test_invalid_horizon_label(self):
        with pytest.raises(ValueError, match="horizon must be one of"):
            build_horizon_entry("30", (0, 0, 0), (0, 0, 0), (0, 0, 0), 0.0, 0.0)

    def test_non_monotone_quantile_rejected(self):
        with pytest.raises(ValueError, match="not monotone"):
            build_horizon_entry("7", (5.0, 0.0, -5.0), (0, 0, 0), (0, 0, 0), 0.0, 0.0)

    def test_probability_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="out of range"):
            build_horizon_entry("7", (0, 0, 0), (0, 0, 0), (0, 0, 0), 1.5, 0.0)

    def test_all_horizons_allowed(self):
        for h in ALLOWED_HORIZONS:
            entry = build_horizon_entry(h, (0, 0, 0), (0, 0, 0), (0, 0, 0), 0, 0)
            assert entry["h"] == h


# ---------- build_prediction_plaintext ----------


class TestBuildPredictionPlaintext:
    def test_basic(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        assert plain["v"] == PREDICTION_PAYLOAD_VERSION
        assert plain["epoch_id"] == "EPOCH-1"
        assert plain["miner_hotkey"] == pk
        assert plain["submitted_round"] == 100
        assert "inner_sig" in plain
        assert len(plain["inner_sig"]) == 64

    def test_invalid_epoch_id_rejected(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        with pytest.raises(ValueError, match="epoch_id"):
            build_prediction_plaintext(
                epoch_id="lowercase",
                miner_hotkey=pk,
                submitted_round=100,
                horizons=horizons,
                miner_signing_key=miner_key,
            )

    def test_wrong_hotkey_length(self, miner_key, horizons):
        with pytest.raises(ValueError, match="miner_hotkey must be 32"):
            build_prediction_plaintext(
                epoch_id="EPOCH-1",
                miner_hotkey=b"\x00" * 31,
                submitted_round=100,
                horizons=horizons,
                miner_signing_key=miner_key,
            )

    def test_round_must_be_positive(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        with pytest.raises(ValueError, match="submitted_round must be positive"):
            build_prediction_plaintext(
                epoch_id="EPOCH-1",
                miner_hotkey=pk,
                submitted_round=0,
                horizons=horizons,
                miner_signing_key=miner_key,
            )

    def test_empty_horizons_rejected(self, miner_key):
        pk = miner_key.public_key().public_bytes_raw()
        with pytest.raises(ValueError, match="horizons must be non-empty"):
            build_prediction_plaintext(
                epoch_id="EPOCH-1",
                miner_hotkey=pk,
                submitted_round=100,
                horizons=[],
                miner_signing_key=miner_key,
            )

    def test_duplicate_horizon_rejected(self, miner_key):
        pk = miner_key.public_key().public_bytes_raw()
        with pytest.raises(ValueError, match="duplicate horizon"):
            build_prediction_plaintext(
                epoch_id="EPOCH-1",
                miner_hotkey=pk,
                submitted_round=100,
                horizons=[
                    build_horizon_entry("7", (0, 0, 0), (0, 0, 0), (0, 0, 0), 0, 0),
                    build_horizon_entry("7", (0, 0, 0), (0, 0, 0), (0, 0, 0), 0, 0),
                ],
                miner_signing_key=miner_key,
            )

    def test_signing_key_mismatch(self, horizons):
        sk1 = Ed25519PrivateKey.generate()
        sk2 = Ed25519PrivateKey.generate()
        pk1 = sk1.public_key().public_bytes_raw()
        with pytest.raises(ValueError, match="does not match"):
            build_prediction_plaintext(
                epoch_id="EPOCH-1",
                miner_hotkey=pk1,
                submitted_round=100,
                horizons=horizons,
                miner_signing_key=sk2,  # mismatch
            )


# ---------- encrypt / decrypt round-trip ----------


class TestEncryptDecrypt:
    def test_round_trip(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1")
        assert len(enc.aes_key) == AES_KEY_BYTES
        assert len(enc.aes_ct) >= AES_NONCE_BYTES + AES_TAG_BYTES
        back = decrypt_prediction(enc.aes_ct, enc.aes_key, epoch_id="EPOCH-1")
        assert back == plain

    def test_inner_sig_verifies_after_round_trip(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1")
        back = decrypt_prediction(enc.aes_ct, enc.aes_key, epoch_id="EPOCH-1")
        assert verify_prediction_inner_sig(back, pk) is True

    def test_wrong_epoch_aad_fails(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1")
        with pytest.raises(InvalidTag):
            decrypt_prediction(enc.aes_ct, enc.aes_key, epoch_id="EPOCH-2")

    def test_tampered_ciphertext_fails(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1")
        bad = bytearray(enc.aes_ct)
        bad[20] ^= 0x01  # flip one bit in ciphertext body
        with pytest.raises(InvalidTag):
            decrypt_prediction(bytes(bad), enc.aes_key, epoch_id="EPOCH-1")

    def test_wrong_key_fails(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1")
        bad_key = bytes(b ^ 0x01 for b in enc.aes_key)
        with pytest.raises(InvalidTag):
            decrypt_prediction(enc.aes_ct, bad_key, epoch_id="EPOCH-1")

    def test_aes_key_explicit(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        fixed_key = b"\x42" * 32
        enc = encrypt_prediction(plain, epoch_id="EPOCH-1", aes_key=fixed_key)
        assert enc.aes_key == fixed_key

    def test_epoch_mismatch_raises_value_error(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        with pytest.raises(ValueError, match="epoch_id mismatch"):
            encrypt_prediction(plain, epoch_id="EPOCH-2")

    def test_missing_inner_sig_raises(self):
        with pytest.raises(ValueError, match="missing inner_sig"):
            encrypt_prediction({"epoch_id": "X"}, epoch_id="X")

    def test_aes_ct_too_short(self):
        with pytest.raises(ValueError, match="aes_ct too short"):
            decrypt_prediction(b"\x00" * 5, b"\x00" * 32, epoch_id="X")

    def test_canonical_cbor_round_trip(self, miner_key, horizons):
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-1",
            miner_hotkey=pk,
            submitted_round=100,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        encoded = canonical_cbor_dumps(plain)
        # Re-encoding must be byte-identical
        re_encoded = canonical_cbor_dumps(canonical_cbor_loads(encoded))
        assert re_encoded == encoded


class TestPayloadSizeBudget:
    def test_typical_2horizon_under_768_bytes(self, miner_key, horizons):
        """Plaintext CBOR for 2 horizons must fit comfortably under TLE budget.

        We don't TLE-encrypt the plaintext directly (we TLE-encrypt the 32-byte
        K), but the AES_ct goes off-chain. This test guards regression on the
        plaintext shape — keeping it small reduces archive bandwidth.
        """
        pk = miner_key.public_key().public_bytes_raw()
        plain = build_prediction_plaintext(
            epoch_id="EPOCH-2026-05-02-XYZ",
            miner_hotkey=pk,
            submitted_round=12345678,
            horizons=horizons,
            miner_signing_key=miner_key,
        )
        encoded = canonical_cbor_dumps(plain)
        assert len(encoded) < 350, f"prediction plaintext too large: {len(encoded)}"
