"""The executor logs container memory split into programs and file cache (12 Oct 2026)."""
import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "rdp", os.path.join(os.path.dirname(__file__), "..", "scripts", "run_daily_pipeline.py"))
rdp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rdp)
MIB = 1024 * 1024


def test_cgroup_v2(tmp_path):
    (tmp_path / "memory.current").write_text(str(8000 * MIB))
    (tmp_path / "memory.stat").write_text(f"anon {3000 * MIB}\nfile {4800 * MIB}\nkernel 5\n")
    assert rdp.container_memory_mb(str(tmp_path)) == {"total": 8000, "anon": 3000, "file_cache": 4800}


def test_cgroup_v1(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "memory.usage_in_bytes").write_text(str(6000 * MIB))
    (tmp_path / "memory" / "memory.stat").write_text(f"cache {2500 * MIB}\nrss {3400 * MIB}\n")
    assert rdp.container_memory_mb(str(tmp_path)) == {"total": 6000, "anon": 3400, "file_cache": 2500}


def test_no_cgroup_files(tmp_path):
    assert rdp.container_memory_mb(str(tmp_path)) is None


def test_the_line_and_record_carry_the_split(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(rdp, "log", lines.append)
    monkeypatch.setattr(rdp, "container_memory_mb", lambda: {"total": 8000, "anon": 3000, "file_cache": 4800})
    rec = {}
    rdp.note_memory("mirror", rec)
    assert rec["memory_mb"]["mirror"]["container"]["file_cache"] == 4800
    assert "container=8000 MiB (programs 3000, file cache 4800)" in lines[0]
