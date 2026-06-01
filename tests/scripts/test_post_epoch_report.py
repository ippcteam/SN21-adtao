"""Tests for scripts/post_epoch_report.py.

Uses httpx.MockTransport (built-in) to mock the CMS endpoint without
network access. Tests cover the matrix the data contract pins:

  * 201 Created / 200 OK draft-upsert paths
  * 409 Conflict (already-published epoch — terminal)
  * 429 Too Many Requests
  * 5xx with exponential backoff up to max_attempts
  * 4xx schema-mismatch (terminal)
  * dry-run path (no network)
  * missing API key → exit 1 before send
  * bearer redacted in any error/info output
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

# scripts/ isn't a package in older test setups; ensure it's importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import post_epoch_report as por  # noqa: E402


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

def _write_synthetic_artifact(tmp_path: Path, n_qualifying: int = 20) -> Path:
    """Write an EpochArtifact JSON for the script to consume."""
    from hope.reporting.epoch_artifact import build_and_write_artifact
    from hope.commitment.on_chain import CommitResult
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
            miner_hotkey=bytes([uid % 256]) * 32,
            miner_uid=uid,
            ok=True,
            plaintext={"horizons": []},
            excluded_reason=None,
            fetch=None,
            on_chain=OnChainCommitTriple(
                timelock_k_present=True,
                sha256_ct_commit=bytes([uid % 256]) * 32,
                self_archive_url=f"https://archive.example.com/m{uid}",
                chain_block_at_k_commit=1_000_000 + uid,
            ),
            scoreability=None,
        )

    score_map = {bytes([i % 256]) * 32: 500_000 + i * 10_000 for i in range(n_qualifying)}
    outcome = EpochScoringOutcome(
        pre_scoring_commit=_commit_ok(1_000_000),
        weights_commit=_weights_ok(1_001_000),
        post_scoring_commit=_commit_ok(1_002_000),
        miner_reads=[_read(i) for i in range(n_qualifying)],
        score_map=score_map,
        block_range_start=1_000_000,
        block_range_end=1_001_000,
    )
    path = build_and_write_artifact(
        outcome=outcome,
        epoch_id="WR-2026-W18-PUB-E1",
        total_registered_uids=n_qualifying + 5,
        chain_fetch_timestamp="2026-05-13T16:55:00+00:00",
        base_dir=tmp_path,
    )
    return path


def _load_payload(tmp_path: Path):
    """Reproduce what the CLI produces — the aggregator output."""
    from hope.reporting.aggregator import aggregate
    from hope.reporting.epoch_artifact import read_artifact
    artifact = read_artifact("WR-2026-W18-PUB-E1", base_dir=tmp_path)
    return aggregate(artifact)


# ----------------------------------------------------------------------------
# Headers / building blocks
# ----------------------------------------------------------------------------

def test_build_request_headers_shape():
    headers = por.build_request_headers("test-key", "WR-EPOCH-123")
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Idempotency-Key"] == "epoch-WR-EPOCH-123"
    assert headers["Content-Type"] == "application/json"


def test_mask_bearer_redacts():
    masked = por._mask_bearer({"Authorization": "Bearer secret-shhh",
                                "Idempotency-Key": "epoch-x"})
    assert masked["Authorization"] == "Bearer <redacted>"
    assert masked["Idempotency-Key"] == "epoch-x"   # other headers untouched


# ----------------------------------------------------------------------------
# post_payload — happy paths
# ----------------------------------------------------------------------------

def test_post_payload_201_terminal_success(tmp_path):
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)

    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(
            201,
            json={"id": "abc-123", "status": "draft", "review_url": "https://cms.example.com/admin/abc-123"},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        response = por.post_payload(payload, endpoint="https://cms.example.com/api",
                                    api_key="test", client=client)

    assert response.status_code == 201
    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.headers["Authorization"] == "Bearer test"
    assert req.headers["Idempotency-Key"] == "epoch-WR-2026-W18-PUB-E1"


def test_post_payload_200_draft_upsert(tmp_path):
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "abc", "status": "draft", "review_url": "x"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(payload, endpoint="https://cms.example.com/api",
                                    api_key="test", client=client)
    assert response.status_code == 200


# ----------------------------------------------------------------------------
# post_payload — failure paths
# ----------------------------------------------------------------------------

def test_post_payload_409_no_retry(tmp_path):
    """409 Conflict (already-published) is terminal — must not retry."""
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)

    call_count = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(409, json={"detail": "already published"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(payload, endpoint="https://cms.example.com/api",
                                    api_key="test", client=client,
                                    sleep_fn=lambda s: None)
    assert response.status_code == 409
    assert call_count["n"] == 1   # exactly one attempt — no retry on 4xx


def test_post_payload_400_no_retry(tmp_path):
    """400 schema mismatch is terminal."""
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)
    n_calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        n_calls["n"] += 1
        return httpx.Response(400, json={"detail": "schema validation failed"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(payload, endpoint="https://cms.example.com/api",
                                    api_key="test", client=client,
                                    sleep_fn=lambda s: None)
    assert response.status_code == 400
    assert n_calls["n"] == 1


def test_post_payload_5xx_then_success(tmp_path):
    """5xx triggers exponential backoff; eventually 201 lands."""
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)

    statuses = [503, 502, 500, 201]
    call_count = {"n": 0}
    sleep_records: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses[call_count["n"]]
        call_count["n"] += 1
        return httpx.Response(status, json={"id": "x"} if status < 400 else {"detail": "down"})

    def fake_sleep(seconds: float) -> None:
        sleep_records.append(seconds)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(
            payload, endpoint="https://cms.example.com/api", api_key="test",
            client=client, max_attempts=5,
            backoff_initial_seconds=1.0, backoff_factor=2.0,
            sleep_fn=fake_sleep,
        )
    assert response.status_code == 201
    assert call_count["n"] == 4
    # 3 sleeps between 4 attempts (1s, 2s, 4s — geometric)
    assert sleep_records == [1.0, 2.0, 4.0]


def test_post_payload_5xx_exhausted_retries(tmp_path):
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)
    n_calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        n_calls["n"] += 1
        return httpx.Response(503, json={"detail": "down"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(
            payload, endpoint="https://cms.example.com/api", api_key="test",
            client=client, max_attempts=3, sleep_fn=lambda s: None,
        )
    assert response.status_code == 503
    assert n_calls["n"] == 3


def test_post_payload_429_no_retry(tmp_path):
    """429 is a 4xx — terminal in our scheme (the cron sleeps + retries via re-run).

    Operators implementing custom retry-after handling should wrap post_payload.
    """
    _write_synthetic_artifact(tmp_path)
    payload = _load_payload(tmp_path)
    n_calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        n_calls["n"] += 1
        return httpx.Response(429, json={"detail": "rate limited"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = por.post_payload(payload, endpoint="https://cms.example.com/api",
                                    api_key="test", client=client,
                                    sleep_fn=lambda s: None)
    assert response.status_code == 429
    assert n_calls["n"] == 1


# ----------------------------------------------------------------------------
# report_response — exit code mapping
# ----------------------------------------------------------------------------

def _resp(status: int, body: dict | str = "") -> httpx.Response:
    if isinstance(body, dict):
        return httpx.Response(status, json=body)
    return httpx.Response(status, text=body)


def test_report_response_201_to_exit_ok(capsys):
    code = por.report_response(_resp(201, {"id": "abc", "review_url": "https://x.example.com"}))
    assert code == por.EXIT_OK
    captured = capsys.readouterr()
    assert "review_url: https://x.example.com" in captured.out


def test_report_response_4xx_to_exit_rejected():
    assert por.report_response(_resp(400, {"detail": "bad schema"})) == por.EXIT_REJECTED
    assert por.report_response(_resp(409, {"detail": "frozen"})) == por.EXIT_REJECTED
    assert por.report_response(_resp(401, {"detail": "no auth"})) == por.EXIT_REJECTED


def test_report_response_5xx_to_exit_persistent():
    assert por.report_response(_resp(500, {"detail": "boom"})) == por.EXIT_PERSISTENT_5XX
    assert por.report_response(_resp(503, {"detail": "down"})) == por.EXIT_PERSISTENT_5XX


# ----------------------------------------------------------------------------
# main — CLI integration
# ----------------------------------------------------------------------------

def test_main_dry_run_succeeds_without_api_key(tmp_path, capsys, monkeypatch):
    artifact_path = _write_synthetic_artifact(tmp_path)
    monkeypatch.delenv("SN21_LEADERBOARD_API_KEY", raising=False)
    exit_code = por.main(["--artifact", str(artifact_path), "--dry-run"])
    assert exit_code == por.EXIT_OK
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["epoch_id"] == "WR-2026-W18-PUB-E1"
    assert out["pool_size"] >= 0


def test_main_missing_api_key_fails(tmp_path, monkeypatch):
    artifact_path = _write_synthetic_artifact(tmp_path)
    monkeypatch.delenv("SN21_LEADERBOARD_API_KEY", raising=False)
    exit_code = por.main(["--artifact", str(artifact_path)])
    assert exit_code == por.EXIT_MISCONFIG


def test_main_missing_artifact_file_fails(tmp_path):
    exit_code = por.main([
        "--artifact", str(tmp_path / "does-not-exist.json"),
        "--dry-run",
    ])
    assert exit_code == por.EXIT_MISCONFIG


# ---------------------------------------------------------------------------
# _is_frozen_409 — IA D-13 published/frozen detection (drives auto-correction)
# ---------------------------------------------------------------------------

def test_is_frozen_409_detects_published_frozen():
    r = httpx.Response(409, json={"error": "Epoch WR-2026-W22-PUB-E1 is published and frozen (IA D-13). Publish a correction as a new epoch entry — do not mutate history."})
    assert por._is_frozen_409(r) is True


def test_is_frozen_409_detail_key_variant():
    r = httpx.Response(409, json={"detail": "row is frozen"})
    assert por._is_frozen_409(r) is True


def test_is_frozen_409_other_409_not_frozen():
    # A 409 that isn't a freeze (e.g. generic conflict) must NOT trigger correction.
    r = httpx.Response(409, json={"error": "duplicate idempotency key"})
    assert por._is_frozen_409(r) is False


def test_is_frozen_409_non_409_statuses():
    assert por._is_frozen_409(httpx.Response(200, json={"id": "x"})) is False
    assert por._is_frozen_409(httpx.Response(400, json={"error": "frozen"})) is False  # wrong status
    assert por._is_frozen_409(httpx.Response(500, text="published")) is False
