"""Tests for the reg-index JSON loader used by the validator-api.

Mirrors the prebuilt-JSON shape produced by RegistrationIndex.to_json
(see hope/validator/registration_index.py). The loader must:
  - keep miner-role and legacy role-less entries
  - drop validator / outcome-signer entries (those are unrelated to the
    submission gate)
  - track file mtime so the lifespan refresh loop can detect changes
"""

from __future__ import annotations

import json
import os
import time

from hope.validator.serve import _load_registered_hotkeys, _maybe_refresh_reg_index


def _write_index(path: str, entries: list[dict]) -> None:
    with open(path, "w") as f:
        json.dump(entries, f)


def test_loader_returns_empty_when_path_missing(tmp_path):
    hotkeys, mtime = _load_registered_hotkeys(str(tmp_path / "no-such-file.json"))
    assert hotkeys == set()
    assert mtime is None


def test_loader_returns_empty_when_path_none():
    hotkeys, mtime = _load_registered_hotkeys(None)
    assert hotkeys == set()
    assert mtime is None


def test_loader_keeps_miner_role_entries(tmp_path):
    path = str(tmp_path / "reg.json")
    _write_index(path, [
        {"hotkey_ss58": "5G1", "role": "M", "block_number": 100},
        {"hotkey_ss58": "5G2", "role": "M", "block_number": 200},
    ])
    hotkeys, mtime = _load_registered_hotkeys(path)
    assert hotkeys == {"5G1", "5G2"}
    assert mtime is not None


def test_loader_drops_validator_and_outcome_signer_entries(tmp_path):
    path = str(tmp_path / "reg.json")
    _write_index(path, [
        {"hotkey_ss58": "5GMINER", "role": "M", "block_number": 100},
        {"hotkey_ss58": "5GVAL", "role": "V", "block_number": 200},
        {"hotkey_ss58": "5GSIGNER", "role": "O", "block_number": 300},
    ])
    hotkeys, _ = _load_registered_hotkeys(path)
    assert hotkeys == {"5GMINER"}


def test_loader_keeps_legacy_role_less_entries(tmp_path):
    """Older indices may not have the role field — keep them as miners."""
    path = str(tmp_path / "reg.json")
    _write_index(path, [
        {"hotkey_ss58": "5GLEGACY", "block_number": 100},
    ])
    hotkeys, _ = _load_registered_hotkeys(path)
    assert hotkeys == {"5GLEGACY"}


def test_loader_handles_bad_json(tmp_path):
    path = str(tmp_path / "reg.json")
    with open(path, "w") as f:
        f.write("not json at all")
    hotkeys, mtime = _load_registered_hotkeys(path)
    assert hotkeys == set()
    # mtime is still set — the file exists; only the parse failed.
    assert mtime is not None


def test_loader_handles_non_list_payload(tmp_path):
    path = str(tmp_path / "reg.json")
    _write_index(path, {"unexpected": "shape"})  # type: ignore[arg-type]
    hotkeys, _ = _load_registered_hotkeys(path)
    assert hotkeys == set()


def test_refresh_picks_up_new_entries(tmp_path):
    path = str(tmp_path / "reg.json")
    _write_index(path, [{"hotkey_ss58": "5G1", "role": "M", "block_number": 100}])
    initial_hotkeys, initial_mtime = _load_registered_hotkeys(path)

    state = {
        "_reg_index_path": path,
        "_reg_index_mtime_ns": initial_mtime,
        "registered_ed25519_hotkeys": initial_hotkeys,
    }

    # Sleep enough that mtime_ns is guaranteed to differ on all filesystems.
    time.sleep(0.01)
    _write_index(path, [
        {"hotkey_ss58": "5G1", "role": "M", "block_number": 100},
        {"hotkey_ss58": "5G2", "role": "M", "block_number": 200},
    ])
    # Force-bump mtime — some CI filesystems (overlayfs) round mtime to
    # the second, so the test write may share an mtime with the initial
    # write. Touching after the write guarantees a fresh mtime_ns.
    fresh_ns = initial_mtime + 1_000_000_000
    os.utime(path, ns=(fresh_ns, fresh_ns))

    _maybe_refresh_reg_index(state)

    assert state["registered_ed25519_hotkeys"] == {"5G1", "5G2"}
    assert state["_reg_index_mtime_ns"] != initial_mtime


def test_refresh_noop_when_mtime_unchanged(tmp_path):
    path = str(tmp_path / "reg.json")
    _write_index(path, [{"hotkey_ss58": "5G1", "role": "M", "block_number": 100}])
    initial_hotkeys, initial_mtime = _load_registered_hotkeys(path)

    state = {
        "_reg_index_path": path,
        "_reg_index_mtime_ns": initial_mtime,
        "registered_ed25519_hotkeys": initial_hotkeys,
    }

    _maybe_refresh_reg_index(state)
    # File untouched — set must be the same object identity is not
    # required, but contents must match.
    assert state["registered_ed25519_hotkeys"] == {"5G1"}
