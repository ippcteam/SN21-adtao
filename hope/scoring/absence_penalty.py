"""Absence penalty — every uncovered episode enters your standing at the floor.

Ruling (Rob, 24 Aug 2026): a miner must not be able to hold a board
position while absent. Before this rule a missed day cost only transient
weight (participation gate, 7-day lookback, forgiven on return) and left
the STANDING untouched — absence on hard days was strictly profitable, and
one miner held #1 on an average built only from easy days it happened to
attend. This rule makes absence a scored event.

THE RULE (published before enabled):
  For every day the SUBNET ran, every RANKED-OR-ACTIVE miner is charged one
  penalty entry, at the published floor score, for EVERY episode of that
  day it did not return a scoreable prediction for. Full coverage = the
  rule never touches you. There is no threshold to duck under (the 75%
  participation bar stays where it always was — the payment gate), and
  there is no exit: as long as your past entries keep you on the board,
  every uncovered day bleeds you toward the floor until you return or your
  window empties and you leave the board naturally. This deliberately
  replaces a separate staleness rule — going quiet IS the bleed.

  Floor = 0.30, strictly below every scoring band observed in production
  (field means 0.50-0.62 on normal days, ~0.45 after the worst batch on
  record), so participation dominates absence on any day we have ever
  seen. Env-overridable; the published value is the contract.

WHO IS CHARGED — "ranked or active", precisely:
  a) any hotkey with standing-ledger entries inside the 35-day window
     (it holds a board position, so its number must stay live), or
  b) any hotkey with shadow presence in the trailing 7 days (it is playing,
     even if nothing has settled for it yet).
  A hotkey that is neither is not participating; admission and liveness
  own that case, not this rule.

SAFETY PROPERTIES, in order:
  1. A day the subnet did NOT run charges nobody (subnet_ran — the
     2026-08-03 lesson: our failure must never read as theirs).
  2. Idempotent forever: one penalty record per (day, hotkey); re-runs and
     catch-up sweeps cannot double-charge.
  3. VERIFIABLE: every applied penalty is written to the penalty log,
     which is published to the public mirror beside the receipts — a
     miner can reproduce their standing, penalties included, from public
     documents alone.
  4. Flag-gated (SN21_ABSENCE_PENALTY, default off): the code ships, the
     docs publish the rule, miners are told, and only then does it bite.
     Applied forward from the enable date, never retroactively.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from hope.backtest import shadow
from hope.scoring import standing_ledger
from hope.scoring.daily_score_flow import WeightedEntry, horizon_entry_weight

FLAG_ENV = "SN21_ABSENCE_PENALTY"
SCORE_ENV = "SN21_ABSENCE_PENALTY_SCORE"
DEFAULT_PENALTY_SCORE = 0.30   # published floor; below every observed band
ACTIVE_LOOKBACK_DAYS = 7       # shadow presence within this window = playing
CATCHUP_DAYS = 3               # each run re-checks this many trailing days

# The announced effective date (Rob, 24 Aug 2026). Days before it are never
# charged, no matter when the flag flips or how far the catch-up sweep
# reaches — "applied forward" is a property of the code, not of timing.
EFFECTIVE_FROM = date(2026, 8, 24)


def absence_penalty_enabled(environ=os.environ) -> bool:
    return (environ.get(FLAG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def penalty_score(environ=os.environ) -> float:
    try:
        return float(environ.get(SCORE_ENV) or DEFAULT_PENALTY_SCORE)
    except ValueError:
        return DEFAULT_PENALTY_SCORE


def _log_path(root: str) -> str:
    return os.path.join(standing_ledger.standing_dir(root),
                        "_absence_penalties.jsonl")


def applied(root: str) -> set[tuple[str, str]]:
    """(day, hotkey) pairs already charged — one charge per pair, ever."""
    p = _log_path(root)
    if not os.path.exists(p):
        return set()
    out: set[tuple[str, str]] = set()
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                out.add((rec["day"], rec["hotkey"]))
    return out


def penalty_log(root: str) -> list[dict]:
    """The full applied-penalty log, oldest first — the published artifact."""
    p = _log_path(root)
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def chargeable_hotkeys(root: str, day: date,
                       lookback: int = ACTIVE_LOOKBACK_DAYS) -> set[str]:
    """Ranked-or-active: on the board (in-window standing entries) OR
    playing (shadow presence in the trailing window incl. day)."""
    ranked = set(standing_ledger.load_entries(root, as_of=day).keys())
    active: set[str] = set()
    for n in range(lookback):
        d = (day - timedelta(days=n)).isoformat()
        if shadow.subnet_ran(root, d):
            active |= set(shadow.day_coverage(root, d).keys())
    return ranked | active


def compute_penalties(root: str, day: date) -> list[dict]:
    """What day `day` charges, as plain dicts. Pure read, no flag check.

    [] when the subnet did not run. Every chargeable hotkey owes one entry
    per episode of the day it did not return a scoreable prediction for —
    a fully absent hotkey owes the whole day.
    """
    if day < EFFECTIVE_FROM:
        return []
    d = day.isoformat()
    if not shadow.subnet_ran(root, d):
        return []
    coverage = shadow.day_coverage(root, d)
    if not coverage:
        return []
    day_episodes = max((eps or 0) for eps, _ in coverage.values())
    if day_episodes <= 0:
        return []
    done = applied(root)
    out: list[dict] = []
    for hk in sorted(chargeable_hotkeys(root, day)):
        if (d, hk) in done:
            continue
        eps_in, preds_out = coverage.get(hk, (0, 0))
        missed = (day_episodes if hk not in coverage
                  else max(0, (eps_in or 0) - (preds_out or 0)))
        if missed > 0:
            out.append({"day": d, "hotkey": hk, "missed": missed})
    return out


def apply_penalties(root: str, day: date, environ=os.environ) -> dict:
    """Charge `day` and the trailing catch-up days. Idempotent. The FLAG is
    the caller's job (daily_loop), so dry reads stay possible flag-off."""
    score = penalty_score(environ)
    charged: list[dict] = []
    written = 0
    for n in range(CATCHUP_DAYS - 1, -1, -1):
        d = day - timedelta(days=n)
        for p in compute_penalties(root, d):
            entries = [WeightedEntry(miner=p["hotkey"], score=score,
                                     weight=horizon_entry_weight(7, "high"),
                                     entered_on=d)
                       for _ in range(p["missed"])]
            written += standing_ledger.append_entries(root, entries)
            with open(_log_path(root), "a") as f:
                f.write(json.dumps({"day": p["day"], "hotkey": p["hotkey"],
                                    "missed": p["missed"],
                                    "score": score}) + "\n")
            charged.append(p)
    return {"penalised_miners": len({p["hotkey"] for p in charged}),
            "penalty_entries": written,
            "penalty_score": score,
            "days_checked": CATCHUP_DAYS,
            "detail": [{"hotkey": p["hotkey"][:16], "day": p["day"],
                        "missed": p["missed"]} for p in charged[:10]]}
