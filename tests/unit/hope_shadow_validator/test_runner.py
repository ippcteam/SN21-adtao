"""Tests for hope/hope_shadow_validator/runner.py + SH-5 rubber-stamp defence."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import (
    ArchiveEndpoint,
    FetchAggregate,
    FetchResult,
)
from hope.commitment.canonical import canonical_cbor_loads
from hope.commitment.inner_sig import verify_inner_sig
from hope.commitment.on_chain import CommitResult
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.scoreability import TimingBounds
from hope.hope_shadow_validator.runner import run_shadow_epoch
from hope.validator.onchain_runner import MinerOnChainInputs
from hope.validator.weights_commit import WeightsCommitResult


def _ok_commit(block: int, reveal_round: int | None = None) -> CommitResult:
    return CommitResult(
        success=True, message="OK", block_number=block,
        extrinsic_hash="0x" + "ab" * 32, reveal_round=reveal_round,
    )


def _make_miner(epoch_id: str, uid: int, block: int):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
    ]
    plain = build_prediction_plaintext(
        epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id=epoch_id)
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    return MinerOnChainInputs(
        miner_uid=uid,
        miner_hotkey=pk,
        revealed_k=enc.aes_key,
        sha256_ct_commit=sha_ct,
        self_archive_url="https://m/x",
        chain_block_at_k_commit=block,
        k_reveal_round=12345700 + uid,
    ), enc.aes_ct


class _Archive:
    def __init__(self):
        self._store = {}
    def store(self, sha, ct):
        self._store[sha] = ct
    def fetch_first_match(self, endpoints, *, epoch_id, miner_identity, expected_sha256):
        ct = self._store.get(expected_sha256)
        if ct is None:
            return FetchAggregate(aes_ct=None, winner=None, attempts=[])
        winner = FetchResult(
            endpoint=endpoints[0], ok=True, aes_ct=ct, sha256_match=True,
            status_code=200, elapsed_ms=1,
        )
        return FetchAggregate(aes_ct=ct, winner=winner, attempts=[winner])


@pytest.fixture
def setup():
    primary_sk = Ed25519PrivateKey.generate()
    primary_pk = primary_sk.public_key().public_bytes_raw()
    shadow_sk = Ed25519PrivateKey.generate()
    shadow_pk = shadow_sk.public_key().public_bytes_raw()

    archive = _Archive()
    inputs = []
    for i in range(3):
        inp, ct = _make_miner("EPOCH-A", uid=i, block=7038900 + i)
        archive.store(inp.sha256_ct_commit, ct)
        inputs.append(inp)

    return {
        "primary_sk": primary_sk, "primary_pk": primary_pk,
        "shadow_sk": shadow_sk, "shadow_pk": shadow_pk,
        "archive": archive, "inputs": inputs,
        "endpoints": [ArchiveEndpoint(tier=2, base_url="https://hope")],
        "timing": TimingBounds(
            epoch_open_round=12000, miner_deadline_round=13000,
            chain_window_min_block=7038000, chain_window_max_block=7039000,
        ),
    }


def _scorer_fixed(value):
    def fn(epoch_id, plaintexts):
        return {hk: value for hk in plaintexts}
    return fn


class TestShadowRunsLikePrimary:
    def test_shadow_emits_independent_commits(self, setup):
        captured = {}

        def capture_pre(**kwargs):
            captured["pre_blob"] = kwargs["pre_scoring_state_cbor"]
            return _ok_commit(7038910)

        def capture_post(**kwargs):
            captured["post_blob"] = kwargs["post_scoring_artifacts_cbor"]
            return _ok_commit(7038930)

        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                side_effect=capture_pre,
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=True, message="OK",
                    block_number=7038920, block_hash=os.urandom(32),
                    extrinsic_hash="0x" + "cd" * 32,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                side_effect=capture_post,
            ),
        ):
            outcome = run_shadow_epoch(
                subtensor=object(),
                shadow_wallet=object(),
                shadow_hotkey=setup["shadow_pk"],
                shadow_signing_key=setup["shadow_sk"],
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert outcome.ok
        # The shadow's 9.C.1 / 9.C.2 carry SHADOW's hotkey, NOT primary's.
        pre = canonical_cbor_loads(captured["pre_blob"])
        post = canonical_cbor_loads(captured["post_blob"])
        assert pre["validator_hotkey"] == setup["shadow_pk"]
        assert post["validator_hotkey"] == setup["shadow_pk"]
        assert verify_inner_sig(pre, setup["shadow_pk"], hotkey_field="validator_hotkey")
        assert verify_inner_sig(post, setup["shadow_pk"], hotkey_field="validator_hotkey")


class TestRubberStampDefenceSH5:
    """If a shadow blindly copies primary's plaintext bytes into its own
    storage slot, an auditor MUST be able to detect it.

    The chain locks storage at (netuid, account=shadow_hotkey). The
    plaintext.validator_hotkey field is what was signed and committed.
    A rubber-stamping shadow would either:
      (a) copy primary's plaintext → plaintext.validator_hotkey == primary_pk;
          but then verify_inner_sig(plaintext, shadow_pk) FAILS because
          the field doesn't match the storage account.
      (b) re-sign primary's content with shadow's key and update the
          validator_hotkey field — but this is no longer "rubber-stamping",
          it's an independent commit (the shadow ran its own code path).

    This test simulates (a) by manually emitting a primary plaintext under
    shadow's chain storage account and asserts the verifier rejects it.
    """

    def test_primary_plaintext_under_shadow_slot_fails_verification(self, setup):
        # Simulate primary's pre-scoring state (signed by primary).
        from hope.commitment.scoring_state import (
            MinerCommitRecord,
            build_pre_scoring_state,
        )
        primary_pre = build_pre_scoring_state(
            validator_hotkey=setup["primary_pk"],
            validator_signing_key=setup["primary_sk"],
            epoch_id="EPOCH-A",
            epoch_idx=42,
            outcomes_release_round=12000,
            outcomes_fetched_at_round=12010,
            miner_commits=[
                MinerCommitRecord(
                    miner_hotkey=inp.miner_hotkey,
                    k_block=inp.chain_block_at_k_commit or 0,
                    k_round=inp.k_reveal_round,
                    sha256_ct=inp.sha256_ct_commit,
                )
                for inp in setup["inputs"]
            ],
            excluded_miners=[],
        )
        primary_decoded = canonical_cbor_loads(primary_pre)

        # If a shadow rubber-stamped this byte-for-byte under its chain account,
        # the verifier checks `verify_inner_sig(plaintext, shadow_hotkey)` per
        # the shadow's chain storage account. That MUST fail because the
        # field inside is primary's hotkey, not the shadow's.
        assert not verify_inner_sig(
            primary_decoded, setup["shadow_pk"], hotkey_field="validator_hotkey"
        )

        # And it still verifies for the actual primary, of course.
        assert verify_inner_sig(
            primary_decoded, setup["primary_pk"], hotkey_field="validator_hotkey"
        )
