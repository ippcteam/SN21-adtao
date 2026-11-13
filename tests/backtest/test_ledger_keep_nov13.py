"""The ledger disk keep rule (hope.backtest.ledger_keep), 13 Nov 2026.

A dry run unless SN21_LEDGER_KEEP_APPLY=1, and only files no step reads again:
old cumulative Prediction Performance documents (never the newest) and stale
*.tmp leftovers. Everything else on the ledger is untouched."""
import os
import time
from datetime import date

from hope.backtest import ledger_keep as K

TODAY = date(2026, 11, 13)


def _ledger(tmp_path):
    perf = tmp_path / "prediction_performance"
    perf.mkdir()
    for d in ("2026-10-01", "2026-10-29", "2026-10-30", "2026-11-12", "2026-11-13"):
        (perf / f"{d}.json").write_text("x" * 1000)
    for folder in ("receipts", "shadow/2026-10-01", "fingerprints", "allocation_audit", "pipeline_runs"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "2026-10-01.json").write_text("y" * 500)
    old_tmp = tmp_path / "receipts" / "2026-11-01.json.tmp"
    old_tmp.write_text("z" * 10)
    os.utime(old_tmp, (time.time() - 3 * 86400,) * 2)
    (tmp_path / "receipts" / "2026-11-13.json.tmp").write_text("fresh")      # a write in progress
    return tmp_path


def _left(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def test_a_dry_run_counts_and_removes_nothing(tmp_path):
    root = _ledger(tmp_path)
    before = _left(root)
    s = K.run_ledger_keep(str(root), TODAY, environ={})
    assert s["mode"] == "dry run" and s["removed"] == 0
    assert s["files"] == 3 and s["mb"] == round(2010 / 1e6, 1)
    assert _left(root) == before
    assert s["folders_mb"]["prediction_performance"] == round(5000 / 1e6, 1)


def test_apply_removes_only_old_performance_documents_and_stale_tmp_files(tmp_path):
    root = _ledger(tmp_path)
    s = K.run_ledger_keep(str(root), TODAY, environ={K.APPLY_ENV: "1"})
    assert s["removed"] == 3 and s["failed"] == 0
    left = _left(root)
    assert "prediction_performance/2026-10-01.json" not in left
    assert "prediction_performance/2026-10-29.json" not in left
    assert "prediction_performance/2026-10-30.json" in left           # 14 days back is kept
    assert "receipts/2026-11-01.json.tmp" not in left
    assert "receipts/2026-11-13.json.tmp" in left                     # not a day old
    for kept in ("receipts/2026-10-01.json", "shadow/2026-10-01/2026-10-01.json",
                 "fingerprints/2026-10-01.json", "allocation_audit/2026-10-01.json",
                 "pipeline_runs/2026-10-01.json"):
        assert kept in left


def test_the_newest_performance_document_is_never_removed(tmp_path):
    perf = tmp_path / "prediction_performance"
    perf.mkdir()
    (perf / "2026-09-01.json").write_text("x")
    K.run_ledger_keep(str(tmp_path), TODAY, environ={K.APPLY_ENV: "1"})
    assert (perf / "2026-09-01.json").exists()


def test_the_keep_window_is_configurable_and_a_bad_value_is_the_default(tmp_path):
    root = _ledger(tmp_path)
    assert K.run_ledger_keep(str(root), TODAY, environ={K.DAYS_ENV: "45"})["files"] == 1   # only the tmp
    assert K.run_ledger_keep(str(root), TODAY, environ={K.DAYS_ENV: "x"})["keep_days"] == K.DEFAULT_DAYS


def test_a_missing_ledger_reports_nothing(tmp_path):
    s = K.run_ledger_keep(str(tmp_path / "none"), TODAY, environ={})
    assert s["files"] == 0 and s["folders_mb"] == {}
