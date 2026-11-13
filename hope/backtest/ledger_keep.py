"""What the executor's ledger disk holds, and the files it no longer needs.

The ledger disk fills about 0.4 GB a day (27.1 GB free on 11 Nov, 26.1 GB on
13 Nov, of 45 GB) and nothing removed anything. Each run now reports every
top-level folder's size, so the growth is measured on the disk itself, and
what this step would remove.

Removed only when SN21_LEDGER_KEEP_APPLY=1 (otherwise a dry run), and only
files no step reads again:
  - prediction_performance/<day>.json older than SN21_LEDGER_KEEP_DAYS (14):
    each is the cumulative document of its day, and only the newest is read
    (the mirror publishes it; every day is recomputable from the receipts).
    The newest is never removed.
  - a *.tmp file more than a day old: an atomic write that never finished.

Nothing else: receipts, shadow predictions, fingerprints, standing indexes,
audits and run records are read again (standings, settle, the 40-day record,
the release replay, copy precedence) and stay until each reader's window is
checked.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

APPLY_ENV = "SN21_LEDGER_KEEP_APPLY"
DAYS_ENV = "SN21_LEDGER_KEEP_DAYS"
DEFAULT_DAYS = 14
TMP_AGE_S = 24 * 3600


def _size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
    return total


def folder_sizes(root: str) -> dict:
    """top-level folder (or file) -> MB, largest first."""
    out = {}
    for entry in os.scandir(root) if os.path.isdir(root) else []:
        size = _size(entry.path) if entry.is_dir(follow_symlinks=False) else entry.stat().st_size
        out[entry.name] = round(size / 1e6, 1)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def prunable(root: str, today: date, keep_days: int, now: float | None = None) -> list[tuple[str, int]]:
    """(path, bytes) of every file the rule would remove."""
    now = time.time() if now is None else now
    out = []
    perf = os.path.join(root, "prediction_performance")
    if os.path.isdir(perf):
        days = sorted(n[:-5] for n in os.listdir(perf) if n.endswith(".json"))
        floor = (today - timedelta(days=keep_days)).isoformat()
        for d in days[:-1]:                       # never the newest
            if d < floor:
                p = os.path.join(perf, f"{d}.json")
                out.append((p, os.path.getsize(p)))
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".tmp"):
                p = os.path.join(dirpath, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if now - st.st_mtime > TMP_AGE_S:
                    out.append((p, st.st_size))
    return out


def run_ledger_keep(root: str, today: date, environ=os.environ) -> dict:
    """Report sizes and remove (or, by default, only count) what the rule allows."""
    try:
        keep_days = max(1, int(environ.get(DAYS_ENV) or DEFAULT_DAYS))
    except ValueError:
        keep_days = DEFAULT_DAYS
    apply = (environ.get(APPLY_ENV) or "").strip() == "1"
    files = prunable(root, today, keep_days)
    removed, failed = 0, 0
    if apply:
        for p, _ in files:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                failed += 1
    return {"mode": "apply" if apply else "dry run", "keep_days": keep_days,
            "files": len(files), "mb": round(sum(b for _, b in files) / 1e6, 1),
            "removed": removed, "failed": failed, "folders_mb": folder_sizes(root)}
