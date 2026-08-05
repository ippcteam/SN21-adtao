"""Chronic failure — strikes, eviction and re-entry for models that don't run.

The miner-facing statement of this policy is docs/MINER_MODEL_SPEC.md §5.
This module implements it, parameterised and flag-gated, so the numbers can
be ratified without a code change and so nothing reaches miners before it is
announced.

WHAT A STRIKE IS
    One calendar day on which a miner's container was EXECUTED and failed
    in a way that is the miner's own fault: crash, non-zero exit, timeout,
    memory/CPU budget breach.

WHAT A STRIKE IS NOT — all three are load-bearing:
  * A weak, wrong or absent PREDICTION is never a strike. Coverage
    shortfalls are already priced by scorer.MIN_COVERAGE_FRACTION
    (scorer.py:30) and the near-zero ramp (null_penalty.py), and a missing
    prediction yields no ledger entry at all rather than a zero
    (settle_day_flow module docstring). Being wrong is paid for in the
    standing; liveness is a separate axis and must stay one.
  * A day the SUBNET did not run is never a strike. A day can be absent
    from the ledger for reasons that have nothing to do with any miner, and
    a consumer that read absence as failure would strike every miner for an
    operator non-run. Strikes are only ever read from a POSITIVE ok:false
    record; a missing day yields no strike AND no observation.
  * A SUBNET-side failure (no docker daemon) is never a strike, and is not
    a clean day either — it is excluded from both sides of the ledger.

Pure module (house pattern: episode_average, champion_promotion,
weight_curve and collateral_floor are all pure with I/O injected).
Persistence lives in standing_ledger (`_eviction_state.json` +
`_strikes.jsonl`, the reserved-underscore convention load_entries already
skips); the daily wiring lives in validator/daily_loop.py; enforcement
lands at the single filter point in validator/daily_stream_weights.py.

Eviction means EXCLUSION FROM THE EARNING SET — nothing else. Standings are
not deleted (scores are facts), the container keeps being executed daily
(that execution is how re-entry is observed), and no alpha is slashed: a
zero-weighted miner's escrowed lock simply freezes under the existing
frozen-floor rule (collateral_floor.fold_day).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from hope.backtest.container_runner import (
    ERR_DOCKER_UNAVAILABLE,
    ERR_EXIT_PREFIX,
    ERR_TIMEOUT_PREFIX,
)
from hope.backtest.image_intake import (
    ERR_PULL_EXIT_PREFIX,
    ERR_PULL_TIMEOUT_PREFIX,
)

# ---- The policy numbers — ruled 2026-08-03 ----------------------------------
# The DEFAULT lives here so code, cutover checklist and governance amendment
# cannot drift apart, and the ENV VARS below still govern at runtime so a later
# restatement stays a setting rather than a code change.
#
# Ruled: five failed days within a rolling fourteen evicts, and the wait before
# re-entry is SEVEN days rather than the fourteen originally proposed — long
# enough to mean something, short enough that a fixed container is not parked
# for a fortnight. The same ruling added a constraint the numbers alone do not
# express: the field must never be emptied, and at least one house model must
# survive. See the non-empty-field floor below.
DEFAULT_STRIKES_TO_EVICT = 5      # N failed days …
DEFAULT_WINDOW_DAYS = 14          # … within M rolling calendar days
DEFAULT_REPEAT_REVIEW_DAYS = 7    # K: repeat offender's cooldown (was 14)

# Runtime overrides — exactly the SN21_D3_MIN_DAILY_EPISODES pattern
# (daily_stream_weights.d3_min_daily_episodes): unset keeps the proposal,
# set adopts the ratified number without touching this file.
STRIKES_ENV = "SN21_CHRONIC_STRIKES"
WINDOW_ENV = "SN21_CHRONIC_WINDOW_DAYS"
REVIEW_ENV = "SN21_CHRONIC_REVIEW_DAYS"
REPEAT_LOOKBACK_ENV = "SN21_CHRONIC_REPEAT_LOOKBACK_DAYS"

# ---- THE NON-EMPTY-FIELD FLOOR — ruled 2026-08-03 ---------------------------
# The field must never be emptied, and at least one house model must survive.
#
# WHY THIS IS NOT OPTIONAL. With the policy on, evicting the last
# placement-eligible model empties the weight vector entirely and the subnet
# pays NOBODY. Early in a subnet's life that is not a distant edge case — the
# last model can also be the only model. Other validators mirror the vector
# within the hour, so an empty vector propagates subnet-wide before anyone
# looks at it.
#
# Two protections, because the requirement has two parts:
#
#   MIN_SURVIVORS      never let the non-evicted field fall below this. Answers
#                      "not evict all miners" for ANY population, including one
#                      with no house model in it.
#   HOUSE_HOTKEY_ENV   a named hotkey that is never evicted at all. Answers
#                      "we need at least 1 house model". Config, not a
#                      hardcoded key — which model is the house model is a
#                      governance choice and it will change.
#
# A withheld eviction is RECORDED (KIND_EVICTION_WITHHELD), never silently
# skipped. The strike history stands, so the moment a second model exists the
# eviction can proceed on its own merits. Hiding it would make the subnet look
# healthy while a model that earned eviction kept earning.
MIN_SURVIVORS = 1
HOUSE_HOTKEY_ENV = "SN21_HOUSE_HOTKEY"
MIN_SURVIVORS_ENV = "SN21_MIN_SURVIVORS"


def house_hotkey_from(environ) -> str | None:
    """The protected house model, or None. Blank/unset means no protection —
    MIN_SURVIVORS still applies, so the field cannot empty either way."""
    raw = (environ.get(HOUSE_HOTKEY_ENV) or "").strip()
    return raw or None


def min_survivors_from(environ) -> int:
    """How many models must always remain. Below 1 is rejected: zero would
    reinstate exactly the failure the ruling forbids."""
    raw = (environ.get(MIN_SURVIVORS_ENV) or "").strip()
    if not raw:
        return MIN_SURVIVORS
    try:
        n = int(raw)
    except ValueError:
        print(f"[chronic] {MIN_SURVIVORS_ENV}={raw!r} is not an integer — "
              f"keeping {MIN_SURVIVORS}", flush=True)
        return MIN_SURVIVORS
    if n < 1:
        print(f"[chronic] {MIN_SURVIVORS_ENV}={n} would allow an empty field — "
              f"keeping {MIN_SURVIVORS}", flush=True)
        return MIN_SURVIVORS
    return n


# Flag gate. Default OFF: strikes are still RECORDED and would-be evictions
# still decided (observation is free and builds the evidence base governance needs
# to fix N/M/K), but nothing touches weights until the amendment ships.
# Verbatim shape of settle_day_flow.settle_scoring_v2_enabled.
FLAG_ENV = "SN21_CHRONIC_FAILURE_POLICY"

# ---- fault ownership --------------------------------------------------------
FAULT_NONE = "none"        # the run succeeded
FAULT_MINER = "miner"      # crash / non-zero exit / timeout / budget breach
FAULT_SUBNET = "subnet"    # our host: no docker daemon
FAULT_UNKNOWN = "unknown"  # unattributable — counts toward NOTHING

# ---- miner-facing reasons (display only; never affects the count) -----------
REASON_NONE = "ok"
REASON_TIMEOUT = "budget_breach_time"
REASON_MEMORY = "budget_breach_memory"   # docker OOM-kill => exit 137
REASON_CRASH = "crash"
REASON_SUBNET_UNAVAILABLE = "subnet_unavailable"
REASON_UNKNOWN = "unclassified"

OOM_EXIT_CODE = 137  # 128 + SIGKILL: how --memory=1g enforcement arrives

# ---- event kinds ------------------------------------------------------------
KIND_STRIKE = "strike"
KIND_CLEAN = "clean"
KIND_EXCLUDED = "excluded"        # ran, failed, not the miner's fault
KIND_EVICTED = "evicted"          # lifecycle
KIND_REINSTATED = "reinstated"    # lifecycle
KIND_EVICTION_RETRACTED = "eviction_retracted"   # lifecycle (see observe_day)
KIND_EVICTION_WITHHELD = "eviction_withheld"     # lifecycle — the non-empty-field floor

# Only these three are day OBSERVATIONS; lifecycle events never participate
# in strike counting.
OBSERVATION_KINDS = (KIND_STRIKE, KIND_CLEAN, KIND_EXCLUDED)
LIFECYCLE_KINDS = (KIND_EVICTED, KIND_REINSTATED, KIND_EVICTION_RETRACTED,
                   KIND_EVICTION_WITHHELD)

_EXIT_CODE_RE = re.compile(re.escape(ERR_EXIT_PREFIX) + r"(-?\d+)")


@dataclass(frozen=True)
class ChronicFailureParams:
    """The policy numbers. Defaults are proposals until ratified."""
    strikes_to_evict: int = DEFAULT_STRIKES_TO_EVICT
    # The non-empty-field floor. Defaults protect the field even if a caller
    # never reads the env — an unset variable must not be able to
    # empty the subnet.
    min_survivors: int = MIN_SURVIVORS
    house_hotkey: str | None = None
    window_days: int = DEFAULT_WINDOW_DAYS
    repeat_review_days: int = DEFAULT_REPEAT_REVIEW_DAYS
    # Whether an ANCIENT prior eviction still makes the next one a "repeat".
    # None = forever (any prior eviction is a repeat). Clause (b) of the ruling
    # "a repeat inside the review period"; whether that period is a raw day
    # count or the four-weekly review cadence (SN21_REWARD_MECHANISM.md §
    # review cadence) is unruled — PENDING RATIFICATION.
    repeat_lookback_days: int | None = None


@dataclass(frozen=True)
class StrikeEvent:
    """One append-only line of the liveness audit trail."""
    day: date
    hotkey: str
    kind: str
    fault: str = FAULT_NONE
    error: str | None = None
    reason: str = REASON_NONE
    detail: dict | None = None   # lifecycle payload (strike count etc.)


@dataclass(frozen=True)
class EvictionState:
    """Current liveness standing of one miner.

    `evicted_on is None` means "not currently evicted". History is kept
    after re-entry: `evictions_total` and `last_eviction_on` are what make
    the second eviction a repeat.
    """
    hotkey: str
    evicted_on: date | None = None
    evictions_total: int = 0
    last_eviction_on: date | None = None
    reinstate_not_before: date | None = None


@dataclass(frozen=True)
class EvictionDecision:
    """One day's fold — same shape as champion_promotion.PromotionDecision."""
    states: dict
    events: tuple = ()          # tuple[StrikeEvent, ...] emitted TODAY
    struck: tuple = ()          # hotkeys that took a strike today
    evicted: tuple = ()         # hotkeys evicted today
    reinstated: tuple = ()      # hotkeys re-admitted today
    excluded: tuple = ()        # hotkeys whose failure was not their fault
    retracted: tuple = ()       # hotkeys whose SAME-DAY eviction was undone
    withheld: tuple = ()        # earned eviction, blocked by the non-empty-field floor


