"""Standing method — which entries the standing is computed from.

Rule amendment of 2026-09-04 (published before enabled, applied forward):

  SN21_STANDING_MODE = absolute          (default) each entry is the score
                                          itself, read from the standing
                                          ledger — the rule in force since
                                          launch, byte-for-byte.
  SN21_STANDING_MODE = episode_relative   each entry is the score MINUS the
                                          mean score of every miner scored on
                                          the same (episode, horizon), read
                                          from the published receipts.

WHY RELATIVE. The absolute mean rewards the mix of changes a miner happened
to be scored on as much as its accuracy: a model that is strong on the
commonest change type and answers everything else at the field level looks
best on a board whose evidence is mostly that type. Measured against the
field on the SAME change, difficulty and type mix cancel: the standing says
how far above or below everyone else a model was on identical episodes.

WHY FROM RECEIPTS. A ledger entry carries score, weight and day only. The
receipt for a settle day carries every miner's score on every (episode,
horizon) that settled that day — the field mean is one line of arithmetic
over the same public document a miner verifies with, so the relative
standing is reproducible from the mirror alone. Nothing in the ledger
changes; the absolute mode still reads it.

ABSENCE. An uncovered episode enters at the published floor (0.0). In
relative terms that is the floor minus the field: F below the field, where
F is the mean of the field means inside the window. Cancellations apply
exactly as in the ledger path (up to `missed` entries per day and hotkey).

Pure except for reading the ledger directory; parsing a window of receipts
is memoised per (root, as_of, window, files) because several steps of one
run read the standings.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta

from hope.scoring import standing_ledger
from hope.scoring.daily_score_flow import horizon_entry_weight
from hope.scoring.episode_average import (
    DEFAULT_WINDOW_DAYS,
    ScoredEpisode,
    half_life_from_env,
    prior_mass_from_env,
)

MODE_ENV = "SN21_STANDING_MODE"
MODE_ABSOLUTE = "absolute"
MODE_EPISODE_RELATIVE = "episode_relative"
MODES = (MODE_ABSOLUTE, MODE_EPISODE_RELATIVE)

PENALTY_ENTRY_WEIGHT = 1.0      # mirrors absence_penalty.PENALTY_ENTRY_WEIGHT
PENALTY_SCORE = 0.0             # the published floor


def standing_mode(environ=os.environ) -> str:
    v = (environ.get(MODE_ENV) or "").strip().lower()
    return v if v in MODES else MODE_ABSOLUTE


def relative_enabled(environ=os.environ) -> bool:
    return standing_mode(environ) == MODE_EPISODE_RELATIVE


def method_params(environ=os.environ) -> dict:
    """The published parameters in force, for audits and reports."""
    return {
        "mode": standing_mode(environ),
        "half_life_days": half_life_from_env(environ),
        "prior_mass": prior_mass_from_env(environ),
        "window_days": DEFAULT_WINDOW_DAYS,
    }


# ---- receipts -> relative entries -------------------------------------------

_CACHE: dict = {}


def _receipt_files(root: str, as_of: date, window_days: int) -> list[tuple[date, str]]:
    d = os.path.join(root, "receipts")
    if not os.path.isdir(d):
        return []
    cutoff = as_of - timedelta(days=window_days)
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            day = date.fromisoformat(fn[:-5])
        except ValueError:
            continue
        if cutoff <= day <= as_of:
            out.append((day, os.path.join(d, fn)))
    return out


def _entries_of(path: str) -> list[dict]:
    with open(path) as f:
        env = json.load(f)
    doc = env.get("document", env) if isinstance(env, dict) else {}
    return list((doc.get("metrics") or {}).get("entries") or [])


def field_means(entries: list[dict]) -> dict[tuple[str, int], float]:
    """Mean score per (episode, horizon) over every miner scored on it."""
    acc: dict[tuple[str, int], list[float]] = defaultdict(list)
    for e in entries:
        try:
            acc[(str(e["episode_id"]), int(e["horizon_days"]))].append(float(e["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def load_relative_entries(root: str, as_of: date,
                          window_days: int = DEFAULT_WINDOW_DAYS,
                          ) -> dict[str, list[ScoredEpisode]]:
    """hotkey -> relative ScoredEpisodes in the window, from receipts plus
    the absence-penalty log net of cancellations."""
    files = _receipt_files(root, as_of, window_days)
    sig = (root, as_of.isoformat(), window_days,
           tuple((p, os.path.getmtime(p)) for _, p in files),
           _penalty_signature(root))
    hit = _CACHE.get("k")
    if hit and hit[0] == sig:
        return {hk: list(v) for hk, v in hit[1].items()}

    out: dict[str, list[ScoredEpisode]] = defaultdict(list)
    all_means: list[float] = []
    cutoff = as_of - timedelta(days=window_days)
    for day, path in files:
        entries = _entries_of(path)
        means = field_means(entries)
        all_means.extend(means.values())
        for e in entries:
            try:
                key = (str(e["episode_id"]), int(e["horizon_days"]))
                score = float(e["score"])
                miner = str(e["miner"])
            except (KeyError, TypeError, ValueError):
                continue
            scored_on = day
            fo = e.get("finalized_on")
            if fo:
                try:
                    scored_on = date.fromisoformat(str(fo)[:10])
                except ValueError:
                    pass
            if scored_on < cutoff or scored_on > as_of:
                continue
            out[miner].append(ScoredEpisode(
                score=score - means[key], scored_on=scored_on,
                weight=horizon_entry_weight(key[1])))
    field_level = (sum(all_means) / len(all_means)) if all_means else 0.0
    absence_value = PENALTY_SCORE - field_level
    for hk, day, missed in _net_penalties(root, as_of, window_days):
        out[hk].extend(ScoredEpisode(score=absence_value, scored_on=day,
                                     weight=PENALTY_ENTRY_WEIGHT)
                       for _ in range(missed))
    result = {hk: v for hk, v in out.items() if v}
    _CACHE["k"] = (sig, {hk: list(v) for hk, v in result.items()})
    return result


def _penalty_path(root: str) -> str:
    return os.path.join(standing_ledger.standing_dir(root), "_absence_penalties.jsonl")


def _penalty_signature(root: str):
    p = _penalty_path(root)
    c = standing_ledger._cancellations_path(root)
    return tuple((q, os.path.getmtime(q)) for q in (p, c) if os.path.exists(q))


def _net_penalties(root: str, as_of: date, window_days: int):
    """(hotkey, day, missed) per penalty record inside the window, net of
    cancellations for that (day, hotkey)."""
    p = _penalty_path(root)
    if not os.path.exists(p):
        return []
    cancelled: dict[tuple[str, str], int] = defaultdict(int)
    for c in standing_ledger.load_cancellations(root):
        cancelled[(str(c.get("day")), str(c.get("hotkey")))] += int(c.get("missed") or 0)
    cutoff = as_of - timedelta(days=window_days)
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            try:
                day = date.fromisoformat(str(rec["day"]))
                hk = str(rec["hotkey"])
                missed = int(rec.get("missed") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if day < cutoff or day > as_of:
                continue
            key = (day.isoformat(), hk)
            take = min(missed, cancelled.get(key, 0))
            if take:
                cancelled[key] -= take
                missed -= take
            if missed > 0:
                out.append((hk, day, missed))
    return out


def load_standing_entries(root: str, as_of: date, environ=os.environ,
                          window_days: int = DEFAULT_WINDOW_DAYS,
                          ) -> dict[str, list[ScoredEpisode]]:
    """The entries every ranking consumer must read: ledger (absolute) or
    receipts (episode-relative), by the published mode."""
    if relative_enabled(environ):
        return load_relative_entries(root, as_of, window_days)
    return standing_ledger.load_entries(root, as_of=as_of, window_days=window_days)
