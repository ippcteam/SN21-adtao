"""Option A's participation gate — the bridge pays only those who show up.

Governance ruling of 2026-08-02, choosing Option A: hold the last proven
weights through the settling gap, but make the bridge conditional. The
validator checks each day that a miner submitted qualifying daily predictions;
miss a day and weight decays, miss several and it zeroes. This is that check.

WHY "SUBMITTED" MEANS DELIVERED PREDICTIONS, NOT "THE MODEL RAN".
On 2026-08-03 we ran deliberately broken models through the real execution path
(scripts/hostile_model_probe.py). A model that prints rubbish exits cleanly and
is recorded as a SUCCESSFUL run with zero predictions — _parse_output ignores
non-JSON as "never fatal". So `ok=True` is satisfiable by a container that does
nothing at all, and gating on it would pay exactly the free-riding the gate
exists to stop. Coverage — predictions delivered as a share of the day's
bundle — is already written per miner per day by shadow.record_day
(`episodes_in`, `predictions_out`), so this needs no new plumbing.

WHY OUR OUTAGE DAYS CANNOT COUNT.
On the same morning a worker died at 04:11, the changelog never synced, and no
bundle was produced at all. Under a naive gate every one of the ~120 scored
hotkeys would have been marked absent for OUR failure — and with five other
validators mirroring our vector within the hour, that would have propagated
subnet-wide before anyone looked. A day the subnet did not run is not a day a
miner failed to submit. shadow.day_run_status returns an EMPTY dict for such a
day and its docstring says so explicitly: "An empty dict means THE SUBNET DID
NOT RUN — never 'everybody failed'."

The direction of every judgement call here is toward PAYING. A gate that
wrongly pays costs one miner's share for one day; a gate that wrongly zeroes
strips a miner across the whole subnet at once, with no independent check,
because the copiers copy the mistake too.

NUMBERS ARE [PENDING RATIFICATION] and are environment values, never code edits — same
discipline as the chronic-failure strike numbers. The ruling asked for a definition of
"submitted some scores"; the proposals below are ours until he rules.

Pure module: no I/O, no chain calls. The ledger read lives in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- RATIFIED, 2026-08-03 --------------------------------------------
# Share of the day's bundle a miner must actually predict for the day to count
# as a submission. NOT "did the container exit 0".
#
# A 0.50 floor was proposed; the ruling set 0.75 — the scoring bar is higher
# still, but 75% is what counts as having submitted. So the bar for "showing up"
# is three quarters of the day's episodes, not half.
#
# The 0.90 admission bar (backtest.gate min_coverage_ratio) is a DIFFERENT
# number and stays where it is — that one is about quality of a submitted
# model, this one is about turning up each day.
#
# Name kept as DEFAULT_MIN_COVERAGE so every import site keeps working;
# the value is no longer pending.
DEFAULT_MIN_COVERAGE = 0.75

# Weight retained after each consecutive missed day. The ruling: miss a day, weight
# decays; miss several, it zeroes."
DEFAULT_DECAY_PER_MISS = 0.5

# Consecutive misses at which the bridge pays nothing.
DEFAULT_MISSES_TO_ZERO = 3

MIN_COVERAGE_ENV = "SN21_PARTICIPATION_MIN_COVERAGE"
DECAY_ENV = "SN21_PARTICIPATION_DECAY_PER_MISS"
ZERO_AT_ENV = "SN21_PARTICIPATION_MISSES_TO_ZERO"

# Verdicts
SUBMITTED = "submitted"
MISSED = "missed"
SUBNET_DOWN = "subnet_down"      # excluded — our failure, never theirs


@dataclass(frozen=True)
class ParticipationParams:
    min_coverage: float = DEFAULT_MIN_COVERAGE
    decay_per_miss: float = DEFAULT_DECAY_PER_MISS
    misses_to_zero: int = DEFAULT_MISSES_TO_ZERO


def _num_env(environ, key, fallback, cast):
    """Unset, blank or malformed keeps the proposal. A deploy typo must never
    silently loosen or tighten what miners are paid."""
    raw = (environ.get(key) or "").strip()
    if not raw:
        return fallback
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        print(f"[participation] {key}={raw!r} is not valid — keeping {fallback}",
              flush=True)
        return fallback
    return val


def params_from_env(environ) -> ParticipationParams:
    """The policy numbers as configuration. Out-of-range values keep the proposal
    rather than being clamped silently — a coverage of 5.0 is a typo, not an
    instruction, and clamping it to 1.0 would hide that."""
    mc = _num_env(environ, MIN_COVERAGE_ENV, DEFAULT_MIN_COVERAGE, float)
    if not 0.0 < mc <= 1.0:
        print(f"[participation] {MIN_COVERAGE_ENV}={mc} outside (0,1] — "
              f"keeping {DEFAULT_MIN_COVERAGE}", flush=True)
        mc = DEFAULT_MIN_COVERAGE
    dc = _num_env(environ, DECAY_ENV, DEFAULT_DECAY_PER_MISS, float)
    if not 0.0 <= dc < 1.0:
        print(f"[participation] {DECAY_ENV}={dc} outside [0,1) — "
              f"keeping {DEFAULT_DECAY_PER_MISS}", flush=True)
        dc = DEFAULT_DECAY_PER_MISS
    mz = _num_env(environ, ZERO_AT_ENV, DEFAULT_MISSES_TO_ZERO, int)
    if mz < 1:
        print(f"[participation] {ZERO_AT_ENV}={mz} must be >= 1 — "
              f"keeping {DEFAULT_MISSES_TO_ZERO}", flush=True)
        mz = DEFAULT_MISSES_TO_ZERO
    return ParticipationParams(mc, dc, mz)


def day_verdict(episodes_in: int | None, predictions_out: int | None,
                subnet_ran: bool, params: ParticipationParams) -> str:
    """One miner, one day: SUBMITTED, MISSED, or SUBNET_DOWN.

    `subnet_ran` False -> SUBNET_DOWN regardless of anything else. That is the
    2026-08-03 case: no bundle shipped, so nobody could have submitted.

    A bundle with zero episodes is also SUBNET_DOWN, not a miss — there was
    nothing to predict, so failing to predict it is not a failure. This is the
    thin-day case, and the published weekend rule already says thin days must not
    punish anybody.
    """
    if not subnet_ran:
        return SUBNET_DOWN
    total = int(episodes_in or 0)
    if total <= 0:
        return SUBNET_DOWN
    delivered = int(predictions_out or 0)
    return SUBMITTED if (delivered / total) >= params.min_coverage else MISSED


def bridge_multiplier(verdicts, params: ParticipationParams) -> float:
    """Weight multiplier in [0,1] from a miner's recent day verdicts.

    `verdicts` is chronological, oldest first. SUBNET_DOWN days are dropped
    entirely — they neither reward nor punish, and they do NOT break a run of
    misses, because a day we did not ship tells us nothing about whether the
    miner would have shown up.

    Only CONSECUTIVE misses at the tail matter: a miner who missed on Monday
    and has submitted every day since is participating, and the bridge is a
    participation test rather than a memory of past sins. Scoring is where
    history lives.
    """
    tail = [v for v in verdicts if v != SUBNET_DOWN]
    consecutive = 0
    for v in reversed(tail):
        if v == MISSED:
            consecutive += 1
        else:
            break
    if consecutive == 0:
        return 1.0
    if consecutive >= params.misses_to_zero:
        return 0.0
    return params.decay_per_miss ** consecutive
