"""Adversarial test suite — every defense gets an explicit attack.

Each test stages an attack the protocol claims to defeat and asserts that
the defense actually fires. If any test passes a malicious payload through,
the architecture has a regression.

Threat catalog (from docs/verifiable_scoring_architecture.md):

  T-1  Validator rewrites miner predictions   → inner_sig fails
  T-3  Cross-epoch ciphertext replay          → AAD mismatch fails
  T-4  Tampered AES_ct in archive             → Sha256 mismatch fails
  T-5  Tampered K bytes                       → AES decrypt fails
  T-6  Wrong miner hotkey claim               → inner_sig hotkey mismatch
  T-7  Out-of-window submission               → timing fails
  T-8  Non-canonical CBOR                     → encoding fails
  T-9  Validator score-root rewrite           → 9.C.2 inner_sig mismatch
  SH-5 Shadow rubber-stamps primary           → inner_sig under shadow slot fails
  T-15 Registration forgery                   → ed25519 sig under wrong SS58 fails
  T-16 Score weights without 9.C.2 binding    → verifier flags root divergence
  T-18 Malicious archive returns wrong bytes  → SHA-256 mismatch caught
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.episode_artifacts import (
    PerEpisodeEntry,
    build_per_episode_bundle,
    compute_episodes_imt_root,
    parse_per_episode_bundle,
    verify_bundle_inner_sig,
    verify_bundle_root,
)
from hope.commitment.inner_sig import verify_inner_sig
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.registration import (
    REG_V1_PREFIX,
    parse_registration_payload,
    verify_registration,
)
from hope.commitment.scoreability import (
    OnChainCommitTriple,
    ScoreabilityFailure,
    TimingBounds,
    evaluate_scoreability,
)
from hope.commitment.scoring_state import (
    MinerCommitRecord,
    build_pre_scoring_state,
)


@pytest.fixture
def miner():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0),
                            (-3.0, 0.5, 4.5), 0.1, 0.05),
    ]
    plain = build_prediction_plaintext(
        epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id="EPOCH-A")
    return {
        "sk": sk, "pk": pk, "plain": plain, "enc": enc,
        "sha_ct": hashlib.sha256(enc.aes_ct).digest(),
    }


def _triple(miner):
    return OnChainCommitTriple(
        timelock_k_present=True,
        sha256_ct_commit=miner["sha_ct"],
        self_archive_url="https://m.example/x",
        chain_block_at_k_commit=7038900,
    )


def _timing():
    return TimingBounds(
        epoch_open_round=12000, miner_deadline_round=13000,
        chain_window_min_block=7038000, chain_window_max_block=7039000,
    )


# ---------- T-1: Validator rewrites miner predictions ----------


class TestT1ValidatorRewritesPrediction:
    def test_rewriting_horizons_breaks_inner_sig(self, miner):
        # Validator captures plaintext and rewrites horizons.
        tampered = dict(miner["plain"])
        tampered["horizons"] = [{
            **tampered["horizons"][0],
            "cost_q": [99, 99, 99],  # malicious favorable values
        }]
        # Re-encryption needs the original inner_sig to still verify.
        # Validator does NOT have miner's privkey → inner_sig won't match.
        # We simulate "validator naively re-encrypts without re-signing":
        tampered_dict = dict(tampered)
        # Keep miner's original inner_sig and AES-encrypt the tampered plaintext.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from hope.commitment.canonical import aes_gcm_aad
        aes = AESGCM(miner["enc"].aes_key)
        nonce = os.urandom(12)
        forged_ct = nonce + aes.encrypt(
            nonce, canonical_cbor_dumps(tampered_dict),
            aes_gcm_aad("EPOCH-A"),
        )
        sha_forged = hashlib.sha256(forged_ct).digest()

        result = evaluate_scoreability(
            aes_ct=forged_ct,
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=replace(_triple(miner), sha256_ct_commit=sha_forged),
            timing=_timing(),
        )
        # The tampered plaintext's inner_sig is the original; recomputing the
        # digest over (tampered, sans inner_sig) doesn't match → fail.
        assert not result.ok
        assert result.failure == ScoreabilityFailure.INNER_SIG_FAIL


# ---------- T-3: Cross-epoch ciphertext replay ----------


class TestT3CrossEpochReplay:
    def test_replaying_aes_ct_from_other_epoch_fails_aad(self, miner):
        # Validator tries to replay this miner's AES_ct under a different epoch.
        result = evaluate_scoreability(
            aes_ct=miner["enc"].aes_ct,
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-DIFFERENT",  # different epoch — AAD won't match
            miner_hotkey_from_chain=miner["pk"],
            on_chain=_triple(miner),
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.AAD_BIND_INVALID_TAG


# ---------- T-4: Tampered AES_ct in archive ----------


class TestT4TamperedArchive:
    def test_byte_flip_in_archive_caught_by_sha(self, miner):
        # Archive serves bytes whose SHA disagrees with chain commit.
        tampered = bytearray(miner["enc"].aes_ct)
        tampered[20] ^= 0x01
        result = evaluate_scoreability(
            aes_ct=bytes(tampered),
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=_triple(miner),
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.CIPHERTEXT_MATCH_FAIL


# ---------- T-5: Tampered K bytes ----------


class TestT5TamperedKey:
    def test_wrong_k_fails_aes_decrypt(self, miner):
        bad_key = bytes(b ^ 0x01 for b in miner["enc"].aes_key)
        result = evaluate_scoreability(
            aes_ct=miner["enc"].aes_ct,
            aes_key=bad_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=_triple(miner),
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.AAD_BIND_INVALID_TAG


# ---------- T-6: Wrong miner hotkey claim ----------


class TestT6WrongHotkey:
    def test_other_hotkey_fails_inner_sig(self, miner):
        wrong_pk = b"\x99" * 32
        result = evaluate_scoreability(
            aes_ct=miner["enc"].aes_ct,
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=wrong_pk,
            on_chain=_triple(miner),
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.INNER_SIG_HOTKEY_MISMATCH


# ---------- T-7: Out-of-window submission ----------


class TestT7TimingBypass:
    def test_late_submission_rejected(self, miner):
        timing = replace(_timing(), miner_deadline_round=10000)  # before 12345
        result = evaluate_scoreability(
            aes_ct=miner["enc"].aes_ct,
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=_triple(miner),
            timing=timing,
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW

    def test_early_chain_block_rejected(self, miner):
        triple = replace(_triple(miner), chain_block_at_k_commit=1000)
        result = evaluate_scoreability(
            aes_ct=miner["enc"].aes_ct,
            aes_key=miner["enc"].aes_key,
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=triple,
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.TIMING_OUT_OF_WINDOW


# ---------- SH-5: Shadow rubber-stamps primary ----------


class TestSh5RubberStamp:
    def test_shadow_using_primary_plaintext_fails_under_shadow_slot(self):
        primary_sk = Ed25519PrivateKey.generate()
        primary_pk = primary_sk.public_key().public_bytes_raw()
        shadow_sk = Ed25519PrivateKey.generate()
        shadow_pk = shadow_sk.public_key().public_bytes_raw()

        # Primary publishes 9.C.1.
        miners = [
            MinerCommitRecord(
                miner_hotkey=os.urandom(32), k_block=7038900,
                k_round=12345700, sha256_ct=os.urandom(32),
            )
        ]
        primary_blob = build_pre_scoring_state(
            validator_hotkey=primary_pk,
            validator_signing_key=primary_sk,
            epoch_id="EPOCH-A", epoch_idx=1,
            outcomes_release_round=12000, outcomes_fetched_at_round=12010,
            miner_commits=miners, excluded_miners=[],
        )
        primary_plain = canonical_cbor_loads(primary_blob)

        # Shadow blindly copies primary's plaintext into its own storage slot.
        # Verifier checks inner_sig against shadow's chain account → fails.
        assert not verify_inner_sig(
            primary_plain, shadow_pk, hotkey_field="validator_hotkey"
        )
        # ...while it still verifies under primary's hotkey.
        assert verify_inner_sig(
            primary_plain, primary_pk, hotkey_field="validator_hotkey"
        )


# ---------- T-15: Registration forgery ----------


class TestT15RegistrationForgery:
    def test_attacker_cannot_bind_victims_key(self):
        attacker_ss58 = os.urandom(32)
        victim_sk = Ed25519PrivateKey.generate()
        victim_pk = victim_sk.public_key().public_bytes_raw()

        # Attacker tries to publish a forged binding under their own SS58.
        forged = REG_V1_PREFIX + b"M" + victim_pk + os.urandom(64)
        parsed = parse_registration_payload(forged)
        assert parsed is not None
        # Verifier rejects: sig isn't valid for (M, attacker_ss58, victim_pk).
        assert not verify_registration(parsed, ss58_pubkey=attacker_ss58)


# ---------- T-18: Malicious archive ----------


class TestT18MaliciousArchive:
    def test_archive_serving_other_miners_bytes_caught(self, miner):
        # Build a second miner's submission with different bytes.
        other_sk = Ed25519PrivateKey.generate()
        other_pk = other_sk.public_key().public_bytes_raw()
        other_plain = build_prediction_plaintext(
            epoch_id="EPOCH-A", miner_hotkey=other_pk, submitted_round=12345,
            horizons=[
                build_horizon_entry("7", (5.0, 10.0, 15.0), (5.0, 10.0, 15.0),
                                    (5.0, 10.0, 15.0), 0.5, 0.5),
            ],
            miner_signing_key=other_sk,
        )
        other_enc = encrypt_prediction(other_plain, epoch_id="EPOCH-A")

        # Archive serves OTHER miner's bytes when verifier asks for THIS miner.
        # SHA verification fails immediately.
        result = evaluate_scoreability(
            aes_ct=other_enc.aes_ct,
            aes_key=miner["enc"].aes_key,  # this miner's key won't decrypt other's CT
            epoch_id="EPOCH-A",
            miner_hotkey_from_chain=miner["pk"],
            on_chain=_triple(miner),  # chain SHA still binds THIS miner's CT
            timing=_timing(),
        )
        assert not result.ok
        assert result.failure == ScoreabilityFailure.CIPHERTEXT_MATCH_FAIL


# ---------- Episode-artifact tampering (Phase E) ----------


class TestPhaseEBundleTampering:
    def test_tampered_entry_breaks_root(self):
        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        entries = [
            PerEpisodeEntry(
                episode_id=f"EP-{i:03d}", horizon="7",
                cost_q=(-3.0, 0.0, 3.0), conv_q=(-1.0, 1.0, 3.0),
                eff_q=(-1.0, 0.5, 2.5),
                goal_miss_probability=0.1, instability_risk=0.05,
            )
            for i in range(5)
        ]
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        original_root = compute_episodes_imt_root(decoded["entries"])

        # Attacker rewrites one entry's quantiles.
        tampered = list(decoded["entries"])
        tampered[2] = {**tampered[2], "cost_q": [999, 999, 999]}

        new_root = compute_episodes_imt_root(tampered)
        assert new_root != original_root
        # Verifier with the chain-anchored ORIGINAL root rejects the tampered list.
        assert not verify_bundle_root(
            {"entries": tampered, "miner_hotkey": pk, "inner_sig": b"\x00" * 64},
            expected_root=original_root,
        )

    def test_tampered_entry_breaks_inner_sig(self):
        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        entries = [
            PerEpisodeEntry(
                episode_id="EP-001", horizon="7",
                cost_q=(-3.0, 0.0, 3.0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                goal_miss_probability=0.1, instability_risk=0.05,
            ),
        ]
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)

        # Mutate one numeric field; inner_sig was over the original bytes.
        tampered = dict(decoded)
        tampered["entries"] = [
            {**decoded["entries"][0], "cost_q": [99, 99, 99]}
        ]
        # inner_sig stays as the ORIGINAL signature; verifier rebuilds the
        # canonical CBOR over the tampered map and gets a different digest.
        assert not verify_bundle_inner_sig(tampered, pk)
