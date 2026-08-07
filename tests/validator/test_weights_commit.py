"""Unit tests for hope/validator/weights_commit.py — Layer 9.C.3."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hope.validator.weights_commit import (
    commit_weights_layer_9c3,
    estimate_weights_reveal_round,
)


@dataclass
class FakeReceipt:
    block_number: int
    extrinsic_hash: bytes


@dataclass
class FakeResponse:
    success: bool
    message: str
    extrinsic_receipt: FakeReceipt | None


class FakeSubtensor:
    def __init__(self, success=True, block=7038900, block_hash="0x" + "ab" * 32):
        self._success = success
        self._block = block
        self._block_hash = block_hash
        self.last_call = None
        self.substrate = self  # so _resolve_block_hash works

    def set_weights(self, **kwargs):
        self.last_call = kwargs
        if not self._success:
            return FakeResponse(success=False, message="rejected", extrinsic_receipt=None)
        return FakeResponse(
            success=True,
            message="OK",
            extrinsic_receipt=FakeReceipt(
                block_number=self._block,
                extrinsic_hash=b"\xcd" * 32,
            ),
        )

    def get_block_hash(self, block_number):
        return self._block_hash


@pytest.fixture
def wallet():
    return object()


class TestCommitWeights:
    def test_success(self, wallet):
        st = FakeSubtensor()
        res = commit_weights_layer_9c3(
            subtensor=st,
            validator_wallet=wallet,
            netuid=21,
            uids=[1, 2, 3],
            weights=[0.5, 0.3, 0.2],
        )
        assert res.success
        assert res.block_number == 7038900
        assert res.block_hash == bytes.fromhex("ab" * 32)
        assert res.extrinsic_hash == "0x" + "cd" * 32
        # Verify args forwarded
        assert st.last_call["netuid"] == 21
        assert st.last_call["uids"] == [1, 2, 3]
        assert st.last_call["weights"] == [0.5, 0.3, 0.2]
        assert st.last_call["commit_reveal_version"] == 4

    def test_failure(self, wallet):
        st = FakeSubtensor(success=False)
        res = commit_weights_layer_9c3(
            subtensor=st,
            validator_wallet=wallet,
            netuid=21,
            uids=[1, 2],
            weights=[0.6, 0.4],
        )
        assert not res.success
        assert res.block_number is None
        assert res.block_hash is None

    def test_uids_weights_length_mismatch(self, wallet):
        st = FakeSubtensor()
        with pytest.raises(ValueError, match="length mismatch"):
            commit_weights_layer_9c3(
                subtensor=st,
                validator_wallet=wallet,
                netuid=21,
                uids=[1, 2, 3],
                weights=[0.5, 0.5],
            )

    def test_empty_uids_rejected(self, wallet):
        st = FakeSubtensor()
        with pytest.raises(ValueError, match="non-empty"):
            commit_weights_layer_9c3(
                subtensor=st,
                validator_wallet=wallet,
                netuid=21,
                uids=[],
                weights=[],
            )

    def test_block_hash_none_when_substrate_missing(self, wallet):
        @dataclass
        class StNoSub:
            substrate = None
            def set_weights(self, **kwargs):
                return FakeResponse(
                    success=True, message="OK",
                    extrinsic_receipt=FakeReceipt(7000000, b"\x00" * 32),
                )

        st = StNoSub()
        res = commit_weights_layer_9c3(
            subtensor=st,
            validator_wallet=wallet,
            netuid=21,
            uids=[1],
            weights=[1.0],
        )
        assert res.success
        assert res.block_number == 7000000
        assert res.block_hash is None


class TestEstimateRevealRound:
    def test_basic(self):
        # 360 blocks × 12s/block = 4320s; / 3s/round = 1440 rounds
        r = estimate_weights_reveal_round(current_round=1000, blocks_until_reveal=360)
        assert r == 1000 + 1440

    def test_rounds_up(self):
        # 1 block × 12s = 12s; ceil(12/3) = 4 rounds
        r = estimate_weights_reveal_round(current_round=0, blocks_until_reveal=1)
        assert r == 4

    def test_non_default_period(self):
        r = estimate_weights_reveal_round(
            current_round=0, blocks_until_reveal=10,
            block_time_secs=12.0, drand_period_secs=4.0,
        )
        # 10 × 12 = 120; / 4 = 30
        assert r == 30

# ---- merged from the duplicate tests/unit tree (2026-08-07) ----
# These lived ONLY in tests/unit/validator/test_weights_commit.py. Both trees were collected by
# pytest, so they ran — but a behaviour change updating one copy would
# leave the other asserting stale behaviour. One tree now.


@dataclass
class _HK:
    ss58_address: str


class _WalletWithHotkey:
    def __init__(self, ss58: str = "5Fake"):
        self.hotkey = _HK(ss58)


@dataclass
class _Query:
    value: object


class TestPostCommitVerification:
    """Bug fix: set_weights can return success=True even when the chain rejected
    the commit (WeightsSetRateLimit). LastUpdate not advancing => downgrade."""

    def test_downgrades_when_last_update_does_not_advance(self):
        # success=True with an EMPTY receipt (triggers the fallback block stamp),
        # but LastUpdate is stuck => the commit didn't land => report failure.
        class StRateLimited:
            def __init__(self):
                self.substrate = self
            def set_weights(self, **kwargs):
                return FakeResponse(success=True, message="OK", extrinsic_receipt=None)
            def get_current_block(self):
                return 8400000
            def get_block_hash(self, n):
                return "0x" + "ab" * 32
            def get_uid_for_hotkey_on_subnet(self, ss58, netuid):
                return 64
            def query(self, module, storage, params):
                # LastUpdate[netuid]: index 64 is constant => no advance
                return _Query([0] * 64 + [1000])

        res = commit_weights_layer_9c3(
            subtensor=StRateLimited(),
            validator_wallet=_WalletWithHotkey(),
            netuid=21,
            uids=[1, 2],
            weights=[0.5, 0.5],
        )
        assert res.success is False
        assert "not_applied" in res.message
        assert res.block_hash is None
        assert res.block_number is None

    def test_keeps_success_when_last_update_advances(self):
        class StApplied:
            def __init__(self):
                self._calls = 0
                self.substrate = self
            def set_weights(self, **kwargs):
                return FakeResponse(success=True, message="OK", extrinsic_receipt=None)
            def get_current_block(self):
                return 8400050
            def get_block_hash(self, n):
                return "0x" + "ab" * 32
            def get_uid_for_hotkey_on_subnet(self, ss58, netuid):
                return 64
            def query(self, module, storage, params):
                self._calls += 1
                val = 1000 if self._calls == 1 else 1050  # advanced post-commit
                return _Query([0] * 64 + [val])

        res = commit_weights_layer_9c3(
            subtensor=StApplied(),
            validator_wallet=_WalletWithHotkey(),
            netuid=21,
            uids=[1, 2],
            weights=[0.5, 0.5],
        )
        assert res.success is True
        assert res.block_hash == bytes.fromhex("ab" * 32)

    def test_verify_disabled_keeps_success(self):
        # verify_applied=False => skip the LastUpdate check entirely.
        class StRateLimited:
            def __init__(self):
                self.substrate = self
            def set_weights(self, **kwargs):
                return FakeResponse(success=True, message="OK", extrinsic_receipt=None)
            def get_current_block(self):
                return 8400000
            def get_block_hash(self, n):
                return "0x" + "ab" * 32
            def get_uid_for_hotkey_on_subnet(self, ss58, netuid):
                return 64
            def query(self, module, storage, params):
                return _Query([0] * 64 + [1000])

        res = commit_weights_layer_9c3(
            subtensor=StRateLimited(),
            validator_wallet=_WalletWithHotkey(),
            netuid=21,
            uids=[1],
            weights=[1.0],
            verify_applied=False,
        )
        assert res.success is True
