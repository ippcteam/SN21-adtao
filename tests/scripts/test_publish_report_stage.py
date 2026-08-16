"""stage_publish_report — the gated daily leaderboard publish.

The critical property: nothing reaches the CMS before the transition-plan
reveal. Both the master flag and the date lock are exercised here, plus the
happy path (flag on, date reached) with the metagraph read and the CMS POST
mocked so the test never touches the network.
"""

from __future__ import annotations

import json
import os

import pytest
from substrateinterface.utils.ss58 import ss58_encode

import scripts.run_daily_pipeline as rdp


def _hk(seed: int) -> str:
    return ss58_encode(bytes([seed]) * 32, ss58_format=42)


@pytest.fixture
def ledger_with_standings(tmp_path):
    day = "2026-08-20"
    intent = {
        "day": day,
        "gated": False,
        "weights": {_hk(1): 0.6, _hk(2): 0.4},
        "standings": {_hk(1): 0.80, _hk(2): 0.50, _hk(3): 0.20},
    }
    (tmp_path / f"intended_weights_{day}.json").write_text(json.dumps(intent))
    return str(tmp_path), day


def _clear_gate_env(monkeypatch):
    for k in ("SN21_DAILY_REPORT_PUBLISH", "SN21_DAILY_REPORT_NOT_BEFORE",
              "SN21_LEADERBOARD_API_KEY", "SN21_LEADERBOARD_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)


def test_flag_off_holds_by_default(monkeypatch, ledger_with_standings):
    root, day = ledger_with_standings
    _clear_gate_env(monkeypatch)
    out = rdp.stage_publish_report(root, day)
    assert out["published"] is False
    assert "flag off" in out["reason"]


def test_date_lock_holds_even_with_flag_on(monkeypatch, ledger_with_standings):
    root, day = ledger_with_standings
    _clear_gate_env(monkeypatch)
    monkeypatch.setenv("SN21_DAILY_REPORT_PUBLISH", "1")
    # A reveal date far in the future: the flag is on but the calendar isn't.
    monkeypatch.setenv("SN21_DAILY_REPORT_NOT_BEFORE", "2099-01-01")
    out = rdp.stage_publish_report(root, day)
    assert out["published"] is False
    assert "before reveal date" in out["reason"]


def test_happy_path_builds_and_posts(monkeypatch, ledger_with_standings):
    root, day = ledger_with_standings
    _clear_gate_env(monkeypatch)
    monkeypatch.setenv("SN21_DAILY_REPORT_PUBLISH", "1")
    monkeypatch.setenv("SN21_DAILY_REPORT_NOT_BEFORE", "2000-01-01")
    monkeypatch.setenv("SN21_LEADERBOARD_API_KEY", "test-key")

    # Metagraph: map the three standing hotkeys to real UIDs.
    monkeypatch.setattr(rdp, "_uid_by_hotkey",
                        lambda: {_hk(1): 1, _hk(2): 2, _hk(3): 3})

    captured = {}

    class _Resp:
        status_code = 200

    def _fake_post(payload, *, endpoint, api_key, **kw):
        captured["payload"] = payload
        captured["endpoint"] = endpoint
        captured["api_key"] = api_key
        return _Resp()

    monkeypatch.setattr("scripts.post_epoch_report.post_payload", _fake_post)

    out = rdp.stage_publish_report(root, day)
    assert out["published"] is True
    assert out["epoch_id"] == f"BD-{day}"
    assert out["miners"] == 3
    assert out["status"] == 200
    # Posted the daily horizons, and only to the configured/default endpoint.
    assert captured["payload"].horizon_set == ["7d", "14d", "28d"]
    assert captured["api_key"] == "test-key"


def test_metagraph_unavailable_holds(monkeypatch, ledger_with_standings):
    root, day = ledger_with_standings
    _clear_gate_env(monkeypatch)
    monkeypatch.setenv("SN21_DAILY_REPORT_PUBLISH", "1")
    monkeypatch.setenv("SN21_DAILY_REPORT_NOT_BEFORE", "2000-01-01")
    monkeypatch.setenv("SN21_LEADERBOARD_API_KEY", "test-key")
    monkeypatch.setattr(rdp, "_uid_by_hotkey", lambda: None)
    out = rdp.stage_publish_report(root, day)
    assert out["published"] is False
    assert out["reason"] == "metagraph unavailable"


def test_missing_api_key_holds(monkeypatch, ledger_with_standings):
    root, day = ledger_with_standings
    _clear_gate_env(monkeypatch)
    monkeypatch.setenv("SN21_DAILY_REPORT_PUBLISH", "1")
    monkeypatch.setenv("SN21_DAILY_REPORT_NOT_BEFORE", "2000-01-01")
    monkeypatch.setattr(rdp, "_uid_by_hotkey", lambda: {_hk(1): 1})
    out = rdp.stage_publish_report(root, day)
    assert out["published"] is False
    assert "API_KEY" in out["reason"]
