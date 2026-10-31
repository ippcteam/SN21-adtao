"""The heartbeat carries how long the day's models took and their memory (31 Oct 2026).

On 30 Oct the spread (median 68 s, 95% under 864 s, 41 over 5 min, 14 over 10 min) had to be read from the
per-model records through the executor's shell."""
import importlib.util
import os

from hope.backtest.container_runner import RunResult
from hope.backtest.shadow import ShadowModel, run_shadow_day

spec = importlib.util.spec_from_file_location(
    "pipeline", os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "run_daily_pipeline.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def results(times):
    return {f"5Hotkey{i:03d}xxxxxxxx": {"ok": t < 900, "error": None if t < 900 else "timeout>900s: CPU time limit",
                                        "duration_s": t, "peak_rss_mb": 100.0 + i}
            for i, t in enumerate(times)}


def test_the_spread_counts_and_slowest_models():
    h = P.shadow_run_health(results([10, 20, 68, 120, 350, 650, 905.4, 905.3]))
    t = h["model_time_s"]
    assert (t["n"], t["p50"], t["max"]) == (8, 350.0, 905.4)
    assert (t["over_300s"], t["over_600s"], t["at_budget"]) == (4, 3, 2)
    assert t["slowest"][0] == ["5Hotkey0", 905.4] and len(t["slowest"]) == 5
    assert h["model_peak_rss_mb"]["max"] == 107.0


def test_runs_that_never_started_are_left_out():
    r = results([30, 40])
    r["5Pullfailedxxxx"] = {"ok": False, "error": "pull_failed: x", "duration_s": None, "peak_rss_mb": None}
    h = P.shadow_run_health(r)
    assert h["model_time_s"]["n"] == 2 and h["model_peak_rss_mb"]["n"] == 2


def test_no_timed_runs_gives_none():
    h = P.shadow_run_health({"a": {"ok": True}})
    assert h["model_time_s"] is None and h["model_peak_rss_mb"] is None


def test_the_shadow_summary_passes_time_and_memory_through(tmp_path):
    m = ShadowModel(hotkey="5Hk", image_digest="r@sha256:" + "0" * 64, admitted_at="2026-10-29")
    out = run_shadow_day("2026-10-31", [{"episode_id": "e1"}], [m],
                         lambda mm, eps: RunResult(ok=True, predictions_out=1, duration_s=12.5, peak_rss_mb=99.0),
                         str(tmp_path), workers=1)
    assert out["results"]["5Hk"]["duration_s"] == 12.5 and out["results"]["5Hk"]["peak_rss_mb"] == 99.0
