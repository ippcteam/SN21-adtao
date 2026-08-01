"""Standing ledger — the persistent store the daily weight path reads.

E1 (daily_score_flow) emits per-(episode,horizon) WeightedEntry values as
horizons finalise; this module persists them per miner and reloads them as
the ScoredEpisode lists that D13's `standing` consumes. Same storage
discipline as the shadow ledger (M3): append-only JSONL under a root dir,
no DB migration, rail-attestable, trivially migrated later.

Layout:
    <root>/standing/<hotkey>.jsonl      one line per WeightedEntry
    <root>/standing/_promotion.json     current PromotionState ([D8])
    <root>/standing/_promotion_log.jsonl  promotion events, append-only
    <root>/standing/_eviction_state.json  current EvictionStates (liveness)
    <root>/standing/_strikes.jsonl        strike/lifecycle events, append-only

Entries older than `window_days` contribute nothing to the D13 average
(the window cut in episode_weighted_average), so `load_entries` drops them
at read time and `compact` rewrites files to shed dead weight — the ledger
never needs unbounded growth.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, timedelta
from typing import Iterable, Optional

from hope.scoring.daily_score_flow import WeightedEntry
from hope.scoring.episode_average import (
    DEFAULT_WINDOW_DAYS,
    ScoredEpisode,
)
from hope.scoring.champion_promotion import PromotionState
from hope.scoring.chronic_failure import (
    OBSERVATION_KINDS,
    EvictionState,
    StrikeEvent,
)

_SAFE_HOTKEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def standing_dir(root: str) -> str:
    return os.path.join(root, "standing")


def _entry_path(root: str, hotkey: str) -> str:
    if not hotkey or any(c not in _SAFE_HOTKEY for c in hotkey):
        raise ValueError(f"unsafe hotkey for ledger path: {hotkey!r}")
    return os.path.join(standing_dir(root), f"{hotkey}.jsonl")


def append_entries(root: str, entries: Iterable[WeightedEntry]) -> int:
    """Append one day's WeightedEntries (from day_flow) to the ledger."""
    os.makedirs(standing_dir(root), exist_ok=True)
    n = 0
    by_miner: dict[str, list[WeightedEntry]] = {}
    for e in entries:
        by_miner.setdefault(e.miner, []).append(e)
    for miner, miner_entries in by_miner.items():
        with open(_entry_path(root, miner), "a") as f:
            for e in miner_entries:
                f.write(json.dumps({
                    "score": e.score, "weight": e.weight,
                    "day": str(e.entered_on),
                }) + "\n")
                n += 1
    return n


def load_entries(
    root: str,
    as_of: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, list[ScoredEpisode]]:
    """Load every miner's in-window entries as ScoredEpisode lists."""
    d = standing_dir(root)
    if not os.path.isdir(d):
        return {}
    cutoff = as_of - timedelta(days=window_days)
    out: dict[str, list[ScoredEpisode]] = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl") or name.startswith("_"):
            continue
        hotkey = name[:-6]
        eps: list[ScoredEpisode] = []
        with open(os.path.join(d, name)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                scored_on = date.fromisoformat(rec["day"])
                if scored_on < cutoff:
                    continue
                eps.append(ScoredEpisode(
                    score=float(rec["score"]),
                    scored_on=scored_on,
                    weight=float(rec.get("weight", 1.0)),
                ))
        if eps:
            out[hotkey] = eps
    return out


def scored_day_counts(entries: dict[str, list[ScoredEpisode]]) -> dict[str, int]:
    """Distinct scored DAYS per miner — [D8] condition 3 is calendar-denominated."""
    return {hk: len({e.scored_on for e in eps}) for hk, eps in entries.items()}


def compact(root: str, as_of: date, window_days: int = DEFAULT_WINDOW_DAYS) -> int:
    """Rewrite ledger files keeping only in-window entries; returns lines shed."""
    d = standing_dir(root)
    if not os.path.isdir(d):
        return 0
    cutoff = as_of - timedelta(days=window_days)
    shed = 0
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl") or name.startswith("_"):
            continue
        path = os.path.join(d, name)
        with open(path) as f:
            lines = [ln for ln in f if ln.strip()]
        kept = [
            ln for ln in lines
            if date.fromisoformat(json.loads(ln)["day"]) >= cutoff
        ]
        if len(kept) != len(lines):
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(kept)
            os.replace(tmp, path)
            shed += len(lines) - len(kept)
    return shed


# ---- [D8] promotion persistence ------------------------------------------

def _promotion_path(root: str) -> str:
    return os.path.join(standing_dir(root), "_promotion.json")


def load_promotion_state(root: str) -> PromotionState:
    path = _promotion_path(root)
    if not os.path.exists(path):
        return PromotionState()
    with open(path) as f:
        rec = json.load(f)
    return PromotionState(
        champion=rec.get("champion"),
        challenger=rec.get("challenger"),
        lead_started=(date.fromisoformat(rec["lead_started"])
                      if rec.get("lead_started") else None),
        last_observed=(date.fromisoformat(rec["last_observed"])
                       if rec.get("last_observed") else None),
        vacated_on=(date.fromisoformat(rec["vacated_on"])
                    if rec.get("vacated_on") else None),
        vacate_reason=rec.get("vacate_reason"),
    )


def save_promotion_state(root: str, state: PromotionState) -> None:
    os.makedirs(standing_dir(root), exist_ok=True)
    rec = {
        "champion": state.champion,
        "challenger": state.challenger,
        "lead_started": str(state.lead_started) if state.lead_started else None,
        "last_observed": str(state.last_observed) if state.last_observed else None,
        "vacated_on": str(state.vacated_on) if state.vacated_on else None,
        "vacate_reason": state.vacate_reason,
    }
    tmp = _promotion_path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, _promotion_path(root))


