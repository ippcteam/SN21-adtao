"""The daily model-release step — docs/SN21_MODEL_PRIVACY.md.

After the day's weights are written, replay the release rule from its start
date to today, write the release ledger, and copy every newly released model
to the operator's public repository.

Switches (both unset = the step does nothing):
  SN21_MODEL_RELEASE_FROM    first run day the rule observes (YYYY-MM-DD).
                             Before it, or unset, nothing is computed.
  SN21_RELEASE_MIRROR        public repository for the copies, for example
                             ghcr.io/<org>/sn21-released. Unset = the ledger is
                             written but nothing is copied.
  SN21_RELEASE_MIRROR_AUTH   {"username": ..., "password": ...} that may push
                             to that repository.
  SN21_RELEASE_LIST_REPO /   where the public list of releases is kept, and a
  SN21_RELEASE_LIST_TOKEN    token that may write it (hope/privacy/release_list.py).

The ledger is recomputed in full every day from the run records (replay), so
it can never drift from what the records say, and anyone holding the same
records gets the same ledger. A copy that fails is retried the next day; the
release itself is not undone or delayed by it.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from hope.privacy.release_ledger import ReleaseParams, replay

RELEASE_FROM_ENV = "SN21_MODEL_RELEASE_FROM"
MIRROR_ENV = "SN21_RELEASE_MIRROR"
MIRROR_AUTH_ENV = "SN21_RELEASE_MIRROR_AUTH"

LEDGER_FILE = os.path.join("privacy", "release_ledger.json")
MIRRORED_FILE = os.path.join("privacy", "mirrored.json")


def release_from(environ=os.environ) -> date | None:
    raw = (environ.get(RELEASE_FROM_ENV) or "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _mirror_auth(environ) -> tuple[str, str] | None:
    try:
        cfg = json.loads(environ.get(MIRROR_AUTH_ENV) or "{}")
    except ValueError:
        return None
    if isinstance(cfg, dict) and cfg.get("username") and cfg.get("password"):
        return str(cfg["username"]), str(cfg["password"])
    return None


def _read(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def build_ledger(ledger_root: str, day: date, start: date,
                 params: ReleaseParams = ReleaseParams()) -> dict:
    """The release ledger as of `day`: every release since `start`, and every
    digest still closed with the day it closed."""
    state, releases, observed = replay(ledger_root, start, day, params)
    closed = sorted(
        ({"digest": dg, "closed_since": st.closed_since.isoformat(),
          "hotkeys": sorted(st.hotkeys)}
         for dg, st in state.digests.items() if st.released_on is None),
        key=lambda r: (r["closed_since"], r["digest"]))
    return {"rule": "docs/SN21_MODEL_PRIVACY.md", "from": start.isoformat(),
            "as_of": day.isoformat(), "observed_days": observed,
            "parameters": {"max_embargo_days": params.max_embargo_days,
                           "release_after_days_out": params.release_after_days_out},
            "releases": releases, "closed": closed}


def run_release_step(ledger_root: str, day: date, environ=os.environ,
                     mirror_fn=None) -> dict:
    """Write the ledger for `day` and copy what is due. Never raises: the
    caller logs the summary, and a failure here must not stop the run."""
    start = release_from(environ)
    if start is None:
        return {"skipped": f"{RELEASE_FROM_ENV} not set"}
    if day < start:
        return {"skipped": f"rule starts {start.isoformat()}"}
    ledger = build_ledger(ledger_root, day, start)
    _write(os.path.join(ledger_root, LEDGER_FILE), ledger)
    summary = {"observed_days": ledger["observed_days"],
               "closed": len(ledger["closed"]),
               "released": len(ledger["releases"]),
               "released_today": sum(1 for r in ledger["releases"]
                                     if r["released_on"] == day.isoformat())}

    from hope.privacy.release_list import publish_release_list
    target = (environ.get(MIRROR_ENV) or "").strip()
    if not target:
        summary["mirror"] = f"{MIRROR_ENV} not set"
        summary["public_list"] = publish_release_list(
            ledger, _read(os.path.join(ledger_root, MIRRORED_FILE), {}), environ)
        return summary
    if mirror_fn is None:
        from hope.privacy.mirror import mirror_image as mirror_fn
    mirrored_path = os.path.join(ledger_root, MIRRORED_FILE)
    mirrored = _read(mirrored_path, {})
    auth = _mirror_auth(environ)
    done, failed = 0, []
    for r in ledger["releases"]:
        dg = r["digest"]
        if dg in mirrored:
            continue
        if not r.get("image_ref"):
            failed.append({"digest": dg, "error": "no source repository on record"})
            continue
        try:
            res = mirror_fn(r["image_ref"], dg, target, target_credentials=auth)
        except Exception as exc:                       # retried tomorrow
            failed.append({"digest": dg, "error": str(exc)[:200]})
            continue
        mirrored[dg] = {"target": res["target"], "source": f"{r['image_ref']}@{dg}",
                        "released_on": r["released_on"],
                        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _write(mirrored_path, mirrored)
        done += 1
    summary["mirror"] = {"copied": done, "failed": failed, "total": len(mirrored)}
    summary["public_list"] = publish_release_list(ledger, mirrored, environ)
    return summary
