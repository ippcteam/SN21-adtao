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
    )


def save_promotion_state(root: str, state: PromotionState) -> None:
    os.makedirs(standing_dir(root), exist_ok=True)
    rec = {
        "champion": state.champion,
        "challenger": state.challenger,
        "lead_started": str(state.lead_started) if state.lead_started else None,
        "last_observed": str(state.last_observed) if state.last_observed else None,
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
