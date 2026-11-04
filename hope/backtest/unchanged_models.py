"""Models that never change are removed after 40 days (Sir Rob, KzoNlmEF, 2 Nov 2026: option a).

A hotkey that runs the same model (the same image digest) for 40 days in a row,
counted from the first run day that model delivered predictions, is not run
from day 41: no score, no pay, no absence charge. The hotkey stays registered;
to come back it commits a new model, which goes through the admission gate like
any new model. Off until the notice sets the date (SN21_UNCHANGED_REMOVAL_FROM,
YYYY-MM-DD).

The record lives in the ledger (registry/_digest_since.json): hotkey ->
{"digest": sha256:<hex>, "since": the first run day of that digest}. A new
digest restarts the count. It is updated every day from the models that ran,
whether or not the switch is on, so the counts are right on the switch date and
the public status feed can show each miner's days before then. The first time
it is missing it is seeded from the shadow ledger, oldest day first.

Pure apart from load/save/seed: `update`, `standing` and `to_remove` take
everything they need as arguments.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

SWITCH_ENV = "SN21_UNCHANGED_REMOVAL_FROM"
RECORD = os.path.join("registry", "_digest_since.json")
MAX_DAYS = 40


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


def update(record: dict, ran: dict, day: str) -> dict:
    """Fold one run day in. ran: hotkey -> image ref of every model that delivered
    predictions that day. A hotkey whose digest changed starts again at `day`;
    hotkeys that did not run keep their entry (a missed day does not reset it)."""
    out = dict(record or {})
    for hk, ref in (ran or {}).items():
        dg = bare_digest(ref)
        if not dg:
            continue
        cur = out.get(hk) or {}
        if cur.get("digest") != dg:
            out[hk] = {"digest": dg, "since": str(day)[:10]}
    return out


def seed_from_ledger(root: str) -> dict:
    """The record rebuilt from the shadow ledger, oldest day first.

    Only models that delivered predictions count, as in `update`."""
    from hope.backtest import shadow

    record: dict = {}
    for day in shadow.shadow_days(root):
        d = shadow.shadow_dir(root, day)
        ran = {}
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not fn.endswith(".jsonl"):
                continue
            hk = fn[:-6]
            last = shadow.last_record(root, day, hk) or {}
            if last.get("image_digest") and (last.get("predictions_out") or 0) > 0:
                ran[hk] = last["image_digest"]
        record = update(record, ran, day)
    return record


def remember(root: str, ran: dict, as_of: str) -> dict:
    """Load (or seed), fold today in, save. Returns the record."""
    record = load(root)
    if record is None:
        record = seed_from_ledger(root)
    record = update(record, ran, as_of)
    save(root, record)
    return record


def standing(entry: dict | None, as_of: str) -> dict | None:
    """What the status feed shows for one hotkey: the digest's first run day,
    days unchanged counting that day as 1, and the first day it is not run."""
    if not entry or not entry.get("since"):
        return None
    since = date.fromisoformat(entry["since"])
    today = date.fromisoformat(str(as_of)[:10])
    return {"digest_since": since.isoformat(),
            "days_unchanged": max(0, (today - since).days + 1),
            "removal_on": (since + timedelta(days=MAX_DAYS)).isoformat()}


def to_remove(record: dict, active: dict, as_of: str) -> dict:
    """hotkey -> standing for every active hotkey whose current digest has run
    for MAX_DAYS days or more by `as_of`. A hotkey whose active digest is not
    the recorded one (a new model not yet run) is never removed."""
    out = {}
    for hk, model in (active or {}).items():
        entry = (record or {}).get(hk)
        ref = getattr(model, "image_digest", model)
        if not entry or entry.get("digest") != bare_digest(ref):
            continue
        st = standing(entry, as_of)
        if st and str(as_of)[:10] >= st["removal_on"]:
            out[hk] = st
    return out
