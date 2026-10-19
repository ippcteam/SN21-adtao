"""Each hotkey's last admitted model, so it keeps running while a new one waits.

The chain holds only a hotkey's LATEST model commitment. When a miner commits a
new model, the registry sees that digest alone: until it is admitted nothing
runs for the hotkey, and if it is rejected nothing runs at all, so every one of
those days is a full missed basket. From the switch date
(SN21_KEEP_LAST_ADMITTED_FROM, YYYY-MM-DD, decided 18 Oct 2026), a hotkey whose
current commitment is waiting for admission or was rejected keeps running the
last model of its own that was admitted and ran. The new model takes over the
day it is admitted. The model that runs is always an admitted one.

The record lives in the ledger (registry/_last_admitted.json): hotkey ->
{"image_digest": the runnable repo@sha256:..., "since": run date}. It is
updated every day from the models that ran, whether or not the switch is on, so
it is complete on the switch date. The first time it is missing it is seeded
from the shadow ledger.
"""

from __future__ import annotations

import json
import os
from datetime import date

from hope.backtest.shadow import ShadowModel

SWITCH_ENV = "SN21_KEEP_LAST_ADMITTED_FROM"
RECORD = os.path.join("registry", "_last_admitted.json")
SEED_DAYS = 60


def enabled(as_of: str, environ=os.environ) -> bool:
    """True from the switch date on; off when unset or unreadable."""
    raw = (environ.get(SWITCH_ENV) or "").strip()
    if not raw:
        return False
    try:
        return date.fromisoformat(str(as_of)[:10]) >= date.fromisoformat(raw)
    except ValueError:
        return False


def bare_digest(ref: str | None) -> str | None:
    """sha256:<hex> from either spelling (repo@sha256:... or sha256:...)."""
    if not ref:
        return None
    s = str(ref)
    return s.split("@", 1)[1] if "@" in s else s


def _path(root: str) -> str:
    return os.path.join(root, RECORD)


def load(root: str) -> dict | None:
    """The record, or None when there is none yet (or it is unreadable)."""
    try:
        with open(_path(root)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save(root: str, record: dict) -> None:
    path = _path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def seed_from_ledger(root: str, admitted: set, days: int = SEED_DAYS) -> dict:
    """hotkey -> the newest model it ran that is admitted, from the shadow ledger.

    Reads the last `days` basket days, newest first; the first admitted model
    found for a hotkey wins. Only models that delivered predictions count."""
    from hope.backtest import shadow

    record: dict = {}
    for day in list(reversed(shadow.shadow_days(root)))[:days]:
        d = shadow.shadow_dir(root, day)
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not fn.endswith(".jsonl"):
                continue
            hk = fn[:-6]
            if hk in record:
                continue
            last = shadow.last_record(root, day, hk) or {}
            ref = last.get("image_digest")
            if ref and (last.get("predictions_out") or 0) > 0 and bare_digest(ref) in admitted:
                record[hk] = {"image_digest": ref, "since": day}
    return record


def remember(root: str, active: dict, as_of: str, admitted: set) -> dict:
    """Merge today's admitted models into the record and save it.

    Hotkeys not active today keep their entry: that entry is the point."""
    record = load(root)
    if record is None:
        record = seed_from_ledger(root, admitted)
    for hk, model in (active or {}).items():
        ref = getattr(model, "image_digest", None)
        if ref and bare_digest(ref) in admitted:
            record[hk] = {"image_digest": ref, "since": str(as_of)[:10]}
    save(root, record)
    return record


def fallbacks(registry: dict, record: dict, admitted: set, as_of: str) -> dict:
    """hotkey -> ShadowModel of its last admitted model, for every hotkey whose
    current commitment is waiting or rejected and that has one still admitted."""
    out: dict = {}
    for hk, waiting in (registry.get("pending_admission") or {}).items():
        prev = (record or {}).get(hk) or {}
        ref = prev.get("image_digest")
        if not ref or bare_digest(ref) not in admitted:
            continue
        if bare_digest(ref) == bare_digest(waiting):
            continue
        out[hk] = ShadowModel(hotkey=hk, image_digest=ref, admitted_at=str(as_of)[:10])
    return out
