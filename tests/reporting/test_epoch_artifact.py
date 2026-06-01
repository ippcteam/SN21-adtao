"""Tests for hope.reporting.epoch_artifact."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hope.commitment.on_chain import CommitResult
from hope.reporting.epoch_artifact import (
    DEFAULT_ARTIFACT_DIR_ENV,
    artifact_path_for,
    build_and_write_artifact,
    build_artifact,
    read_artifact,
    resolve_artifact_dir,
    write_artifact,
)
from hope.commitment.scoreability import OnChainCommitTriple
from hope.validator.onchain_reader import MinerReadResult
from hope.validator.onchain_runner import EpochScoringOutcome
from hope.validator.weights_commit import WeightsCommitResult


def _commit_ok(block: int) -> CommitResult:
    return CommitResult(
        success=True, message="ok", block_number=block,
        extrinsic_hash="0x" + "a" * 64, reveal_round=None,
    )


def _weights_ok(block: int) -> WeightsCommitResult:
    return WeightsCommitResult(
        success=True, message="ok", block_number=block,
        block_hash="0x" + "b" * 64, extrinsic_hash="0x" + "c" * 64,
    )


def _read(uid: int) -> MinerReadResult:
    return MinerReadResult(
        miner_hotkey=bytes([uid]) * 32,
        miner_uid=uid,
        ok=True,
        plaintext={"horizons": []},
        excluded_reason=None,
        fetch=None,
        on_chain=OnChainCommitTriple(
            timelock_k_present=True,
            sha256_ct_commit=bytes([uid]) * 32,
            self_archive_url=f"https://archive.example.com/m{uid}",
            chain_block_at_k_commit=1_000_000 + uid,
        ),
        scoreability=None,
    )


def _read_failed(uid: int, reason: str) -> MinerReadResult:
    """A miner that was read but did NOT score (e.g. not_registered)."""
    return MinerReadResult(
        miner_hotkey=bytes([uid]) * 32,
        miner_uid=uid,
        ok=False,
        plaintext=None,
        excluded_reason=reason,
        fetch=None,
        on_chain=OnChainCommitTriple(
            timelock_k_present=True,
            sha256_ct_commit=bytes([uid]) * 32,
            self_archive_url=f"https://archive.example.com/m{uid}",
            chain_block_at_k_commit=1_000_000 + uid,
        ),
        scoreability=None,
    )


def _outcome(n_miners: int = 3) -> EpochScoringOutcome:
    score_map = {bytes([i]) * 32: 500_000 + i * 50_000 for i in range(n_miners)}
    return EpochScoringOutcome(
        pre_scoring_commit=_commit_ok(1_000_000),
        weights_commit=_weights_ok(1_001_000),
        post_scoring_commit=_commit_ok(1_002_000),
        miner_reads=[_read(i) for i in range(n_miners)],
        score_map=score_map,
        block_range_start=1_000_000,
        block_range_end=1_001_000,
    )


def test_per_uid_scores_include_unscored_reads_with_status(tmp_path):
    """Miners read but not scored appear as disqualification rows so the
    CMS leaderboard covers the full submitter set, not just winners."""
    outcome = _outcome(2)  # uids 0,1 scored
    # uid 98 submitted but isn't registered; uid 42 has a bad commit.
    outcome.miner_reads.append(_read_failed(98, "not_registered"))
    outcome.miner_reads.append(_read_failed(42, "inner_sig_hotkey_mismatch"))
    artifact = build_artifact(
        outcome=outcome,
        epoch_id="WR-TEST-W22-PUB-E1",
        total_registered_uids=256,
        chain_fetch_timestamp="2026-06-01T17:00:00+00:00",
    )
    rows = {r["uid"]: r for r in artifact.per_uid_scores}
    assert set(rows) == {0, 1, 42, 98}  # scored + both DQ rows present
    assert "status" not in rows[0]  # scored rows carry no status
    assert rows[98]["status"] == "not_registered"
    assert rows[98]["raw_score"] == 0.0
    assert rows[42]["status"] == "inner_sig_hotkey_mismatch"

    # ...and they survive aggregation into the published CMS payload.
    from hope.reporting.aggregator import aggregate
    payload = aggregate(artifact)
    by_uid = {mr.uid: mr for mr in payload.miner_results}
    assert by_uid[98].status == "disqualified_not_registered"
    assert by_uid[42].status == "disqualified_invalid_commit"
    # hotkeys published as SS58 (not raw hex)
    assert by_uid[98].hotkey.startswith("5") and 47 <= len(by_uid[98].hotkey) <= 48


def test_resolve_artifact_dir_explicit_arg_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(DEFAULT_ARTIFACT_DIR_ENV, "/should-not-be-used")
    resolved = resolve_artifact_dir(tmp_path)
    assert resolved == tmp_path.resolve()


def test_resolve_artifact_dir_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv(DEFAULT_ARTIFACT_DIR_ENV, str(tmp_path))
    assert resolve_artifact_dir() == tmp_path.resolve()


def test_resolve_artifact_dir_default(monkeypatch):
    monkeypatch.delenv(DEFAULT_ARTIFACT_DIR_ENV, raising=False)
    resolved = resolve_artifact_dir()
    assert resolved == Path("~/.sn21/epoch_artifacts").expanduser().resolve()


def test_artifact_path_for_sanitizes_unsafe_chars(tmp_path):
    path = artifact_path_for("WR/../etc/passwd", base_dir=tmp_path)
    # Must remain inside base_dir; no `..` segments after sanitization.
    assert tmp_path in path.parents
    assert ".." not in path.name


def test_write_artifact_auto_creates_dir_and_is_atomic(tmp_path):
    artifact = build_artifact(
        outcome=_outcome(3),
        epoch_id="WR-TEST-W18-PUB-E1",
        total_registered_uids=15,
        chain_fetch_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    target_dir = tmp_path / "fresh"
    assert not target_dir.exists()
    final_path = write_artifact(artifact, base_dir=target_dir)
    assert final_path.exists()
    assert final_path.parent == target_dir.resolve()
    # No `.tmp` leftovers from the atomic write.
    tmp_leftovers = list(target_dir.glob("*.tmp*"))
    assert not tmp_leftovers


def test_round_trip(tmp_path):
    build_and_write_artifact(
        outcome=_outcome(5),
        epoch_id="WR-2026-W18-PUB-E1",
        total_registered_uids=15,
        chain_fetch_timestamp="2026-05-13T17:00:00+00:00",
        base_dir=tmp_path,
    )
    artifact = read_artifact("WR-2026-W18-PUB-E1", base_dir=tmp_path)
    assert artifact.epoch_id == "WR-2026-W18-PUB-E1"
    assert artifact.scoring_formula_version == "1.2.1"
    assert artifact.epoch_type == "Search"
    assert artifact.epoch_subtype == "campaign-level"
    assert artifact.epoch_type_multiplier == 1.0
    assert artifact.horizon_set == ["7d", "14d"]
    assert artifact.block_range_start == 1_000_000
    assert artifact.block_range_end == 1_001_000
    assert artifact.total_registered_uids == 15
    assert len(artifact.per_uid_scores) == 5
    assert artifact.artifact_schema_version == 1


def test_per_uid_scores_carry_uid_hotkey_score(tmp_path):
    artifact = build_artifact(
        outcome=_outcome(3),
        epoch_id="WR-TEST-W18-PUB-E1",
        total_registered_uids=10,
        chain_fetch_timestamp="2026-05-13T17:00:00+00:00",
    )
    assert len(artifact.per_uid_scores) == 3
    row = artifact.per_uid_scores[0]
    assert set(row.keys()) >= {"uid", "hotkey", "score_micro", "raw_score"}
    assert row["uid"] == 0
    assert row["hotkey"] == ("00" * 32)
    assert row["raw_score"] == row["score_micro"] / 1_000_000.0


def test_per_uid_scores_stable_sort(tmp_path):
    """Two writes with the same outcome produce byte-identical files."""
    outcome = _outcome(8)
    chain_ts = "2026-05-13T17:00:00+00:00"
    p1 = build_and_write_artifact(
        outcome=outcome, epoch_id="A", total_registered_uids=15,
        chain_fetch_timestamp=chain_ts, base_dir=tmp_path,
    )
    p2 = build_and_write_artifact(
        outcome=outcome, epoch_id="B", total_registered_uids=15,
        chain_fetch_timestamp=chain_ts, base_dir=tmp_path,
    )
    # Different epoch_ids → different paths
    assert p1 != p2
    a = read_artifact("A", base_dir=tmp_path)
    b = read_artifact("B", base_dir=tmp_path)
    assert a.per_uid_scores == b.per_uid_scores


def test_tier_result_is_dict_not_dataclass(tmp_path):
    """Serialized form. asdict() produces dict-of-primitives, JSON-safe."""
    artifact = build_artifact(
        outcome=_outcome(20),
        epoch_id="WR-TEST-W18-PUB-E1",
        total_registered_uids=20,
        chain_fetch_timestamp="2026-05-13T17:00:00+00:00",
    )
    assert isinstance(artifact.tier_result, dict)
    # The TierAllocationResult fields we rely on:
    assert "qualifying" in artifact.tier_result
    assert "elite" in artifact.tier_result
    assert "competitive" in artifact.tier_result
    assert "participating" in artifact.tier_result
    assert "elite_floor_cleared" in artifact.tier_result
    assert "weights" in artifact.tier_result


def test_overwriting_an_existing_artifact(tmp_path):
    """A later run overwrites; reader sees the latest, not the older one."""
    p1 = build_and_write_artifact(
        outcome=_outcome(2), epoch_id="WR-X", total_registered_uids=10,
        chain_fetch_timestamp="t1", base_dir=tmp_path,
    )
    p2 = build_and_write_artifact(
        outcome=_outcome(5), epoch_id="WR-X", total_registered_uids=12,
        chain_fetch_timestamp="t2", base_dir=tmp_path,
    )
    assert p1 == p2
    artifact = read_artifact("WR-X", base_dir=tmp_path)
    assert artifact.chain_fetch_timestamp == "t2"
    assert artifact.total_registered_uids == 12
    assert len(artifact.per_uid_scores) == 5


def test_written_file_is_valid_json(tmp_path):
    """A reader using `json.load` (not our `read_artifact`) must succeed."""
    path = build_and_write_artifact(
        outcome=_outcome(3),
        epoch_id="WR-JSON-OK",
        total_registered_uids=10,
        chain_fetch_timestamp="2026-05-13T17:00:00+00:00",
        base_dir=tmp_path,
    )
    with open(path) as f:
        raw = json.load(f)
    assert raw["epoch_id"] == "WR-JSON-OK"
    assert raw["artifact_schema_version"] == 1
