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

  Floor = 0.00, and each uncovered episode is charged at the FULL standing
  mass a covered episode would have contributed (weight 1.0). Amended
  2026-08-24 (same day the rule went live, before any charge was ever
  applied — the penalty log was still empty): the floor was first published
  as 0.30, justified against day-MEAN bands (0.45-0.62). That reasoning
  conflated day means with per-entry scores: 11.4% of real per-entry
  honest scores fall below 0.30 (measured over 134,425 production entries,
  16-20 Aug), so on those episodes the "penalty" would have paid BETTER
  than an honest prediction — a censor line, not a floor. And the original
  single 0.20-weight entry per episode carried a fifth of the mass a
  covered episode contributes (0.20+0.35+0.45 across its horizons), so
  dropping any below-average episode stayed rational. With floor 0.0 at
  weight 1.0, covering an episode at any honest score s >= 0 always yields
  a standing >= dropping it: participation strictly dominates, per episode,
  with no tail exception. Env-overridable; the published value is the
  contract.

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
from hope.scoring.daily_score_flow import WeightedEntry

FLAG_ENV = "SN21_ABSENCE_PENALTY"
SCORE_ENV = "SN21_ABSENCE_PENALTY_SCORE"
DEFAULT_PENALTY_SCORE = 0.0    # published floor; <= every achievable honest
                               # score, so a penalty can never beat honesty
# One uncovered episode costs the same standing mass a covered episode would
# have contributed: its three horizon entries sum to 1.0 (0.20+0.35+0.45).
# At the original 0.20 (one 7-day-high entry) dropping a below-average
# episode swapped 1.0 mass of low scores for a fifth of the mass — rational
# to drop. At 1.0, cover(s>=0) >= drop(0.0) holds per episode, always.
PENALTY_ENTRY_WEIGHT = 1.0
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


# The run budget the CONTRACT publishes (MINER_MODEL_SPEC: 15 CPU-minutes
# per basket). Not the enforced wall ceiling — while operations runs a
# tighter ceiling than this, a model stopped at that ceiling was stopped
# by us, and `operator_fault` reads a timeout against THIS number.
PUBLISHED_RUN_BUDGET_S = 15 * 60


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


def operator_fault(error: str | None, wall_budget_s: int | None = None) -> bool:
    """Is this day-run failure OURS rather than the miner's?

    WHY THIS EXISTS (2026-09-02). The rule charges a miner for what it did
    not deliver, and until now it read coverage alone: episodes_in minus
    predictions_out, whatever the reason. So a day when OUR side could not
    fetch the image, or cut the run off early, was charged exactly like a
    miner who never showed up. Four models were charged that way on
    2026-09-01 — one of them the best budget-change model on the subnet.

    The execution layer already draws this line (container_runner: "a
    pull/verify failure is NOT a run failure ... so a registry problem
    never strikes a miner the way a crashing container would"); the
    penalty simply never asked. Now it asks.

    OURS: the image could not be pulled, the host had no disk, the sandbox
    was unavailable — the model never got a fair chance to run.

    THEIRS: the container ran and crashed, had no entrypoint, or printed
    nothing usable. That is what the rule is for.

    A TIMEOUT IS OURS ONLY WHEN WE UNDER-BUDGETED. A model that outruns
    the budget we publish is the miner's problem; a model stopped before
    reaching that budget was stopped by us. `wall_budget_s` is the budget
    in force for the day being judged (None = don't attribute timeouts to
    us, which is the fail-safe direction for an unknown budget: it charges
    the miner, so it must only ever be taken deliberately).
    """
    if not error:
        return False
    err = str(error)
    for marker in ("pull_failed", "pull timeout>", "disk_low",
                   "sandbox_not_available"):
        if err.startswith(marker):
            return True
    if err.startswith("timeout>") and wall_budget_s:
        # "timeout>120s" — the ceiling that actually stopped this run.
        digits = "".join(c for c in err[len("timeout>"):] if c.isdigit())
        if digits and int(digits) < wall_budget_s:
            return True
    return False


def compute_penalties(root: str, day: date,
                      published_budget_s: int | None = None) -> list[dict]:
    """What day `day` charges, as plain dicts. Pure read, no flag check.

    [] when the subnet did not run. Every chargeable hotkey owes one entry
    per episode of the day it did not return a scoreable prediction for —
    a fully absent hotkey owes the whole day.

    A hotkey whose day failed on OUR side is not charged at all: see
    `operator_fault`. `published_budget_s` is the run budget the contract
    promised for that day, which is what decides whether a timeout was the
    miner outrunning the budget or us cutting them short.
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
    status = shadow.day_run_status(root, d)
    out: list[dict] = []
    for hk in sorted(chargeable_hotkeys(root, day)):
        if (d, hk) in done:
            continue
        ok, error = status.get(hk, (None, None))
        if ok is False and operator_fault(error, published_budget_s):
            # The model was scheduled and OUR side failed it — no charge.
            # It simply adds no evidence for the day, like a censored
            # horizon: absence of a fair chance is not absence.
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
        for p in compute_penalties(root, d,
                                   published_budget_s=PUBLISHED_RUN_BUDGET_S):
            entries = [WeightedEntry(miner=p["hotkey"], score=score,
                                     weight=PENALTY_ENTRY_WEIGHT,
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
