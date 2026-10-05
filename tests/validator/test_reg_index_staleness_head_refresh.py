"""The reg-index staleness alarm follows the head refresh, not only the block-scan.

With the archive block-scan switched off (SN21_SKIP_REG_INDEX_BLOCK), the
sidecar's last_scanned_block froze and the alarm fired on every tick although
the head refresh kept the index current. The alarm now takes the newer of the
two stamps, and still fires when the head refresh stops completing.
"""
import json
import types

import pytest

from scripts import check_reg_index_staleness as alarm
from scripts import refresh_reg_index_head as head_refresh
from scripts import build_reg_index as builder


def _write_state(tmp_path, **state):
    index = tmp_path / "reg-index.json"
    index.write_text("[]")
    (tmp_path / "reg-index.json.state.json").write_text(json.dumps(state))
    return str(index)


def test_freshest_takes_the_newer_stamp(tmp_path):
    idx = _write_state(tmp_path, last_scanned_block=9_000_000, last_head_refresh_block=9_217_900)
    assert alarm.read_freshest_block(idx + ".state.json") == (9_217_900, "head refresh")
    idx = _write_state(tmp_path, last_scanned_block=9_217_950, last_head_refresh_block=9_217_900)
    assert alarm.read_freshest_block(idx + ".state.json") == (9_217_950, "archive block-scan")


def test_only_the_old_scan_stamp_still_works(tmp_path):
    idx = _write_state(tmp_path, last_scanned_block=9_100_000)
    assert alarm.read_freshest_block(idx + ".state.json") == (9_100_000, "archive block-scan")
    assert alarm.read_last_scanned_block(idx + ".state.json") == 9_100_000


def test_missing_or_broken_sidecar_is_unknown(tmp_path):
    assert alarm.read_freshest_block(str(tmp_path / "nope.state.json")) == (None, "none")
    bad = tmp_path / "x.state.json"
    bad.write_text("not json")
    assert alarm.read_freshest_block(str(bad)) == (None, "none")


def _run_alarm(monkeypatch, idx, head):
    import hope.validator._subtensor as st
    monkeypatch.setattr(st, "make_subtensor", lambda net: types.SimpleNamespace(get_current_block=lambda: head))
    return alarm.main(["--index", idx, "--threshold-blocks", "2000"])


def test_alarm_quiet_when_head_refresh_is_recent(tmp_path, monkeypatch):
    # the real situation on 5 Oct: block-scan frozen far behind, head refresh current
    idx = _write_state(tmp_path, last_scanned_block=9_000_000, last_head_refresh_block=9_217_500)
    assert _run_alarm(monkeypatch, idx, head=9_217_960) == alarm.EXIT_FRESH


def test_alarm_fires_when_head_refresh_stops(tmp_path, monkeypatch):
    idx = _write_state(tmp_path, last_scanned_block=9_000_000, last_head_refresh_block=9_210_000)
    assert _run_alarm(monkeypatch, idx, head=9_217_960) == alarm.EXIT_STALE


def test_alarm_fires_as_before_without_a_head_stamp(tmp_path, monkeypatch):
    idx = _write_state(tmp_path, last_scanned_block=9_000_000)
    assert _run_alarm(monkeypatch, idx, head=9_217_960) == alarm.EXIT_STALE


def test_head_refresh_stamp_preserves_other_keys(tmp_path):
    idx = _write_state(tmp_path, last_scanned_block=9_000_000, role="miner", netuid=21)
    head_refresh._stamp_head_refresh(idx, 9_217_960, 223)
    state = json.loads((tmp_path / "reg-index.json.state.json").read_text())
    assert state["last_head_refresh_block"] == 9_217_960
    assert state["head_refresh_entries"] == 223
    assert state["last_scanned_block"] == 9_000_000 and state["role"] == "miner"


def test_head_refresh_stamp_creates_sidecar_when_missing(tmp_path):
    idx = str(tmp_path / "reg-index.json")
    head_refresh._stamp_head_refresh(idx, 100, 1)
    state = json.loads((tmp_path / "reg-index.json.state.json").read_text())
    assert state["last_head_refresh_block"] == 100


def test_builder_save_keeps_the_head_refresh_stamp(tmp_path):
    idx = _write_state(tmp_path, last_scanned_block=1, last_head_refresh_block=9_217_960)
    builder._save(idx, "miner", 21, 9_100_000, [])
    state = json.loads((tmp_path / "reg-index.json.state.json").read_text())
    assert state["last_scanned_block"] == 9_100_000
    assert state["last_head_refresh_block"] == 9_217_960


def test_refresh_stamps_only_after_a_completed_sweep(tmp_path, monkeypatch):
    idx = str(tmp_path / "reg-index.json")
    fake = types.SimpleNamespace(
        get_current_block=lambda: 555,
        metagraph=lambda netuid: types.SimpleNamespace(hotkeys=[]),
    )
    monkeypatch.setattr(head_refresh, "make_subtensor", lambda net: fake)
    head_refresh.refresh(idx, "finney", 21, head_refresh._ROLES["miner"])
    state = json.loads((tmp_path / "reg-index.json.state.json").read_text())
    assert state["last_head_refresh_block"] == 555

    def boom(net):
        raise RuntimeError("chain unreachable")
    monkeypatch.setattr(head_refresh, "make_subtensor", boom)
    with pytest.raises(RuntimeError):
        head_refresh.refresh(idx, "finney", 21, head_refresh._ROLES["miner"])
    state = json.loads((tmp_path / "reg-index.json.state.json").read_text())
    assert state["last_head_refresh_block"] == 555          # unchanged by the failure
