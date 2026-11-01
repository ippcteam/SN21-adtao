"""Each stage boundary in the run record says when it was reached (1 Nov 2026)."""
from scripts import run_daily_pipeline as rdp


def test_stage_boundary_carries_seconds_since_start(monkeypatch):
    monkeypatch.setattr(rdp, "container_memory_mb", lambda: None)
    monkeypatch.setattr(rdp, "_RUN_STARTED", 1000.0)
    monkeypatch.setattr(rdp.time, "time", lambda: 1754.4)
    rec = {}
    rdp.note_memory("shadow", rec)
    assert rec["memory_mb"]["shadow"]["at_s"] == 754


def test_no_stamp_outside_a_run(monkeypatch):
    monkeypatch.setattr(rdp, "container_memory_mb", lambda: None)
    monkeypatch.setattr(rdp, "_RUN_STARTED", None)
    rec = {}
    rdp.note_memory("resolve", rec)
    assert "at_s" not in rec["memory_mb"]["resolve"]
