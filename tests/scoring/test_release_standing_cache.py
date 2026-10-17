"""Standing memory released after the steps that load it (17 Oct 2026).

The single-slot standing cache never hit inside a daily run (each caller asks
with different settings) and kept a full copy of the window's entries resident:
+0.8 GB after type accuracy, +1.1 GB after weights. It is now dropped after
those steps; a later call rebuilds the same standing."""
from __future__ import annotations

import inspect
from datetime import date

from hope.scoring import standing_method as sm
from hope.validator import daily_loop
from tests.scoring.test_standing_index import ENV, _flat, _ledger


def test_release_drops_the_cached_entries_and_a_rebuild_is_identical(tmp_path):
    root = str(tmp_path)
    _ledger(root)
    day = date(2026, 10, 3)
    sm._CACHE.clear()
    first = _flat(sm.load_relative_entries(root, day, 28, environ=ENV))
    assert sm._CACHE                              # the load filled the slot
    sm.release_standing_cache()
    assert not sm._CACHE
    assert _flat(sm.load_relative_entries(root, day, 28, environ=ENV)) == first


def test_release_never_raises_without_glibc(monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes, "CDLL", lambda name: (_ for _ in ()).throw(OSError("no libc")))
    sm._CACHE["k"] = ("sig", {}, {})
    sm.release_standing_cache()
    assert not sm._CACHE


def test_the_daily_loop_releases_after_type_accuracy_and_after_weights():
    src = inspect.getsource(daily_loop.run_daily_loop)
    for step in ('_mem("type accuracy")', '_mem("weights")'):
        before = src[:src.index(step)].rstrip().splitlines()[-1].strip()
        assert before == "_release_standing_memory()", step


def test_the_type_accuracy_step_keeps_only_the_winner():
    src = inspect.getsource(daily_loop.run_daily_loop)
    block = src[src.index("_by_miner = load_standing_entries"):src.index('_mem("type accuracy")')]
    assert "_by_miner = _standing = None" in block


def test_a_failing_release_cannot_fail_the_run(monkeypatch):
    monkeypatch.setattr(sm, "release_standing_cache", lambda: 1 / 0)
    daily_loop._release_standing_memory()          # swallowed
