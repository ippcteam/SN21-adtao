"""Models that never change are removed after 40 days (Sir Rob, KzoNlmEF, 2 Nov 2026: option a).

A hotkey that runs the same model (the same image digest) for 40 days in a row,
counted from the first run day that model delivered predictions, is not run
from day 41: no score, no pay, no absence charge. The hotkey stays registered;
to come back it commits a new model, which goes through the admission gate like
any new model. A removed digest is never run again, by any hotkey. Off until
the notice sets the date (SN21_UNCHANGED_REMOVAL_FROM, YYYY-MM-DD).

The record lives in the ledger (registry/_digest_since.json):
  through   the last shadow day folded in
  hotkeys   hotkey -> {"digest": sha256:<hex>, "since": first run day of it}
  digests   digest -> the first basket day it delivered predictions, on any hotkey
  removed   digest -> {"hotkey", "removed_on", "image_ref"}
  by_day    run day -> hotkeys not run that day (last 14 days)
It is brought up to date from the shadow ledger on every read, whether or not
the switch is on, so the counts are right on the switch date and the public
status feed can show each miner's days before then. The count is per digest:
a new digest starts its own count, and a digest run before keeps the count it
started with (12 Nov 2026, decided with Khurram: SN21_REWARDS.md counts "the
same image digest" from its first daily run, and a per-hotkey restart let a
miner switch between two digests and never reach day 41). A day a hotkey did
not run does not reset it.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

SWITCH_ENV = "SN21_UNCHANGED_REMOVAL_FROM"
RECORD = os.path.join("registry", "_digest_since.json")
MAX_DAYS = 40


def switch_date(environ=os.environ) -> str | None:
    """The date the rule applies from (YYYY-MM-DD), or None when unset or unreadable."""
    raw = (environ.get(SWITCH_ENV) or "").strip()
    try:
        return date.fromisoformat(raw).isoformat() if raw else None
    except ValueError:
        return None


def enabled(as_of: str, environ=os.environ) -> bool:
    """True from the switch date on; off when unset or unreadable."""
    start = switch_date(environ)
    try:
        return bool(start) and date.fromisoformat(str(as_of)[:10]) >= date.fromisoformat(start)
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


def empty() -> dict:
    return {"through": None, "hotkeys": {}, "digests": {}, "removed": {}, "by_day": {}}


def load(root: str) -> dict | None:
    """The record, or None when there is none yet (or it is unreadable)."""
    try:
        with open(_path(root)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "hotkeys" in data else None
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
    predictions that day."""
    out = {**empty(), **(record or {})}
    hotkeys = dict(out["hotkeys"])
    digests = dict(out.get("digests") or {})
    d = str(day)[:10]
    for hk, ref in (ran or {}).items():
        dg = bare_digest(ref)
        if not dg:
            continue
        first = min(digests.get(dg) or d, d)
        digests[dg] = first
        if (hotkeys.get(hk) or {}).get("digest") != dg:
            # A digest run before, by this hotkey or another, keeps its first day.
            hotkeys[hk] = {"digest": dg, "since": first}
    out["hotkeys"] = hotkeys
    out["digests"] = digests
    out["through"] = max(filter(None, (out.get("through"), str(day)[:10])))
    return out


def _ran_on(root: str, day: str) -> dict:
    """hotkey -> image ref of each model that delivered predictions on `day`."""
    from hope.backtest import shadow

    d = shadow.shadow_dir(root, day)
    ran = {}
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".jsonl"):
            continue
        last = shadow.last_record(root, day, fn[:-6]) or {}
        if last.get("image_digest") and (last.get("predictions_out") or 0) > 0:
            ran[fn[:-6]] = last["image_digest"]
    return ran


def refresh(root: str, save_it: bool = True) -> dict:
    """The record, with every shadow day after `through` folded in, oldest first."""
    from hope.backtest import shadow

    record = load(root) or empty()
    if "digests" not in record:
        # A record from before the count was per digest: rebuild the counts from
        # every shadow day, so a digest a hotkey went back to carries its first
        # day. What was removed, and when, is kept as it is.
        record = {**empty(), "removed": record.get("removed") or {},
                  "by_day": record.get("by_day") or {}}
    through = record.get("through")
    for day in shadow.shadow_days(root):
        if through is None or day > through:
            record = update(record, _ran_on(root, day), day)
    if save_it:
        save(root, record)
    return record


def standing(entry: dict | None, as_of: str) -> dict | None:
    """What the status feed shows for one hotkey: the digest's first run day,
    days unchanged counting that day as 1, and the first run day it is not run.

    `since` is the BASKET day the digest first delivered predictions on; the run
    that served it is the next day (run D serves BD-(D-1)), and every date here
    is a run day, as `as_of` is. Counting from the basket day gave each model
    39 runs and put its first day not run one day before the published rule
    (first run day + 40; found 12 Nov 2026, before the first removals)."""
    if not entry or not entry.get("since"):
        return None
    first_run = date.fromisoformat(entry["since"]) + timedelta(days=1)
    today = date.fromisoformat(str(as_of)[:10])
    return {"digest_since": first_run.isoformat(),
            "days_unchanged": max(0, (today - first_run).days + 1),
            "removal_on": (first_run + timedelta(days=MAX_DAYS)).isoformat()}


