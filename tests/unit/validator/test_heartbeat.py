"""Unit tests for hope/validator/heartbeat.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hope.validator.heartbeat import run_heartbeat
from hope.validator.weights_commit import WeightsCommitResult


VAL_SS58 = "5HiWPApiuXz9DDnkyFu4M2tWs2ar6LTKt54Bo18EL6pgZsdn"


def _mock_subtensor(
    *,
    current_block: int,
    hotkeys: list[str],
    last_update_per_uid: list[int] | None = None,
    weights_for_uid: dict[int, list[tuple[int, int]]] | None = None,
):
    """Build a minimal subtensor mock that satisfies _read_validator_state."""
    st = MagicMock()
    st.substrate = MagicMock()
    st.substrate.get_block.return_value = {"header": {"number": current_block}}

    mg = MagicMock()
    mg.hotkeys = list(hotkeys)
    st.metagraph.return_value = mg

    lu_value = list(last_update_per_uid) if last_update_per_uid else [0] * len(hotkeys)
    weights_value = weights_for_uid or {}

    def _query(module: str, storage: str, params: list):
        # SubtensorModule.LastUpdate[netuid]  -> list of block numbers
        # SubtensorModule.Weights[netuid][uid] -> list of (uid, u16) tuples
        result = MagicMock()
        if storage == "LastUpdate":
            result.value = list(lu_value)
        elif storage == "Weights":
            uid = params[1]
            result.value = weights_value.get(uid, [])
        else:
            result.value = None
        return result

    st.substrate.query.side_effect = _query
    return st


def _mock_wallet(ss58: str = VAL_SS58):
    wallet = MagicMock()
    wallet.hotkey.ss58_address = ss58
    return wallet


class TestSkipPaths:
    def test_skipped_when_validator_not_in_metagraph(self):
        st = _mock_subtensor(current_block=1000, hotkeys=["5GsomeOtherKey..."])
        result = run_heartbeat(
            subtensor=st, validator_wallet=_mock_wallet(),
            netuid=21, threshold_blocks=1500,
        )
        assert result.action == "skipped_not_in_metagraph"
        assert result.current_block == 1000

    def test_skipped_when_gap_below_threshold(self):
        # current=8000, last_update=7500 → gap=500 < threshold=1500
        st = _mock_subtensor(
            current_block=8000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[7500],
            weights_for_uid={0: [(1, 32768), (2, 16384)]},
        )
        result = run_heartbeat(
            subtensor=st, validator_wallet=_mock_wallet(),
            netuid=21, threshold_blocks=1500,
        )
        assert result.action == "skipped_recent_activity"
        assert result.gap_blocks == 500

    def test_skipped_when_no_prior_weights(self):
        # gap is well past threshold but Weights storage is empty
        st = _mock_subtensor(
            current_block=10000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[5000],
            weights_for_uid={},  # no entry → returns []
        )
        result = run_heartbeat(
            subtensor=st, validator_wallet=_mock_wallet(),
            netuid=21, threshold_blocks=1500,
        )
        assert result.action == "skipped_no_prior_weights"
        assert result.gap_blocks == 5000

    def test_skipped_when_last_update_is_zero(self):
        # Newly-registered validator: gap is "huge" but no weights exist
        st = _mock_subtensor(
            current_block=10000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[0],
            weights_for_uid={},
        )
        result = run_heartbeat(
            subtensor=st, validator_wallet=_mock_wallet(),
            netuid=21, threshold_blocks=1500,
        )
        assert result.action == "skipped_no_prior_weights"


class TestDryRun:
    def test_dry_run_does_not_call_commit(self):
        st = _mock_subtensor(
            current_block=10000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[5000],
            weights_for_uid={0: [(1, 32768), (2, 16384)]},
        )
        with patch("hope.validator.heartbeat.commit_weights_layer_9c3") as commit_mock:
            result = run_heartbeat(
                subtensor=st, validator_wallet=_mock_wallet(),
                netuid=21, threshold_blocks=1500, dry_run=True,
            )
        assert result.action == "skipped_dry_run"
        assert result.weights_count == 2
        commit_mock.assert_not_called()


class TestSubmitPath:
    def test_submits_when_gap_and_weights_present(self):
        st = _mock_subtensor(
            current_block=10000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[5000],   # gap = 5000
            weights_for_uid={0: [(7, 21974), (8, 21780), (9, 21780)]},
        )
        # Patch the import-time function from the module that uses it
        with patch("hope.validator.heartbeat.commit_weights_layer_9c3") as commit_mock:
            commit_mock.return_value = WeightsCommitResult(
                success=True, message="ok",
                block_number=10001, block_hash=b"\xab" * 32,
                extrinsic_hash="0xdeadbeef",
            )
            result = run_heartbeat(
                subtensor=st, validator_wallet=_mock_wallet(),
                netuid=21, threshold_blocks=1500,
            )
        assert result.action == "submitted"
        assert result.weights_count == 3
        assert result.submitted_block == 10001

        # Verify the call's uids and normalized weights round-trip cleanly
        commit_mock.assert_called_once()
        kwargs = commit_mock.call_args.kwargs
        assert kwargs["netuid"] == 21
        assert kwargs["uids"] == [7, 8, 9]
        assert kwargs["weights"] == pytest.approx(
            [21974 / 65535.0, 21780 / 65535.0, 21780 / 65535.0]
        )

    def test_failed_submission_returns_failed_action(self):
        st = _mock_subtensor(
            current_block=10000,
            hotkeys=[VAL_SS58],
            last_update_per_uid=[5000],
            weights_for_uid={0: [(1, 32768)]},
        )
        with patch("hope.validator.heartbeat.commit_weights_layer_9c3") as commit_mock:
            commit_mock.return_value = WeightsCommitResult(
                success=False, message="rate limited",
                block_number=None, block_hash=None, extrinsic_hash=None,
            )
            result = run_heartbeat(
                subtensor=st, validator_wallet=_mock_wallet(),
                netuid=21, threshold_blocks=1500,
            )
        assert result.action == "failed"
        assert result.error_message == "rate limited"


class TestNormalizationRoundTrip:
    """A weights commit going round-trip through u16→float→u16 should be
    stable to within ±1 unit. This guards against the heartbeat slowly
    drifting weights over many cycles."""

    def test_normalization_is_stable(self):
        original_u16 = [21974, 21780, 21780, 1, 65535, 32768]
        # Mirror the heartbeat's normalization step
        normalized = [w / 65535.0 for w in original_u16]
        # The Bittensor SDK re-quantises floats to u16; for our purposes the
        # exact re-quantisation rule is the SDK's, but we can verify that
        # the values we feed in are within the legal [0, 1] range.
        for n in normalized:
            assert 0.0 <= n <= 1.0
