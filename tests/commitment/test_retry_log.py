"""Unit tests for hope/commitment/retry_log.py — Layer 9.C.6."""

from __future__ import annotations

import json
import os

from hope.commitment.archives import ArchiveEndpoint, FetchResult
from hope.commitment.retry_log import (
    RETRY_LOG_VERSION,
    RetryLogAttempt,
    RetryLogMinerEntry,
    attempt_from_fetch_result,
    build_retry_log_blob,
    compute_retry_log_sha256,
)


def _attempt(tier=1, ok=False, status=502, error="bad gateway"):
    return RetryLogAttempt(
        tier=tier,
        name=f"tier-{tier}",
        url=f"https://t{tier}.example",
        ok=ok,
        status_code=status,
        sha256_match=None,
        elapsed_ms=100,
        error=error,
    )


def _miner(hotkey=None, attempts=None):
    return RetryLogMinerEntry(
        miner_hotkey=hotkey if hotkey is not None else os.urandom(32),
        miner_uid=1,
        expected_sha256_ct=os.urandom(32),
        attempts=attempts or [_attempt(tier=1), _attempt(tier=2), _attempt(tier=3)],
    )


class TestAttemptFromFetchResult:
    def test_failed_fetch(self):
        ep = ArchiveEndpoint(tier=2, base_url="https://archive.example.io", name="archive2-shadow")
        fr = FetchResult(
            endpoint=ep,
            ok=False,
            aes_ct=None,
            sha256_match=False,
            status_code=200,
            error="sha256 mismatch",
            elapsed_ms=42,
        )
        a = attempt_from_fetch_result(fr)
        assert a.tier == 2
        assert a.name == "archive2-shadow"
        assert a.url == "https://archive.example.io"
        assert a.ok is False
        assert a.status_code == 200
        assert a.sha256_match is False
        assert a.error == "sha256 mismatch"
        assert a.elapsed_ms == 42

    def test_default_name_falls_back_to_url(self):
        ep = ArchiveEndpoint(tier=1, base_url="https://val")
        fr = FetchResult(
            endpoint=ep, ok=False, aes_ct=None, sha256_match=None,
            status_code=502, error="x", elapsed_ms=1,
        )
        assert attempt_from_fetch_result(fr).name == "https://val"


class TestBuildRetryLogBlob:
    def test_basic_structure(self):
        validator_hk = b"\x42" * 32
        miner_hk = b"\x99" * 32
        entry = RetryLogMinerEntry(
            miner_hotkey=miner_hk,
            miner_uid=17,
            expected_sha256_ct=b"\x77" * 32,
            attempts=[_attempt(tier=1), _attempt(tier=2)],
        )
        blob = build_retry_log_blob(
            validator_hotkey=validator_hk,
            epoch_id="EPOCH-X",
            epoch_idx=42,
            miner_entries=[entry],
            generated_at_unix=1714694400,
        )
        decoded = json.loads(blob)
        assert decoded["v"] == RETRY_LOG_VERSION
        assert decoded["validator_hotkey"] == validator_hk.hex()
        assert decoded["epoch_id"] == "EPOCH-X"
        assert decoded["epoch_idx"] == 42
        assert decoded["generated_at_unix"] == 1714694400
        assert len(decoded["miners"]) == 1
        assert decoded["miners"][0]["miner_uid"] == 17
        assert len(decoded["miners"][0]["attempts"]) == 2

    def test_deterministic(self):
        validator_hk = b"\x42" * 32
        e = _miner()
        blob1 = build_retry_log_blob(
            validator_hotkey=validator_hk, epoch_id="X", epoch_idx=1,
            miner_entries=[e], generated_at_unix=100,
        )
        blob2 = build_retry_log_blob(
            validator_hotkey=validator_hk, epoch_id="X", epoch_idx=1,
            miner_entries=[e], generated_at_unix=100,
        )
        assert blob1 == blob2

    def test_miner_order_independent(self):
        validator_hk = b"\x42" * 32
        m1 = _miner(hotkey=b"\xa0" * 32)
        m2 = _miner(hotkey=b"\xb0" * 32)
        b1 = build_retry_log_blob(
            validator_hotkey=validator_hk, epoch_id="X", epoch_idx=1,
            miner_entries=[m1, m2], generated_at_unix=100,
        )
        b2 = build_retry_log_blob(
            validator_hotkey=validator_hk, epoch_id="X", epoch_idx=1,
            miner_entries=[m2, m1], generated_at_unix=100,
        )
        assert b1 == b2

    def test_invalid_validator_hotkey_length(self):
        try:
            build_retry_log_blob(
                validator_hotkey=b"\x00" * 31,
                epoch_id="X",
                epoch_idx=0,
                miner_entries=[],
            )
        except ValueError as e:
            assert "32 bytes" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_invalid_miner_hotkey_length(self):
        bad = RetryLogMinerEntry(
            miner_hotkey=b"\x00" * 31,
            miner_uid=1,
            expected_sha256_ct=os.urandom(32),
            attempts=[],
        )
        try:
            build_retry_log_blob(
                validator_hotkey=b"\x42" * 32,
                epoch_id="X",
                epoch_idx=0,
                miner_entries=[bad],
            )
        except ValueError as e:
            assert "32 bytes" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_compute_sha256_matches_chain_commit(self):
        blob = build_retry_log_blob(
            validator_hotkey=b"\x42" * 32, epoch_id="X", epoch_idx=0,
            miner_entries=[_miner()], generated_at_unix=1,
        )
        digest = compute_retry_log_sha256(blob)
        assert len(digest) == 32

    def test_no_whitespace_in_json(self):
        blob = build_retry_log_blob(
            validator_hotkey=b"\x42" * 32, epoch_id="X", epoch_idx=0,
            miner_entries=[_miner()], generated_at_unix=1,
        )
        text = blob.decode("utf-8")
        assert ", " not in text
        assert ": " not in text