def chronic_failure_policy_enabled(environ=os.environ) -> bool:
    """Single source of truth for whether eviction may touch weights.

    Default OFF: eviction retracts a published promise (MINER_MODEL_SPEC §2
    "no additional punishment"), so it ships with the amendment, not before.
    """
    return environ.get(FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(environ, name: str, default: int | None,
             minimum: int = 1) -> int | None:
    """One override. Unset/blank/garbage keeps the default — a typo in a
    deploy env must never silently loosen or tighten an eviction policy."""
    raw = environ.get(name, "")
    raw = raw.strip() if isinstance(raw, str) else raw
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def params_from_env(environ=os.environ) -> ChronicFailureParams:
    """The policy numbers as the RUNTIME sees them.

    This is what makes the module docstring's "parameterised so the numbers
    can be ratified without a code change" true: N/M/K each have an env
    override (SN21_CHRONIC_STRIKES / _WINDOW_DAYS / _REVIEW_DAYS, plus
    _REPEAT_LOOKBACK_DAYS), defaulting to the proposals above. Same shape and
    same fail-safe-on-garbage behaviour as d3_min_daily_episodes.
    """
    return ChronicFailureParams(
        strikes_to_evict=_int_env(environ, STRIKES_ENV,
                                  DEFAULT_STRIKES_TO_EVICT),
        window_days=_int_env(environ, WINDOW_ENV, DEFAULT_WINDOW_DAYS),
        repeat_review_days=_int_env(environ, REVIEW_ENV,
                                    DEFAULT_REPEAT_REVIEW_DAYS,
                                    minimum=0),
        repeat_lookback_days=_int_env(environ, REPEAT_LOOKBACK_ENV, None),
    )


# ---- fault classification ---------------------------------------------------

def classify_failure(ok: bool, error: str | None) -> str:
    """Who owns this failed run — the miner, us, or nobody knowable.

    The ONLY place the error-string taxonomy lives. The markers are imported
    constants (container_runner / image_intake) rather than local literals
    precisely because they are built inline as f-strings at the failure
    sites: a rename there must break the import, not silently reclassify a
    subnet fault as a miner fault.

    Unknown is deliberately absorbing: anything unrecognised counts toward
    NOTHING — not a strike, and not a clean day either.

    Scope note: only container_runner strings reach this today, because only
    RunResults are written to the shadow ledger (record_day). Intake-side
    failures end as IntakeResults and are never observed as a day's run;
    ALL THREE pull markers (timeout, docker-unavailable, non-zero pull exit)
    are handled here so that stays true by choice rather than by accident.
    """
    if ok:
        return FAULT_NONE
    err = (error or "").strip()
    if not err:
        # ok=False with no error string is unattributable by construction.
        return FAULT_UNKNOWN
    if err.startswith(ERR_DOCKER_UNAVAILABLE):
        # Our host has no docker daemon. Never the miner's fault.
        return FAULT_SUBNET
    if err.startswith((ERR_PULL_TIMEOUT_PREFIX, ERR_PULL_EXIT_PREFIX)):
        # Registry-side: could be the miner's registry or our network.
        # UNRULED (blocker) -> counts toward nothing. NOTE the pull-exit
        # marker MUST be tested before ERR_EXIT_PREFIX-owned strings and must
        # not itself start with "exit=" — a failed `docker pull` is not a
        # crashed container, and conflating them strikes a miner for our
        # network. (image_intake.ERR_PULL_EXIT_PREFIX exists for this.)
        return FAULT_UNKNOWN
    if err.startswith(ERR_TIMEOUT_PREFIX):
        return FAULT_MINER
    if err.startswith(ERR_EXIT_PREFIX):
        # Every non-zero exit is the miner's fault under spec §5
        # (crash/timeout/budget-breach). NOTE exit=125 is docker itself
        # failing to start the container; it is currently counted as miner
        # fault (bad image config is the common cause) and is flagged for
        # the ruling in the governance amendment.
        return FAULT_MINER
    return FAULT_UNKNOWN


def exit_code(error: str | None) -> int | None:
    """The exit code embedded in a runner error string, when there is one."""
    m = _EXIT_CODE_RE.search(error or "")
    return int(m.group(1)) if m else None


def failure_reason(ok: bool, error: str | None) -> str:
    """Miner-facing label for a failed day. Display only — the strike COUNT
    never depends on this, so refining it (e.g. teaching container_runner to
    name an OOM explicitly) cannot move anybody's eviction date."""
    if ok:
        return REASON_NONE
    fault = classify_failure(ok, error)
    if fault == FAULT_SUBNET:
        return REASON_SUBNET_UNAVAILABLE
    if fault != FAULT_MINER:
        return REASON_UNKNOWN
    err = (error or "").strip()
    if err.startswith(ERR_TIMEOUT_PREFIX):
        return REASON_TIMEOUT
    if exit_code(err) == OOM_EXIT_CODE:
        # docker enforces --memory=1g by SIGKILL; 128+9 is how the 1 GB
        # budget breach in spec §2 actually reaches us.
        return REASON_MEMORY
    return REASON_CRASH


# ---- strike counting --------------------------------------------------------

def _observations(events: Sequence[StrikeEvent], hotkey: str) -> dict:
    """day -> the OPERATIVE observation for that day (last record wins).

    Same discipline as shadow.finalize_day's `lines[-1]` and
    settle_day_flow.load_prediction_index: a failed run followed by a
    successful re-run on the same day reads as the re-run. Without this a
    recovered day would become a permanent strike.

    Retraction is whole, not partial: if the retracted strike was the one
    that triggered an eviction, observe_day also calls `retract_eviction` to
    roll `evictions_total` / `last_eviction_on` back. Clearing `evicted_on`
    alone would leave the miner permanently marked a repeat offender with
    their real strike history cut away — see retract_eviction.
    """
    per_day: dict = {}
    for ev in events:
        if ev.hotkey != hotkey or ev.kind not in OBSERVATION_KINDS:
            continue
        per_day[ev.day] = ev
    return per_day


def strike_days_in_window(
    events: Sequence[StrikeEvent],
    hotkey: str,
    as_of: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
    since: date | None = None,
) -> tuple:
    """The distinct STRIKE days inside the rolling window, ascending.

    Window semantics: `window_days` calendar days ENDING ON and INCLUDING
    `as_of` — for 14 that is as_of-13 .. as_of. Days after `as_of` are
    ignored (never let a future-dated record evict anybody), and `since`
    (the miner's last eviction day) hard-excludes everything at or before
    it so a re-admitted miner does not carry the strikes he was already
    evicted for — without that cut, the next bad day would re-evict him
    instantly on the same five.

    Deliberate asymmetry: failures recorded WHILE evicted are after
    `since`, so they DO count. Eviction withholds weight, not execution;
    a model that still does not run while sitting out has not earned a
    fresh five chances on the day it returns.
    """
    span = max(1, int(window_days))
    cutoff = as_of - timedelta(days=span - 1)
    out = []
    for day, ev in _observations(events, hotkey).items():
        if day > as_of or day < cutoff:
            continue
        if since is not None and day <= since:
            continue
        if ev.kind == KIND_STRIKE:
            out.append(day)
    return tuple(sorted(out))


def strikes_in_window(
    events: Sequence[StrikeEvent],
    hotkey: str,
    as_of: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
    since: date | None = None,
) -> int:
    return len(strike_days_in_window(events, hotkey, as_of, window_days, since))


# ---- state machine ----------------------------------------------------------

def _is_repeat(state: EvictionState, day: date,
               params: ChronicFailureParams) -> bool:
    if state.evictions_total <= 0 or state.last_eviction_on is None:
        return False
    if params.repeat_lookback_days is None:
        return True
    return (day - state.last_eviction_on).days <= int(params.repeat_lookback_days)


def evict(state: EvictionState, day: date,
          params: ChronicFailureParams = ChronicFailureParams(),
          strikes: int | None = None) -> tuple:
    """Evict a miner as of `day`. Returns (new_state, lifecycle event).

    Clause (b): a FIRST eviction is eligible to return as soon as the model
    runs clean (reinstate_not_before = the eviction day itself, so the next
    clean run re-admits). A REPEAT eviction serves `repeat_review_days`
    first — the clean run is still required, the wait is additional.
    """
    repeat = _is_repeat(state, day, params)
    not_before = day + timedelta(days=int(params.repeat_review_days)) if repeat else day
    new = EvictionState(
        hotkey=state.hotkey,
        evicted_on=day,
        evictions_total=state.evictions_total + 1,
        last_eviction_on=day,
        reinstate_not_before=not_before,
    )
    event = StrikeEvent(
        day=day, hotkey=state.hotkey, kind=KIND_EVICTED,
        fault=FAULT_MINER, reason=REASON_NONE,
        detail={
            "strikes": strikes,
            "strikes_to_evict": params.strikes_to_evict,
            "window_days": params.window_days,
            "repeat": repeat,
            "evictions_total": new.evictions_total,
            "reinstate_not_before": str(not_before),
        },
    )
    return new, event


def retract_eviction(state: EvictionState, events: Sequence[StrikeEvent],
                     day: date) -> tuple:
    """Undo an eviction recorded ON `day` because that day's strike is gone.

    `reinstate` is the wrong tool here and getting this wrong is silent: a
    reinstated miner keeps `evictions_total` and `last_eviction_on`, which is
    correct for an eviction that really HAPPENED but catastrophic for one
    that never should have. The two retained fields have teeth —
    `strikes_in_window(since=last_eviction_on)` hard-excludes every day at or
    before the retracted eviction (wiping the miner's genuine strike history
    and handing them a fresh N chances), and `_is_repeat` makes their NEXT
    eviction serve the repeat-offender cooldown. So a retraction must roll
    the counters back too, not merely clear `evicted_on`.

    The pre-eviction counters are reconstructed from the append-only log
    (the eviction lifecycle events for this hotkey on OTHER days), which is
    exactly the history `evictions_total` was counting. Lifecycle events are
    never window-pruned (standing_ledger.load_strike_events) so this stays
    exact for the life of the ledger.

    A RETRACTED eviction must not be counted as history. The log is
    append-only, so a retracted eviction keeps its KIND_EVICTED line forever
    and the KIND_EVICTION_RETRACTED line is what cancels it. Counting the
    raw KIND_EVICTED lines therefore resurrects evictions that were already
    undone: a miner who recovers same-day twice would come back from the
    second retraction carrying evictions_total=1 and a last_eviction_on
    pointing at the first — already-cancelled — eviction, which then makes
    his next genuine eviction serve the repeat cooldown and hides his real
    strike history behind that date. That is the exact damage this function
    exists to prevent, reintroduced one level down.
    """
    retracted_days = {e.day for e in events
                      if e.hotkey == state.hotkey
                      and e.kind == KIND_EVICTION_RETRACTED}
    others = [e for e in events
              if e.hotkey == state.hotkey and e.kind == KIND_EVICTED
              and e.day != day and e.day not in retracted_days]
    prev = others[-1].day if others else None
    new = EvictionState(
        hotkey=state.hotkey,
        evicted_on=None,
        evictions_total=len(others),
        last_eviction_on=prev,
        reinstate_not_before=None,
    )
    event = StrikeEvent(
        day=day, hotkey=state.hotkey, kind=KIND_EVICTION_RETRACTED,
        fault=FAULT_NONE, reason=REASON_NONE,
        detail={"retracted_eviction_on": str(day),
                "evictions_total": new.evictions_total,
                "last_eviction_on": str(prev) if prev else None},
    )
    return new, event


def reinstate(states: Mapping[str, EvictionState], hotkey: str, day: date,
              params: ChronicFailureParams = ChronicFailureParams()) -> tuple:
    """Re-admit `hotkey` on `day` if it is eligible. Returns (states, event).

    Eligibility is the cooldown only — the CALLER establishes that the model
    ran clean (observe_day does; a manual operator call is a deliberate
    override). Returns the states unchanged and a None event when the miner
    is not evicted or is still serving a repeat-offender wait.
    """
    st = states.get(hotkey)
    if st is None or st.evicted_on is None:
        return dict(states), None
    if st.reinstate_not_before is not None and day < st.reinstate_not_before:
        return dict(states), None
    new_states = dict(states)
    new_states[hotkey] = replace(st, evicted_on=None, reinstate_not_before=None)
    event = StrikeEvent(
        day=day, hotkey=hotkey, kind=KIND_REINSTATED,
        fault=FAULT_NONE, reason=REASON_NONE,
        detail={"evictions_total": st.evictions_total,
                "evicted_on": str(st.evicted_on),
                "served_repeat_wait": bool(
                    st.reinstate_not_before and st.reinstate_not_before > st.evicted_on)},
    )
    return new_states, event


def _eviction_blocked(hotkey: str, states: Mapping[str, EvictionState],
                      known: frozenset,
                      params: ChronicFailureParams) -> str | None:
    """Why this eviction must not proceed, or None if it may.

    Two independent reasons, both from the 2026-08-03 governance ruling:
      * the hotkey is the designated house model
      * evicting it would drop the surviving field below min_survivors

    Survivors are counted from the CURRENT states, so evictions already
    applied earlier in the same day are taken into account — otherwise five
    models could each be evicted in turn, every one of them believing four
    others remained.
    """
    if params.house_hotkey and hotkey == params.house_hotkey:
        return (f"house model {hotkey!r} is protected from eviction "
                f"({HOUSE_HOTKEY_ENV})")
    surviving = {
        hk for hk in known
        if hk != hotkey and (states.get(hk) or EvictionState(hotkey=hk)).evicted_on is None
    }
    floor = max(1, int(params.min_survivors))
    if len(surviving) < floor:
        return (f"evicting {hotkey!r} would leave {len(surviving)} model(s), "
                f"below the floor of {floor} — the weight vector would empty "
                f"and the subnet would pay nobody")
    return None


def observe_day(
    states: Mapping[str, EvictionState],
    events: Sequence[StrikeEvent],
    day: date,
    day_runs: Mapping[str, tuple],
    params: ChronicFailureParams = ChronicFailureParams(),
) -> EvictionDecision:
    """Fold one day's EXECUTION RESULTS into the liveness state machine.

    `day_runs`: hotkey -> (ok, error) as recorded by the shadow ledger
    (hope.backtest.shadow.day_run_status). An EMPTY mapping is a no-op by
    construction — a day the subnet did not run produces no strike and no
    observation for anybody. The caller must ALSO skip calling this when
    shadow.subnet_ran is false, so that a day which ran with zero admitted
    models is distinguishable from a day that never ran.

    Deterministic: hotkeys are processed in sorted order and every decision
    is a pure function of (states, events, day, day_runs, params).

    Idempotent: re-running the same day appends nothing new unless the
    day's operative result CHANGED (a same-day re-run flipping fail->clean
    supersedes, matching the ledger's last-record-wins discipline).
    """
    new_states = {hk: st for hk, st in states.items()}
    log = list(events)
    emitted: list = []
    struck: list = []
    evicted_today: list = []
    reinstated_today: list = []
    excluded_today: list = []
    retracted_today: list = []
    withheld_today: list = []
    # Every model we know about, not just today's runners: a model
    # that did not run today is still part of the field, and
    # counting only day_runs would let a quiet day look empty.
    known_hotkeys = frozenset(states) | frozenset(day_runs)

    for hotkey in sorted(day_runs):
        ok, error = day_runs[hotkey]
        ok = bool(ok)
        fault = classify_failure(ok, error)
        if ok:
            kind = KIND_CLEAN
        elif fault == FAULT_MINER:
            kind = KIND_STRIKE
        else:
            # Subnet fault or unattributable: recorded so the day is not
            # silently invisible, but it counts toward NEITHER the strike
            # numerator NOR the clean-run re-entry test.
            kind = KIND_EXCLUDED

        prior = _observations(log, hotkey).get(day)
        ev = StrikeEvent(day=day, hotkey=hotkey, kind=kind, fault=fault,
                         error=(None if ok else error),
                         reason=failure_reason(ok, error))
        if prior is None or prior.kind != kind:
            log.append(ev)
            emitted.append(ev)

        if kind == KIND_STRIKE:
            struck.append(hotkey)
        elif kind == KIND_EXCLUDED:
            excluded_today.append(hotkey)

        st = new_states.get(hotkey) or EvictionState(hotkey=hotkey)

        # RETRACTION: the day's operative result flipped away from a strike
        # (a same-day re-run recovered) and today's own eviction was caused
        # by that now-gone strike. Undo the eviction WHOLE — see
        # retract_eviction for why clearing `evicted_on` alone is a trap.
        if (prior is not None and prior.kind == KIND_STRIKE
                and kind != KIND_STRIKE and st.evicted_on == day):
            st, lifecycle = retract_eviction(st, log, day)
            log.append(lifecycle)
            emitted.append(lifecycle)
            retracted_today.append(hotkey)
            new_states[hotkey] = st

        if st.evicted_on is None:
            if kind == KIND_STRIKE:
                n = strikes_in_window(log, hotkey, day, params.window_days,
                                      since=st.last_eviction_on)
                if n >= max(1, int(params.strikes_to_evict)):
                    # THE NON-EMPTY-FIELD FLOOR, checked BEFORE the eviction lands. Evicting
                    # the last placement-eligible model empties the weight
                    # vector and the subnet pays nobody — and five validators
                    # mirror us within the hour.
                    block = _eviction_blocked(
                        hotkey, new_states, known_hotkeys, params)
                    if block is not None:
                        lifecycle = StrikeEvent(
                            day=day, hotkey=hotkey,
                            kind=KIND_EVICTION_WITHHELD, fault=FAULT_MINER,
                            error=None, reason=block)
                        log.append(lifecycle)
                        emitted.append(lifecycle)
                        withheld_today.append(hotkey)
                    else:
                        st, lifecycle = evict(st, day, params, strikes=n)
                        log.append(lifecycle)
                        emitted.append(lifecycle)
                        evicted_today.append(hotkey)
            new_states[hotkey] = st
        else:
            new_states[hotkey] = st
            if kind == KIND_CLEAN:
                new_states, lifecycle = reinstate(new_states, hotkey, day, params)
                if lifecycle is not None:
                    log.append(lifecycle)
                    emitted.append(lifecycle)
                    reinstated_today.append(hotkey)

    return EvictionDecision(
        states=new_states,
        events=tuple(emitted),
        struck=tuple(struck),
        evicted=tuple(evicted_today),
        reinstated=tuple(reinstated_today),
        excluded=tuple(excluded_today),
        retracted=tuple(retracted_today),
        withheld=tuple(withheld_today),
    )


def evicted_hotkeys(states: Mapping[str, EvictionState],
                    day: date) -> frozenset:
    """Who is excluded from the earning set on `day`.

    Pure: the flag gate is applied by the I/O caller (allocation_from_ledger)
    so that this function always tells the truth about the state machine even
    while enforcement is off.
    """
    return frozenset(
        hk for hk, st in states.items()
        if st.evicted_on is not None and st.evicted_on <= day
    )