def to_remove(record: dict, active: dict, as_of: str) -> dict:
    """hotkey -> reason for every active hotkey not to run on `as_of`: its current
    digest has run MAX_DAYS days or more ("unchanged"), or that digest was
    removed before, from any hotkey ("removed_digest"). A hotkey whose active
    digest has not run yet is otherwise never removed."""
    record = record or empty()
    removed = record.get("removed") or {}
    out = {}
    for hk, model in (active or {}).items():
        dg = bare_digest(getattr(model, "image_digest", model))
        if dg in removed:
            out[hk] = "removed_digest"
            continue
        entry = (record.get("hotkeys") or {}).get(hk)
        if not entry or entry.get("digest") != dg:
            continue
        st = standing(entry, as_of)
        if st and str(as_of)[:10] >= st["removal_on"]:
            out[hk] = "unchanged"
    return out


def apply(root: str, active: dict, as_of: str, environ=os.environ) -> dict:
    """The run's step: refresh the record and, when the switch is on, mark the
    day's removals (their digests stay removed). Returns hotkey -> reason;
    {} while the switch is off."""
    record = refresh(root, save_it=False)
    out = to_remove(record, active, as_of) if enabled(as_of, environ) else {}
    removed = dict(record.get("removed") or {})
    for hk in out:
        dg = bare_digest(getattr(active[hk], "image_digest", active[hk]))
        removed.setdefault(dg, {"hotkey": hk, "removed_on": str(as_of)[:10],
                                "image_ref": str(getattr(active[hk], "image_digest", active[hk]))})
    record["removed"] = removed
    # The day's list too, so a hotkey refused for committing a removed digest
    # it never ran is also not charged or seated that day. Last 14 days kept.
    by_day = {d: h for d, h in (record.get("by_day") or {}).items()
              if d > (date.fromisoformat(str(as_of)[:10]) - timedelta(days=14)).isoformat()}
    if out:
        by_day[str(as_of)[:10]] = sorted(out)
    record["by_day"] = by_day
    save(root, record)
    return out


def removed_hotkeys(root: str, day: str) -> frozenset:
    """Hotkeys whose last recorded model was removed on or before `day`: they are
    neither charged for absence nor seated. A hotkey that has since run a new
    model is not in it."""
    record = load(root) or empty()
    removed = record.get("removed") or {}
    d = str(day)[:10]
    return frozenset(hk for hk, e in (record.get("hotkeys") or {}).items()
                     if e.get("digest") in removed and removed[e["digest"]]["removed_on"] <= d
                     ) | frozenset((record.get("by_day") or {}).get(d) or ())


# ---- a removed model re-added under a new digest ------------------------------
#
# A removed model's exact digest is refused above. A rebuilt copy has a new
# digest, so it is compared by what it predicts: each removed model is run once
# on the admission corpus in force (a "probe", re-run when the corpus moves),
# and every new model's admission predictions on the same corpus are tested
# against those with the copy check the one-payer rule uses (SN21_LINEAGE_*).
# A match is refused at admission as the same model.

PROBES_DIR = os.path.join("registry", "_removed_probes")
PROBES_PER_DAY_ENV = "SN21_UNCHANGED_PROBES_PER_DAY"
DEFAULT_PROBES_PER_DAY = 4


def _probe_dir(root: str, corpus_sha: str) -> str:
    return os.path.join(root, PROBES_DIR, str(corpus_sha)[:16])


def _hex(digest: str) -> str:
    return str(digest).split("sha256:", 1)[-1]


def save_probe(root: str, corpus_sha: str, digest: str, predictions: dict) -> None:
    d = _probe_dir(root, corpus_sha)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _hex(digest) + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(predictions, f, sort_keys=True)
    os.replace(path + ".tmp", path)


def load_probes(root: str, corpus_sha: str) -> dict:
    """sha256:<hex> -> the removed model's predictions on this corpus."""
    d = _probe_dir(root, corpus_sha)
    out = {}
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if fn.endswith(".json"):
            try:
                with open(os.path.join(d, fn)) as f:
                    out["sha256:" + fn[:-5]] = json.load(f)
            except (OSError, ValueError):
                continue
    return out


def probes_due(root: str, corpus_sha: str, environ=os.environ) -> list[tuple[str, str]]:
    """(digest, image_ref) of removed models with no probe on this corpus yet,
    oldest removal first, at most SN21_UNCHANGED_PROBES_PER_DAY."""
    try:
        cap = int(environ.get(PROBES_PER_DAY_ENV) or DEFAULT_PROBES_PER_DAY)
    except ValueError:
        cap = DEFAULT_PROBES_PER_DAY
    have = set(load_probes(root, corpus_sha))
    removed = (load(root) or empty()).get("removed") or {}
    due = sorted(((e.get("removed_on") or "", dg, e.get("image_ref")) for dg, e in removed.items()
                  if dg not in have and e.get("image_ref")))
    return [(dg, ref) for _, dg, ref in due][:max(0, cap)]


def same_as_removed(predictions: dict, outcomes, probes: dict, params) -> dict | None:
    """The first removed model the new model's predictions match, with the
    numbers behind it, or None. outcomes: the corpus truth rows (the scale of
    each distance, as on the one-payer path)."""
    from dataclasses import asdict, is_dataclass

    from hope.scoring.duplication import pair_signals, same_lineage
    from hope.validator.daily_stream_weights import actuals_from_receipt

    rows = [asdict(o) if is_dataclass(o) else dict(o) for o in (outcomes or [])]
    actuals = actuals_from_receipt(rows)
    for dg, theirs in sorted((probes or {}).items()):
        sig = pair_signals(predictions or {}, theirs, actuals, delta=params.disagree_delta)
        if same_lineage(sig, params):
            return {"removed_digest": dg, "params_version": params.version,
                    "signals": asdict(sig)}
    return None
