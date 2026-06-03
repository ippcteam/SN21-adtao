"""Tests for the rolling reg-index builder (scripts/build_reg_index.py).

Cover the NEW logic — checkpoint persistence, resume, and the scan-range
computation — without touching a live chain (scan_range is stubbed).
"""
import json

import pytest

from hope.commitment.registration import RegistrationRole
from hope.validator.registration_index import RegistrationIndex
from scripts.build_reg_index import _save, _load, _state_path, build_once


class _StubSubtensor:
    """Minimal stand-in: only get_current_block is exercised by build_once."""

    def __init__(self, head: int):
        self._head = head

    def get_current_block(self) -> int:
        return self._head


def _entry(block: int, seed: int) -> dict:
    """A serialised reg-index entry (matches RegistrationIndex.to_json shape)."""
    hk = bytes([seed]) * 32
    ed = bytes([seed + 1]) * 32
    return {
        "hotkey_ss58": f"5Fake{seed:056d}",
        "hotkey_pk_hex": hk.hex(),
        "ed25519_pk_hex": ed.hex(),
        "role": RegistrationRole.MINER.value,
        "block_number": block,
    }


def _index(head: int) -> RegistrationIndex:
    return RegistrationIndex(_StubSubtensor(head), 21,
                             expected_role=RegistrationRole.MINER)


def test_checkpoint_property_roundtrip():
    idx = _index(1000)
    assert idx.last_scanned_block is None
    idx.set_last_scanned_block(777)
    assert idx.last_scanned_block == 777


def test_save_writes_bare_list_plus_sidecar(tmp_path):
    idx = _index(1000)
    idx.merge_json([_entry(900, 1), _entry(950, 2)])
    path = str(tmp_path / "reg-index.json")
    _save(path, "miner", 21, last_scanned_block=999, entries=idx.to_json())

    # index file is a BARE LIST (the format every consumer expects)
    with open(path) as f:
        data = json.load(f)
    assert isinstance(data, list) and len(data) == 2

    # sidecar holds the checkpoint
    with open(_state_path(path)) as f:
        state = json.load(f)
    assert state["last_scanned_block"] == 999
    assert state["entries"] == 2


def test_load_restores_entries_and_checkpoint(tmp_path):
    src = _index(1000)
    src.merge_json([_entry(900, 1), _entry(950, 2)])
    path = str(tmp_path / "reg-index.json")
    _save(path, "miner", 21, last_scanned_block=999, entries=src.to_json())

    dst = _index(1000)
    restored = _load(path, dst)
    assert restored == 999
    assert dst.last_scanned_block == 999
    assert dst.size == 2


def test_load_missing_file_returns_none(tmp_path):
    dst = _index(1000)
    assert _load(str(tmp_path / "nope.json"), dst) is None
    assert dst.last_scanned_block is None


def _stub_scan(idx, recorder):
    """Replace scan_range with a recorder that sets the cursor to end_block."""
    def _fake(start_block, end_block, **kw):
        recorder.append((start_block, end_block))
        idx.set_last_scanned_block(end_block)
        return 0
    idx.scan_range = _fake  # type: ignore[assignment]


def test_build_once_incremental_scans_from_checkpoint_plus_one(tmp_path):
    idx = _index(1000)
    idx.set_last_scanned_block(900)
    calls = []
    _stub_scan(idx, calls)
    build_once(idx, str(tmp_path / "i.json"), "miner", 21,
               backfill_start=None, cold_start_lookback=14_400, checkpoint_every=500)
    assert calls == [(901, 1000)]


def test_build_once_cold_start_uses_lookback_window(tmp_path):
    idx = _index(1000)  # no checkpoint
    calls = []
    _stub_scan(idx, calls)
    build_once(idx, str(tmp_path / "i.json"), "miner", 21,
               backfill_start=None, cold_start_lookback=100, checkpoint_every=500)
    assert calls == [(900, 1000)]


def test_build_once_cold_start_honors_backfill_start(tmp_path):
    idx = _index(1000)
    calls = []
    _stub_scan(idx, calls)
    build_once(idx, str(tmp_path / "i.json"), "miner", 21,
               backfill_start=500, cold_start_lookback=100, checkpoint_every=500)
    assert calls == [(500, 1000)]


def test_build_once_up_to_date_does_not_scan(tmp_path):
    idx = _index(1000)
    idx.set_last_scanned_block(1000)
    calls = []
    _stub_scan(idx, calls)
    found = build_once(idx, str(tmp_path / "i.json"), "miner", 21,
                       backfill_start=None, cold_start_lookback=100, checkpoint_every=500)
    assert calls == []
    assert found == 0
