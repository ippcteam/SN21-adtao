"""Tests for the rolling reg-index builder (scripts/build_reg_index.py).

Cover the NEW logic — checkpoint persistence, resume, and the scan-range
computation — without touching a live chain (scan_range is stubbed).
"""
import json

from hope.commitment.registration import RegistrationRole
from hope.validator.registration_index import RegistrationIndex
from scripts.build_reg_index import _load, _save, _state_path, build_once


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


def test_build_once_max_blocks_per_pass_caps_incremental(tmp_path):
    idx = _index(10000)
    idx.set_last_scanned_block(900)
    calls = []
    _stub_scan(idx, calls)
    build_once(idx, str(tmp_path / "i.json"), "miner", 21,
               backfill_start=None, cold_start_lookback=14_400, checkpoint_every=500,
               max_blocks_per_pass=50)
    assert calls == [(901, 950)]  # 901 .. 901+50-1


def test_build_once_max_blocks_per_pass_caps_cold_start(tmp_path):
    idx = _index(10000)  # no checkpoint; lookback 5000 -> start 5000
    calls = []
    _stub_scan(idx, calls)
    build_once(idx, str(tmp_path / "i.json"), "miner", 21,
               backfill_start=None, cold_start_lookback=5000, checkpoint_every=500,
               max_blocks_per_pass=50)
    assert calls == [(5000, 5049)]


def test_build_once_up_to_date_does_not_scan(tmp_path):
    idx = _index(1000)
    idx.set_last_scanned_block(1000)
    calls = []
    _stub_scan(idx, calls)
    found = build_once(idx, str(tmp_path / "i.json"), "miner", 21,
                       backfill_start=None, cold_start_lookback=100, checkpoint_every=500)
    assert calls == []
    assert found == 0


# ---------------------------------------------------------------------------
# Anti-clobber: a slow build pass must NOT overwrite additions a concurrent
# writer (head-refresh) made to the file between this pass's load and save.
# Regression for the live 221 -> 167 revert.
# ---------------------------------------------------------------------------


def _je(pk_hex: str, block: int) -> dict:
    return {
        "hotkey_ss58": "5Stub" + pk_hex[:6],
        "hotkey_pk_hex": pk_hex,
        "ed25519_pk_hex": "ab" * 32,
        "role": "M",
        "block_number": block,
    }


def test_save_unions_with_concurrent_on_disk_additions(tmp_path):
    path = str(tmp_path / "idx.json")
    # Disk already has A + B (e.g. written by a concurrent head-refresh).
    with open(path, "w") as f:
        json.dump([_je("aa" * 32, 100), _je("bb" * 32, 100)], f)
    # A slow build pass only knows about C — saving must NOT drop A and B.
    _save(path, "miner", 21, last_scanned_block=500, entries=[_je("cc" * 32, 200)])
    with open(path) as f:
        saved = {e["hotkey_pk_hex"] for e in json.load(f)}
    assert saved == {"aa" * 32, "bb" * 32, "cc" * 32}  # union, nothing clobbered


def test_save_keeps_higher_block_per_hotkey(tmp_path):
    path = str(tmp_path / "idx.json")
    with open(path, "w") as f:
        json.dump([_je("aa" * 32, 100)], f)  # disk: A @ 100
    _save(path, "miner", 21, last_scanned_block=500, entries=[_je("aa" * 32, 250)])  # newer A @ 250
    with open(path) as f:
        rows = json.load(f)
    assert len(rows) == 1 and rows[0]["block_number"] == 250  # newer wins


def test_save_no_prior_file_writes_entries(tmp_path):
    path = str(tmp_path / "fresh.json")
    _save(path, "miner", 21, last_scanned_block=10, entries=[_je("dd" * 32, 5)])
    with open(path) as f:
        assert {e["hotkey_pk_hex"] for e in json.load(f)} == {"dd" * 32}
