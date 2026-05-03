"""Tests for hope.commitment.on_chain.

These are pure-Python unit tests that mock the Bittensor SDK. End-to-end
chain interaction is covered by Phase 0 testnet diagnostic scripts in
`scripts/phase0/`.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from hope.commitment.on_chain import (
    DEFAULT_REVEAL_SAFETY_ROUNDS,
    MAX_TLE_PLAINTEXT_BYTES,
    CommitResult,
    compute_blake2b_256,
    compute_sha256,
    read_revealed_commitments,
    submit_miner_prediction_layer_9b,
    submit_outcome_reveal_hash_layer_9a2,
    submit_post_scoring_artifacts_layer_9c2,
    submit_pre_scoring_state_layer_9c1,
    submit_release_commit_layer_9a1,
    submit_retry_log_attestation_layer_9c6,
    submit_sha256_commit,
    submit_timelock_commit,
)


def _make_mock_response(success: bool = True, message: str = "Success", block: int = 7038800):
    """Construct a mock ExtrinsicResponse-like object."""
    receipt = MagicMock()
    receipt.block_number = block
    receipt.extrinsic_hash = b"\xab\xcd" * 16  # 32 bytes

    response = MagicMock()
    response.success = success
    response.message = message
    response.extrinsic_receipt = receipt
    return response


class TestConstants:
    def test_max_tle_plaintext_bytes(self):
        # 1024 chain limit - 256 TLE overhead = 768
        assert MAX_TLE_PLAINTEXT_BYTES == 768

    def test_default_reveal_safety_rounds(self):
        assert DEFAULT_REVEAL_SAFETY_ROUNDS == 20


class TestHashHelpers:
    def test_blake2b_256_length(self):
        h = compute_blake2b_256(b"hello")
        assert len(h) == 32

    def test_blake2b_256_deterministic(self):
        h1 = compute_blake2b_256(b"hello")
        h2 = compute_blake2b_256(b"hello")
        assert h1 == h2

    def test_blake2b_256_changes_with_input(self):
        h1 = compute_blake2b_256(b"hello")
        h2 = compute_blake2b_256(b"hello!")
        assert h1 != h2

    def test_sha256_length(self):
        h = compute_sha256(b"hello")
        assert len(h) == 32

    def test_sha256_known_vector(self):
        # SHA-256 of empty bytes
        h = compute_sha256(b"")
        assert h.hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestSubmitSha256Commit:
    def test_validates_hash_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            submit_sha256_commit(
                subtensor=MagicMock(),
                wallet=MagicMock(),
                netuid=466,
                hash_bytes=b"\x00" * 31,
            )

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    def test_calls_publish_with_sha256_data_type(self, mock_publish):
        mock_publish.return_value = _make_mock_response()
        hash_bytes = b"\xa1" * 32
        result = submit_sha256_commit(
            subtensor=MagicMock(),
            wallet=MagicMock(),
            netuid=466,
            hash_bytes=hash_bytes,
        )
        # publish_metadata_extrinsic was called with data_type='Sha256'
        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        assert kwargs["data_type"] == "Sha256"
        assert kwargs["data"] == hash_bytes
        assert kwargs["netuid"] == 466
        assert result.success is True

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    def test_returns_commit_result(self, mock_publish):
        mock_publish.return_value = _make_mock_response(success=True, block=12345)
        result = submit_sha256_commit(
            subtensor=MagicMock(), wallet=MagicMock(), netuid=466, hash_bytes=b"\xa1" * 32
        )
        assert isinstance(result, CommitResult)
        assert result.success is True
        assert result.block_number == 12345
        assert result.reveal_round is None  # plain commits have no reveal_round


class TestSubmitTimelockCommit:
    def test_rejects_oversized_plaintext(self):
        with pytest.raises(ValueError, match="too large"):
            submit_timelock_commit(
                subtensor=MagicMock(),
                wallet=MagicMock(),
                netuid=466,
                plaintext=b"x" * (MAX_TLE_PLAINTEXT_BYTES + 1),
                blocks_until_reveal=20,
            )

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    @patch("hope.commitment.on_chain.bittensor_drand")
    def test_calls_drand_encrypt_then_publish(self, mock_drand, mock_publish):
        mock_drand.encrypt.return_value = (b"encrypted_blob", 28326276)
        mock_publish.return_value = _make_mock_response()

        plaintext = b"hello world" * 10
        result = submit_timelock_commit(
            subtensor=MagicMock(),
            wallet=MagicMock(),
            netuid=466,
            plaintext=plaintext,
            blocks_until_reveal=20,
        )

        mock_drand.encrypt.assert_called_once_with(plaintext, 20, 12.0)
        kwargs = mock_publish.call_args.kwargs
        assert kwargs["data_type"] == "TimelockEncrypted"
        assert kwargs["data"] == {"encrypted": b"encrypted_blob", "reveal_round": 28326276}
        assert result.reveal_round == 28326276

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    @patch("hope.commitment.on_chain.bittensor_drand")
    def test_at_max_plaintext_size_succeeds(self, mock_drand, mock_publish):
        mock_drand.encrypt.return_value = (b"x" * 1000, 99)
        mock_publish.return_value = _make_mock_response()
        plaintext = b"y" * MAX_TLE_PLAINTEXT_BYTES
        result = submit_timelock_commit(
            subtensor=MagicMock(),
            wallet=MagicMock(),
            netuid=466,
            plaintext=plaintext,
            blocks_until_reveal=20,
        )
        assert result.success is True


class TestReadRevealedCommitments:
    def test_returns_empty_list_when_none(self):
        st = MagicMock()
        st.get_revealed_commitment_by_hotkey.return_value = None
        result = read_revealed_commitments(st, netuid=466, hotkey_ss58="5...")
        assert result == []

    def test_parses_hex_payload(self):
        st = MagicMock()
        # Mock returns ((block, hex_str), ...) per SDK signature
        st.get_revealed_commitment_by_hotkey.return_value = (
            (12345, "deadbeef"),
            (12346, "0xcafebabe"),
        )
        result = read_revealed_commitments(st, netuid=466, hotkey_ss58="5...")
        assert len(result) == 2
        assert result[0].block_number == 12345
        assert result[0].plaintext == bytes.fromhex("deadbeef")
        assert result[1].block_number == 12346
        assert result[1].plaintext == bytes.fromhex("cafebabe")

    def test_parses_bytes_payload(self):
        st = MagicMock()
        st.get_revealed_commitment_by_hotkey.return_value = ((100, b"\x01\x02\x03"),)
        result = read_revealed_commitments(st, netuid=466, hotkey_ss58="5...")
        assert len(result) == 1
        assert result[0].plaintext == b"\x01\x02\x03"


class TestLayerSpecificHelpers:
    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    def test_layer_9a1_release_commit(self, mock_publish):
        mock_publish.return_value = _make_mock_response()
        digest = b"\xa1" * 32
        submit_release_commit_layer_9a1(
            subtensor=MagicMock(),
            hope_outcome_signer_wallet=MagicMock(),
            netuid=466,
            release_commit_digest=digest,
        )
        assert mock_publish.call_args.kwargs["data_type"] == "Sha256"
        assert mock_publish.call_args.kwargs["data"] == digest

    def test_layer_9a1_validates_digest_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            submit_release_commit_layer_9a1(
                subtensor=MagicMock(),
                hope_outcome_signer_wallet=MagicMock(),
                netuid=466,
                release_commit_digest=b"\x00" * 16,
            )

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    def test_layer_9a2_outcome_reveal_hash(self, mock_publish):
        mock_publish.return_value = _make_mock_response()
        sha = b"\xb2" * 32
        submit_outcome_reveal_hash_layer_9a2(
            subtensor=MagicMock(),
            hope_outcome_signer_wallet=MagicMock(),
            netuid=466,
            reveal_blob_sha256=sha,
        )
        assert mock_publish.call_args.kwargs["data"] == sha

    def test_layer_9b_validates_aes_key_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            submit_miner_prediction_layer_9b(
                subtensor=MagicMock(),
                miner_wallet=MagicMock(),
                netuid=466,
                aes_key=b"\x00" * 16,
                blocks_until_reveal=20,
            )

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    @patch("hope.commitment.on_chain.bittensor_drand")
    def test_layer_9b_uses_timelock(self, mock_drand, mock_publish):
        mock_drand.encrypt.return_value = (b"x" * 64, 999)
        mock_publish.return_value = _make_mock_response()
        result = submit_miner_prediction_layer_9b(
            subtensor=MagicMock(),
            miner_wallet=MagicMock(),
            netuid=466,
            aes_key=b"\xc3" * 32,
            blocks_until_reveal=20,
        )
        assert mock_publish.call_args.kwargs["data_type"] == "TimelockEncrypted"
        assert result.reveal_round == 999

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    @patch("hope.commitment.on_chain.bittensor_drand")
    def test_layer_9c1_pre_scoring(self, mock_drand, mock_publish):
        mock_drand.encrypt.return_value = (b"x" * 600, 88)
        mock_publish.return_value = _make_mock_response()
        cbor_bytes = b"\xa1" * 400  # within MAX_TLE_PLAINTEXT_BYTES
        result = submit_pre_scoring_state_layer_9c1(
            subtensor=MagicMock(),
            validator_wallet=MagicMock(),
            netuid=466,
            pre_scoring_state_cbor=cbor_bytes,
            blocks_until_reveal=120,
        )
        assert mock_publish.call_args.kwargs["data_type"] == "TimelockEncrypted"
        assert result.success is True

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    @patch("hope.commitment.on_chain.bittensor_drand")
    def test_layer_9c2_post_scoring(self, mock_drand, mock_publish):
        mock_drand.encrypt.return_value = (b"x" * 200, 88)
        mock_publish.return_value = _make_mock_response()
        result = submit_post_scoring_artifacts_layer_9c2(
            subtensor=MagicMock(),
            validator_wallet=MagicMock(),
            netuid=466,
            post_scoring_artifacts_cbor=b"\xb2" * 100,
            blocks_until_reveal=140,
        )
        assert mock_publish.call_args.kwargs["data_type"] == "TimelockEncrypted"
        assert result.reveal_round == 88

    def test_layer_9c6_validates_hash_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            submit_retry_log_attestation_layer_9c6(
                subtensor=MagicMock(),
                validator_wallet=MagicMock(),
                netuid=466,
                retry_log_blob_sha256=b"\x00" * 31,
            )

    @patch("hope.commitment.on_chain.publish_metadata_extrinsic")
    def test_layer_9c6_retry_log(self, mock_publish):
        mock_publish.return_value = _make_mock_response()
        sha = b"\xff" * 32
        submit_retry_log_attestation_layer_9c6(
            subtensor=MagicMock(),
            validator_wallet=MagicMock(),
            netuid=466,
            retry_log_blob_sha256=sha,
        )
        assert mock_publish.call_args.kwargs["data_type"] == "Sha256"
        assert mock_publish.call_args.kwargs["data"] == sha


class TestBudgetSizing:
    """Verify our 9.C.1 plaintext budget fits MAX_TLE_PLAINTEXT_BYTES."""

    def test_v07_revised_9c1_budget_fits(self):
        """Per phase 0 findings 2026-05-03, revised 9.C.1 budget: ~492 bytes binary CBOR."""
        # All hashes (10 × 32 bytes) + ints (4 × 8) + sig + headers + CBOR overhead
        estimated_bytes = (
            48        # header (v + epoch_id)
            + 32 * 7  # 7 different hash fields
            + 8 * 4   # 4 integer fields
            + 32      # validator_hotkey
            + 64      # inner_sig
            + 80      # CBOR overhead
        )
        # Total ≈ 472 bytes; fits in 768
        assert estimated_bytes < MAX_TLE_PLAINTEXT_BYTES, (
            f"9.C.1 budget {estimated_bytes} exceeds MAX_TLE_PLAINTEXT_BYTES={MAX_TLE_PLAINTEXT_BYTES}; "
            f"trim fields or split commit"
        )
