"""The operator's private copy of every model while it runs.

A model the subnet runs must stay public and pullable (SN21_MODEL_PRIVACY.md),
but the miner can still delete or close the image. Two things then break:
the check that refuses a removed model coming back as a copy needs to run the
removed model again on each new held-out set, and a release copies the model
on the day it is released. Both pulled from the miner's image.

So each run, after the weights, copies the models it ran into a PRIVATE
repository of the operator's, by digest (hope.privacy.mirror). The probe and
the release copy then read our copy first and fall back to the miner's image.
Nothing in this repository is ever published: a release still copies to the
public repository on the day it is released, and only then.

Switches (unset = the step does nothing):
  SN21_MODEL_KEEP            the private repository, for example
                             ghcr.io/adtaoai/sn21-kept. Never the public
                             release repository or a path inside it.
  SN21_MODEL_KEEP_AUTH       {"username": ..., "password": ...} that may push to
                             it and read it (unset: SN21_RELEASE_MIRROR_AUTH).
  SN21_MODEL_KEEP_PER_DAY    most copies a run makes (default 20).
  SN21_MODEL_KEEP_MINUTES    and the time it may spend on them (default 45).

PRIVATE OR NOTHING. Before copying anything the step asks GitHub for the
package's visibility and copies only when the answer is "private". A package
that doesn't exist yet, any other registry, or an answer it can't read means
nothing is copied: the package is created and set private by hand first.
Credentials are only ever sent for this repository (hope.backtest.oci_pull),
never for a miner's image.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KEEP_ENV = "SN21_MODEL_KEEP"
KEEP_AUTH_ENV = "SN21_MODEL_KEEP_AUTH"
PER_DAY_ENV = "SN21_MODEL_KEEP_PER_DAY"
MINUTES_ENV = "SN21_MODEL_KEEP_MINUTES"
KEPT_FILE = os.path.join("privacy", "kept.json")
DEFAULT_PER_DAY = 20
DEFAULT_MINUTES = 45


def target(environ=os.environ) -> str | None:
    """The private repository, or None when unset or unsafe (the public
    release repository, or a path inside it)."""
    from hope.privacy.release_step import MIRROR_ENV
    keep = (environ.get(KEEP_ENV) or "").strip().rstrip("/")
    public = (environ.get(MIRROR_ENV) or "").strip().rstrip("/")
    if not keep or "@" in keep or ":" in keep.split("/")[-1]:
        return None
    if public and (keep == public or keep.startswith(public + "/") or public.startswith(keep + "/")):
        return None
    return keep


def credentials(environ=os.environ) -> tuple[str, str] | None:
    from hope.privacy.release_step import MIRROR_AUTH_ENV
    for name in (KEEP_AUTH_ENV, MIRROR_AUTH_ENV):
        try:
            cfg = json.loads(environ.get(name) or "{}")
        except ValueError:
            continue
        if isinstance(cfg, dict) and cfg.get("username") and cfg.get("password"):
            return str(cfg["username"]), str(cfg["password"])
    return None


def _github_visibility(repo_ref: str, token: str, opener=None) -> str | None:
    """'private', 'public', 'internal', or None (not ghcr.io, no such package,
    or no answer)."""
    host, _, path = repo_ref.partition("/")
    if host != "ghcr.io" or "/" not in path:
        return None
    org, _, name = path.partition("/")
    url = (f"https://api.github.com/orgs/{urllib.parse.quote(org)}/packages/container/"
           f"{urllib.parse.quote(name, safe='')}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as resp:
            return (json.load(resp) or {}).get("visibility")
    except (urllib.error.URLError, ValueError, OSError):
        return None


def is_private(environ=os.environ, opener=None) -> tuple[bool, str]:
    keep, auth = target(environ), credentials(environ)
    if not keep:
        return False, f"{KEEP_ENV} not set, or it names the public release repository"
    if not auth:
        return False, "no credentials for the kept repository"
    seen = _github_visibility(keep, auth[1], opener)
    if seen != "private":
        return False, f"{keep} visibility is {seen or 'unknown'}, not private: nothing copied"
    return True, "private"


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load(root: str) -> dict:
    """digest -> {"target", "source", "kept_at"}"""
    return _read(os.path.join(root, KEPT_FILE), {})


def kept_ref(root: str, digest: str, environ=os.environ) -> str | None:
    """`<private repo>@<digest>` when we hold a copy of it, else None."""
    from hope.backtest.unchanged_models import bare_digest
    dg = bare_digest(digest) or digest
    entry = load(root).get(dg)
    keep = target(environ)
    if entry and keep and str(entry.get("target", "")).startswith(keep + "@"):
        return entry["target"]
    return None


def _save(root: str, kept: dict) -> None:
    path = os.path.join(root, KEPT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(kept, f, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def due(root: str) -> list[tuple[str, str]]:
    """(digest, pinned image ref) of the models the latest run ran that we hold
    no copy of: the one running unchanged the longest first (closest to its 40
    days), then by digest."""
    from hope.backtest import shadow
    from hope.backtest import unchanged_models as U
    days = shadow.shadow_days(root)
    if not days:
        return []
    ran = U._ran_on(root, days[-1])
    record = U.load(root) or {}
    since = {hk: (e or {}).get("since") or "9999" for hk, e in (record.get("hotkeys") or {}).items()}
    kept = load(root)
    out: dict = {}
    for hk, ref in ran.items():
        dg = U.bare_digest(ref)
        if not dg or dg in kept or "@" not in str(ref):
            continue
        first = since.get(hk, "9999")
        if dg not in out or first < out[dg][0]:
            out[dg] = (first, str(ref))
    return [(dg, ref) for dg, (_, ref) in sorted(out.items(), key=lambda kv: (kv[1][0], kv[0]))]


def run_keep_step(root: str, environ=os.environ, mirror_fn=None, opener=None,
                  clock=time.monotonic) -> dict:
    """Copy what is due, within the day's count and time. Never raises."""
    if not (environ.get(KEEP_ENV) or "").strip():
        return {"skipped": f"{KEEP_ENV} not set"}
    ok, why = is_private(environ, opener)
    if not ok:
        return {"skipped": why}
    keep, auth = target(environ), credentials(environ)
    try:
        per_day = int(environ.get(PER_DAY_ENV) or DEFAULT_PER_DAY)
        budget = 60 * float(environ.get(MINUTES_ENV) or DEFAULT_MINUTES)
    except ValueError:
        per_day, budget = DEFAULT_PER_DAY, 60.0 * DEFAULT_MINUTES
    if mirror_fn is None:
        from hope.privacy.mirror import mirror_image as mirror_fn
    todo = due(root)
    kept = load(root)
    start = clock()
    copied, failed = 0, []
    for dg, ref in todo:
        if copied + len(failed) >= per_day or clock() - start > budget:
            break
        repo = ref.partition("@")[0]
        try:
            res = mirror_fn(repo, dg, keep, source_credentials=(), target_credentials=auth)
        except Exception as exc:                       # retried next run
            failed.append({"digest": dg, "error": str(exc)[:200]})
            continue
        kept[dg] = {"target": res["target"], "source": ref,
                    "kept_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _save(root, kept)
        copied += 1
    return {"copied": copied, "failed": failed, "left": max(0, len(todo) - copied - len(failed)),
            "kept": len(kept), "repository": keep}