def append_promotion_event(root: str, event: dict) -> None:
    """Promotion log is append-only — the [D8] audit trail."""
    os.makedirs(standing_dir(root), exist_ok=True)
    with open(os.path.join(standing_dir(root), "_promotion_log.jsonl"), "a") as f:
        f.write(json.dumps(event) + "\n")


# ---- chronic-failure (liveness) persistence ---------------------------------
# Same shape as the [D8] pair above, and deliberately in the STANDING root:
# the shadow tree is the raw execution record (facts), standing is derived
# judgement. Both control files are underscore-prefixed, which `load_entries`
# and `compact` already skip — a control file can never be mistaken for a
# miner's score file.

def _eviction_state_path(root: str) -> str:
    return os.path.join(standing_dir(root), "_eviction_state.json")


def _strikes_path(root: str) -> str:
    return os.path.join(standing_dir(root), "_strikes.jsonl")


def _opt_date(raw) -> Optional[date]:
    return date.fromisoformat(raw) if raw else None


def load_eviction_states(root: str) -> dict[str, EvictionState]:
    path = _eviction_state_path(root)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    out: dict[str, EvictionState] = {}
    for hotkey, rec in raw.items():
        out[hotkey] = EvictionState(
            hotkey=rec.get("hotkey", hotkey),
            evicted_on=_opt_date(rec.get("evicted_on")),
            evictions_total=int(rec.get("evictions_total", 0)),
            last_eviction_on=_opt_date(rec.get("last_eviction_on")),
            reinstate_not_before=_opt_date(rec.get("reinstate_not_before")),
        )
    return out


def save_eviction_states(root: str, states: dict[str, EvictionState]) -> None:
    os.makedirs(standing_dir(root), exist_ok=True)
    rec = {
        hotkey: {
            "hotkey": st.hotkey,
            "evicted_on": str(st.evicted_on) if st.evicted_on else None,
            "evictions_total": st.evictions_total,
            "last_eviction_on": (str(st.last_eviction_on)
                                 if st.last_eviction_on else None),
            "reinstate_not_before": (str(st.reinstate_not_before)
                                     if st.reinstate_not_before else None),
        }
        for hotkey, st in sorted(states.items())
    }
    tmp = _eviction_state_path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1)
    os.replace(tmp, _eviction_state_path(root))


def append_strike_events(root: str, events: Iterable[StrikeEvent]) -> int:
    """Liveness log is append-only — the miner-facing audit trail. Order is
    chronological, which is what makes `last record wins` well defined."""
    events = list(events)
    if not events:
        return 0
    os.makedirs(standing_dir(root), exist_ok=True)
    with open(_strikes_path(root), "a") as f:
        for e in events:
            f.write(json.dumps({
                "day": str(e.day), "hotkey": e.hotkey, "kind": e.kind,
                "fault": e.fault, "error": e.error, "reason": e.reason,
                "detail": e.detail,
            }) + "\n")
    return len(events)


def _strike_event(rec: dict) -> StrikeEvent:
    return StrikeEvent(
        day=date.fromisoformat(rec["day"]),
        hotkey=rec["hotkey"],
        kind=rec["kind"],
        fault=rec.get("fault", "none"),
        error=rec.get("error"),
        reason=rec.get("reason", "ok"),
        detail=rec.get("detail"),
    )


def _strike_event_in_window(ev: StrikeEvent, cutoff: Optional[date]) -> bool:
    """Which events a windowed read keeps.

    OBSERVATION events (strike/clean/excluded) are only ever consulted by
    chronic_failure.strikes_in_window, which cuts at `window_days` anyway —
    so anything older than the cutoff is dead weight and is dropped at read
    time, exactly as load_entries drops out-of-window scores. LIFECYCLE
    events (evicted/reinstated/eviction_retracted) are ALWAYS kept: they are
    O(evictions) not O(miners x days), and chronic_failure.retract_eviction
    reconstructs `evictions_total` from them, so pruning them would corrupt
    state rather than merely shrink an audit trail.
    """
    if cutoff is None or ev.kind not in OBSERVATION_KINDS:
        return True
    return ev.day >= cutoff


def load_strike_events(
    root: str,
    as_of: Optional[date] = None,
    window_days: Optional[int] = None,
) -> list[StrikeEvent]:
    """The liveness log. With `as_of` + `window_days`, observation events
    older than the window are dropped at read time (see
    _strike_event_in_window); without them the whole file is returned."""
    path = _strikes_path(root)
    if not os.path.exists(path):
        return []
    cutoff = (as_of - timedelta(days=int(window_days) - 1)
              if as_of is not None and window_days else None)
    out: list[StrikeEvent] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = _strike_event(json.loads(line))
            if _strike_event_in_window(ev, cutoff):
                out.append(ev)
    return out


def compact_strike_events(root: str, as_of: date,
                          window_days: int = DEFAULT_WINDOW_DAYS) -> int:
    """Rewrite _strikes.jsonl keeping what a windowed read would keep;
    returns lines shed. The counterpart `compact` deliberately skips
    underscore-prefixed control files, which left this one file with no
    pruning path at all — the docstring's "never needs unbounded growth"
    promise now has a producer for the liveness log too."""
    path = _strikes_path(root)
    if not os.path.exists(path):
        return 0
    cutoff = as_of - timedelta(days=int(window_days) - 1)
    with open(path) as f:
        lines = [ln for ln in f if ln.strip()]
    kept = [ln for ln in lines
            if _strike_event_in_window(_strike_event(json.loads(ln)), cutoff)]
    if len(kept) == len(lines):
        return 0
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(kept)
    os.replace(tmp, path)
    return len(lines) - len(kept)
