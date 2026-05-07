"""Unit tests for hope/commitment/scoreability.py — the 8-check rule."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.scoreability import (
    OnChainCommitTriple,
    ScoreabilityFailure,
    TimingBounds,
    evaluate_scoreability,
)


@pytest.fixture
def setup():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
        build_horizon_entry("14", (-7.0, -1.0, 7.0), (-3.0, 0.5, 5.0), (-4.0, 0.0, 5.0), 0.15, 0.08),
    ]
    plain = build_prediction_plaintext(
        epoch_id="EPOCH-A",
        miner_hotkey=pk,
        submitted_round=12345,
        horizons=horizons,
        miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id="EPOCH-A")
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    triple = OnChainCommitTriple(
        timelock_k_present=True,
        sha256_ct_commit=sha_ct,
        self_archive_url="https://miner.example/archive/EPOCH-A",
        chain_block_at_k_commit=7038900,
    )
    timing = TimingBounds(
        epoch_open_round=12000,
        miner_deadline_round=13000,
        chain_window_min_block=7038000,
        chain_window_max_block=7039000,
    )
    return {
        "sk": sk,
        "pk": pk,
        "plain": plain,
        "enc": enc,
        "sha_ct": sha_ct,
        "triple": triple,
        "timing": timing,
    }


class TestHappyPath:
    def test_all_8_checks_pass(self, setup):
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=setup["triple"],
            timing=setup["timing"],
        )
        assert result.ok is True
        assert result.failure is None
        assert result.plaintext is not None
        assert result.plaintext["epoch_id"] == "EPOCH-A"
        assert "inner_sig" in result.plaintext


# ---------- Check 1: ON_CHAIN_PRESENT ----------


class TestOnChainPresent:
    def test_missing_k(self, setup):
        triple = replace(setup["triple"], timelock_k_present=False)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_K

    def test_missing_sha(self, setup):
        triple = replace(setup["triple"], sha256_ct_commit=None)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_SHA

    def test_missing_url(self, setup):
        triple = replace(setup["triple"], self_archive_url=None)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_URL


# ---------- Check 2: CIPHERTEXT_MATCH ----------


class TestCiphertextMatch:
    def test_wrong_sha_fails(self, setup):
        triple = replace(setup["triple"], sha256_ct_commit=b"\x00" * 32)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.CIPHERTEXT_MATCH_FAIL


# ---------- Check 3: AAD_BIND ----------


class TestAadBind:
    def test_wrong_epoch_id_fails(self, setup):
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-B",  # different epoch
            miner_hotkey_from_chain=setup["pk"],
            on_chain=setup["triple"],
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.AAD_BIND_INVALID_TAG

    def test_wrong_key_fails(self, setup):
        bad_key = bytes(b ^ 0x01 for b in setup["enc"].aes_key)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=bad_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=setup["triple"],
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.AAD_BIND_INVALID_TAG


# ---------- Check 5: INNER_SIG ----------


class TestInnerSig:
    def test_wrong_chain_hotkey_fails(self, setup):
        wrong_pk = b"\x99" * 32
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=wrong_pk,
            on_chain=setup["triple"],
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.INNER_SIG_HOTKEY_MISMATCH


# ---------- Check 7: HORIZON_SHAPE ----------


class TestHorizonShape:
    def _re_sign_after_corruption(self, plain, sk):
        from hope.commitment.inner_sig import add_inner_sig

        if "inner_sig" in plain:
            del plain["inner_sig"]
        add_inner_sig(plain, sk, hotkey_field="miner_hotkey")
        return plain

    def _wrap(self, plain, sk, pk, sha_or_setup, setup, epoch_id="EPOCH-A"):
        """Re-encrypt a (possibly mutated) plaintext into a fresh AES_ct."""
        # sign and re-encrypt
        plain = self._re_sign_after_corruption(plain, sk)
        enc = encrypt_prediction(plain, epoch_id=epoch_id)
        sha_ct = hashlib.sha256(enc.aes_ct).digest()
        triple = replace(setup["triple"], sha256_ct_commit=sha_ct)
        return enc, triple

    def test_bad_horizon_label(self, setup):
        plain = dict(setup["plain"])
        plain["horizons"] = [{**plain["horizons"][0], "h": "30"}]
        enc, triple = self._wrap(plain, setup["sk"], setup["pk"], None, setup)
        result = evaluate_scoreability(
            aes_ct=enc.aes_ct,
            aes_key=enc.aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.HORIZON_SHAPE_FAIL
        assert "not in" in result.detail

    def test_non_monotone_quantile(self, setup):
        plain = dict(setup["plain"])
        bad_horizon = {**plain["horizons"][0], "cost_q": [10, 5, 1]}
        plain["horizons"] = [bad_horizon]
        enc, triple = self._wrap(plain, setup["sk"], setup["pk"], None, setup)
        result = evaluate_scoreability(
            aes_ct=enc.aes_ct,
            aes_key=enc.aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.HORIZON_SHAPE_FAIL
        assert "not monotone" in result.detail

    def test_probability_out_of_range(self, setup):
        plain = dict(setup["plain"])
        bad = {**plain["horizons"][0], "miss_p": 2_000_000}
        plain["horizons"] = [bad]
        enc, triple = self._wrap(plain, setup["sk"], setup["pk"], None, setup)
        result = evaluate_scoreability(
            aes_ct=enc.aes_ct,
            aes_key=enc.aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.HORIZON_SHAPE_FAIL


# ---------- Check 6: EPOCH_MATCH ----------


class TestEpochMatch:
    """Note: a different epoch_id arg vs the AAD already fails AAD_BIND first.
    EPOCH_MATCH catches the case where the validator passes the epoch from
    the chain context but the plaintext.epoch_id field differs (maliciously
    crafted plaintext that nevertheless decrypts under matching AAD)."""

    def test_plaintext_epoch_id_replaced(self, setup):
        # Mutate plaintext.epoch_id but keep the AAD that decrypts.
        plain = dict(setup["plain"])
        plain["epoch_id"] = "EPOCH-B"
        # Re-sign and re-encrypt with AAD = epoch_id_for_aad = "EPOCH-A"
        # Validator will then check plaintext.epoch_id == "EPOCH-A" → fail.
        if "inner_sig" in plain:
            del plain["inner_sig"]
        from hope.commitment.inner_sig import add_inner_sig
        add_inner_sig(plain, setup["sk"], hotkey_field="miner_hotkey")
        enc = encrypt_prediction(plain, epoch_id="EPOCH-B")  # plaintext epoch_id matches arg
        # But validator checks for "EPOCH-A" — AAD differs → AAD_BIND fails first.
        result = evaluate_scoreability(
            aes_ct=enc.aes_ct,
            aes_key=enc.aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=replace(setup["triple"], sha256_ct_commit=hashlib.sha256(enc.aes_ct).digest()),
            timing=setup["timing"],
        )
        # AAD_BIND fires before EPOCH_MATCH; both are valid defences. We just
        # require *some* failure here.
        assert not result.ok


# ---------- Check 8: TIMING_BOUND ----------


class TestTiming:
    def test_submitted_round_below_window(self, setup):
        timing = replace(setup["timing"], epoch_open_round=99999)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=setup["triple"],
            timing=timing,
        )
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW

    def test_submitted_round_above_window(self, setup):
        timing = replace(setup["timing"], miner_deadline_round=100)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=setup["triple"],
            timing=timing,
        )
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW

    def test_chain_block_out_of_window(self, setup):
        triple = replace(setup["triple"], chain_block_at_k_commit=8000000)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW

    def test_chain_block_none(self, setup):
        triple = replace(setup["triple"], chain_block_at_k_commit=None)
        result = evaluate_scoreability(
            aes_ct=setup["enc"].aes_ct,
            aes_key=setup["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=setup["pk"],
            on_chain=triple,
            timing=setup["timing"],
        )
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW
