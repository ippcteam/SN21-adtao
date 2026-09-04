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
    window_from_env,
)

MODE_ENV = "SN21_STANDING_MODE"
MODE_ABSOLUTE = "absolute"
MODE_EPISODE_RELATIVE = "episode_relative"
MODES = (MODE_ABSOLUTE, MODE_EPISODE_RELATIVE)

# The published effective date. The rule is announced first and applied
# from this day forward; with the env set ahead of time the switch happens
# on the date the miners were told, not on the day an operator edits a
# variable. Unset = apply as soon as the mode is set.
EFFECTIVE_FROM_ENV = "SN21_STANDING_EFFECTIVE_FROM"


def effective_from(environ=os.environ) -> date | None:
    raw = (environ.get(EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None

PENALTY_ENTRY_WEIGHT = 1.0      # mirrors absence_penalty.PENALTY_ENTRY_WEIGHT
PENALTY_SCORE = 0.0             # the published floor

# Champion promotion margin under a relative standing. The published test
# "leads by at least 5% (relative)" has no meaning for a standing that sits
# near zero and can be negative, so the relative mode publishes an ABSOLUTE
# lead instead (challenger >= champion + margin). Unset = the relative test.
PROMOTION_MARGIN_ABS_ENV = "SN21_PROMOTION_MARGIN_ABS"


# The weight curve's score threshold. Published as 0.0 for the absolute
# standing ("at threshold or above earns"). A RELATIVE standing sits at 0.0
# when a miner matches the field, so reading the same 0.0 there would pay
# only miners above the field and cut the earning set below the published
# twenty. Under the relative mode the threshold is therefore not applied
# (-1.0, below any achievable relative standing): the top twenty by standing
# earn the published shares exactly as the rewards doc states. Explicit env
# wins in either mode.
CURVE_THRESHOLD_ENV = "SN21_CURVE_SCORE_THRESHOLD"
RELATIVE_CURVE_THRESHOLD = -1.0


def curve_score_threshold(environ=os.environ, day: date | None = None) -> float:
    raw = (environ.get(CURVE_THRESHOLD_ENV) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return RELATIVE_CURVE_THRESHOLD if relative_enabled(environ, day) else 0.0


def promotion_margin_abs(environ=os.environ, day: date | None = None) -> float | None:
    """The absolute champion margin, in force only with the relative
    standing (it is the relative standing that makes the 5% test
    meaningless); before the effective date the published relative test
    applies unchanged."""
    if not relative_enabled(environ, day):
        return None
    try:
        v = float((environ.get(PROMOTION_MARGIN_ABS_ENV) or "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def standing_mode(environ=os.environ, day: date | None = None) -> str:
    """The mode IN FORCE on `day` (today when None): the configured mode,
    unless the effective date has not arrived, in which case absolute."""
    v = (environ.get(MODE_ENV) or "").strip().lower()
    mode = v if v in MODES else MODE_ABSOLUTE
    if mode != MODE_ABSOLUTE:
        start = effective_from(environ)
        if start is not None and (day or date.today()) < start:
            return MODE_ABSOLUTE
    return mode


def relative_enabled(environ=os.environ, day: date | None = None) -> bool:
    return standing_mode(environ, day) == MODE_EPISODE_RELATIVE


def method_params(environ=os.environ, day: date | None = None) -> dict:
    """The published parameters in force on `day`, for audits and reports."""
    return {
        "mode": standing_mode(environ, day),
        "configured_mode": (environ.get(MODE_ENV) or "").strip().lower() or MODE_ABSOLUTE,
        "effective_from": (effective_from(environ).isoformat() if effective_from(environ) else None),
        "half_life_days": half_life_from_env(environ),
        "prior_mass": prior_mass_from_env(environ),
        "window_days": window_from_env(environ),
        "promotion_margin_abs": promotion_margin_abs(environ, day),
        "curve_score_threshold": curve_score_threshold(environ, day),
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
                          window_days: int | None = None,
                          ) -> dict[str, list[ScoredEpisode]]:
    """hotkey -> relative ScoredEpisodes in the window, from receipts plus
    the absence-penalty log net of cancellations."""
    if window_days is None:
        window_days = window_from_env()
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
            # The receipt's own entry weight when it carries one (horizon
            # blend × episode weight, from the resolution gate); the plain
            # horizon share for receipts published before that field existed.
            try:
                w = float(e.get("weight"))
            except (TypeError, ValueError):
                w = 0.0
            if not w > 0:
                w = horizon_entry_weight(key[1])
            out[miner].append(ScoredEpisode(
                score=score - means[key], scored_on=scored_on, weight=w))
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
                          window_days: int | None = None,
                          ) -> dict[str, list[ScoredEpisode]]:
    """The entries every ranking consumer must read: ledger (absolute) or
    receipts (episode-relative), by the published mode."""
    if window_days is None:
        window_days = window_from_env(environ)
    if relative_enabled(environ, as_of):
        return load_relative_entries(root, as_of, window_days)
    return standing_ledger.load_entries(root, as_of=as_of, window_days=window_days)
