"""Chronic failure — strikes, eviction, re-entry.

The machinery ships with ZERO real-world failure samples: only one model has
ever run in shadow (reference-v1, ours) and it recorded ok:true on all four
days present in sn21_ledger. Tests are therefore the only evidence, so they
pin the SAFETY properties first (a day we did not run can never strike
anybody; a subnet-side failure can never strike anybody) before the policy
arithmetic.

Numbers under test are Rob's RULING of 2026-08-03: 5 strikes in a rolling 14
days, re-entry wait 7 (he moved it from 14: "I would suggest 7 is better").
He also ruled that the field must never be emptied — "we have to not evict all
miners - we need at least 1 house model" — which is why every eviction
scenario carries a healthy BYSTANDER: with one model in the population nothing
can be evicted at all, and that is correct.
"""

import json
import os
from dataclasses import replace
from datetime import date, timedelta

from hope.backtest.container_runner import (
    ERR_DOCKER_UNAVAILABLE,
    ERR_EXIT_PREFIX,
    ERR_TIMEOUT_PREFIX,
    RunResult,
)
from hope.backtest.image_intake import (
    ERR_PULL_EXIT_PREFIX,
    ERR_PULL_TIMEOUT_PREFIX,
)
from hope.backtest.shadow import (
    ShadowModel,
    day_run_status,
    record_day,
    run_shadow_day,
    subnet_ran,
)
from hope.scoring import standing_ledger as sl
from hope.scoring.chronic_failure import (
    min_survivors_from,
    house_hotkey_from,
    MIN_SURVIVORS_ENV,
    HOUSE_HOTKEY_ENV,
    KIND_EVICTION_WITHHELD,
    FAULT_MINER,
    FAULT_NONE,
    FAULT_SUBNET,
    FAULT_UNKNOWN,
    KIND_CLEAN,
    KIND_EVICTED,
    KIND_EVICTION_RETRACTED,
    KIND_EXCLUDED,
    KIND_REINSTATED,
    KIND_STRIKE,
    PENDING_ROB_REPEAT_REVIEW_DAYS,
    PENDING_ROB_STRIKES_TO_EVICT,
    PENDING_ROB_WINDOW_DAYS,
    REASON_CRASH,
    REASON_MEMORY,
    REASON_TIMEOUT,
    ChronicFailureParams,
    EvictionState,
    StrikeEvent,
    chronic_failure_policy_enabled,
    classify_failure,
    evicted_hotkeys,
    failure_reason,
    observe_day,
    params_from_env,
    reinstate,
    retract_eviction,
    strikes_in_window,
)

DAY = date(2026, 8, 11)
P = ChronicFailureParams()          # Rob's proposed defaults
HK = "minerA"


def _d(n):
    """n days before DAY."""
    return DAY - timedelta(days=n)


# A HEALTHY SECOND MODEL in every eviction scenario. Rob ruled 2026-08-03 that
# the field must never be emptied ("we have to not evict all miners"), so with
# only one model in the population NOTHING can ever be evicted — the guard
# withholds it. That is correct behaviour, and it means an eviction test needs
# somebody left standing. Adding a bystander is not a workaround; it is the
# only shape in which an eviction is legal at all.
BYSTANDER = "healthy-bystander"


def _fail(hotkey=HK, error=f"{ERR_EXIT_PREFIX}1: b'boom'", with_bystander=True):
    runs = {hotkey: (False, error)}
    if with_bystander and hotkey != BYSTANDER:
        runs[BYSTANDER] = (True, None)
    return runs


def _ok(hotkey=HK, with_bystander=True):
    runs = {hotkey: (True, None)}
    if with_bystander and hotkey != BYSTANDER:
        runs[BYSTANDER] = (True, None)
    return runs


def _run_days(states, events, days_and_runs, params=P):
    """Fold a sequence of (day, day_runs) forward, accumulating the log."""
    log = list(events)
    last = None
    for day, runs in days_and_runs:
        last = observe_day(states, log, day, runs, params)
        states = last.states
        log.extend(last.events)
    return states, log, last


# ---- defaults ARE Rob's ruling, and are the only numbers in the code -------

def test_defaults_are_robs_ruled_numbers():
    """Rob 2026-08-03: "are you saying that if a miner fails submit (ie bad
    code) 5x in 14 days then 14 days before return? I would suggest 7 is
    better." So N=5 and M=14 stand; the RE-ENTRY WAIT drops 14 -> 7.

    The default must BE the ruling, not an env override on top of a superseded
    proposal — a host that misses the variable would otherwise lock a miner
    out for twice as long as Rob decided."""
    assert (PENDING_ROB_STRIKES_TO_EVICT, PENDING_ROB_WINDOW_DAYS,
            PENDING_ROB_REPEAT_REVIEW_DAYS) == (5, 14, 7)
    assert P.strikes_to_evict == 5
    assert P.window_days == 14
    assert P.repeat_review_days == 7
    # "repeat" lookback deliberately unset: any prior eviction is a repeat
    # until Rob rules on what the review period means.
    assert P.repeat_lookback_days is None


def test_the_numbers_are_ratifiable_from_the_environment():
    """[finding 5] The module claims the policy is 'built parameterised …
    so the numbers can be ratified without a code change'. That is only
    true if N/M/K each have a runtime override. Same shape as
    SN21_D3_MIN_DAILY_EPISODES."""
    assert params_from_env({}) == P                     # unset keeps proposals

    ratified = params_from_env({
        "SN21_CHRONIC_STRIKES": "3",
        "SN21_CHRONIC_WINDOW_DAYS": "21",
        "SN21_CHRONIC_REVIEW_DAYS": "28",
        "SN21_CHRONIC_REPEAT_LOOKBACK_DAYS": "90",
    })
    assert (ratified.strikes_to_evict, ratified.window_days,
            ratified.repeat_review_days, ratified.repeat_lookback_days) \
        == (3, 21, 28, 90)

    # and the override GOVERNS the arithmetic, not just the dataclass
    days = [(_d(i), _fail()) for i in (2, 1, 0)]
    _, _, last = _run_days({}, [], days, ratified)
    assert last.evicted == (HK,)                        # 3 strikes, not 5
    _, _, under_default = _run_days({}, [], days, P)
    assert under_default.evicted == ()                  # 3 < 5

    # a typo must never silently loosen or tighten a policy
    assert params_from_env({"SN21_CHRONIC_STRIKES": "five"}) == P
    assert params_from_env({"SN21_CHRONIC_WINDOW_DAYS": ""}) == P
    assert params_from_env({"SN21_CHRONIC_STRIKES": "0"}).strikes_to_evict == 1


# ---- fault classification, against the PRODUCING modules' own literals ----

def test_classify_failure_against_the_real_error_strings():
    # miner fault — the two shapes container_runner actually emits
    assert classify_failure(False, f"{ERR_TIMEOUT_PREFIX}900s (container killed)") \
        == FAULT_MINER
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}1: b'traceback'") == FAULT_MINER
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}137: b''") == FAULT_MINER
    # subnet fault — our host has no docker
    assert classify_failure(False, ERR_DOCKER_UNAVAILABLE) == FAULT_SUBNET
    # unruled / unattributable
    assert classify_failure(False, f"{ERR_PULL_TIMEOUT_PREFIX}600s") == FAULT_UNKNOWN
    assert classify_failure(False, None) == FAULT_UNKNOWN
    assert classify_failure(False, "something nobody has seen") == FAULT_UNKNOWN
    # success
    assert classify_failure(True, None) == FAULT_NONE


def test_classify_failure_reads_the_strings_the_runner_really_builds(monkeypatch):
    """Pin the taxonomy to REAL RunResults, not hand-typed strings: this is
    the test that fails if container_runner's f-strings drift. No docker
    daemon is required — the subprocess boundary is faked, the error strings
    are the module's own."""
    import subprocess as sp

    from hope.backtest import container_runner as cr

    ref = "example.com/miner@sha256:" + "0" * 64
    eps = [{"episode_id": "e1"}]

    class _Killed:
        returncode = 137
        stdout = b""
        stderr = b"Killed"

    def _no_docker(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cr.subprocess, "run", _no_docker)
    res = cr.run_basket_docker(ref, eps)
    assert isinstance(res, RunResult) and res.error == ERR_DOCKER_UNAVAILABLE
    assert classify_failure(res.ok, res.error) == FAULT_SUBNET

    calls = []

    def _timeout(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise sp.TimeoutExpired(cmd="docker", timeout=900)
        return _Killed()   # the docker-kill call

    monkeypatch.setattr(cr.subprocess, "run", _timeout)
    res = cr.run_basket_docker(ref, eps, timeout_s=900)
    assert res.error.startswith(ERR_TIMEOUT_PREFIX)
    assert classify_failure(res.ok, res.error) == FAULT_MINER
    assert failure_reason(res.ok, res.error) == REASON_TIMEOUT

    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: _Killed())
    res = cr.run_basket_docker(ref, eps)
    assert res.error.startswith(ERR_EXIT_PREFIX)
    assert classify_failure(res.ok, res.error) == FAULT_MINER
    assert failure_reason(res.ok, res.error) == REASON_MEMORY   # 1 GB OOM kill


def test_pull_side_strings_are_the_intake_modules_own(monkeypatch):
    import subprocess as sp

    from hope.backtest import image_intake as ii

    def _timeout(*a, **k):
        raise sp.TimeoutExpired(cmd="docker", timeout=600)

    monkeypatch.setattr(ii.subprocess, "run", _timeout)
    ok, err = ii._default_puller("example.com/m@sha256:" + "0" * 64)
    assert not ok and err.startswith(ERR_PULL_TIMEOUT_PREFIX)
    assert classify_failure(False, err) == FAULT_UNKNOWN   # unruled

    def _no_docker(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ii.subprocess, "run", _no_docker)
    ok, err = ii._default_puller("example.com/m@sha256:" + "0" * 64)
    assert not ok and classify_failure(False, err) == FAULT_SUBNET


def test_a_failed_docker_pull_never_strikes_the_miner(monkeypatch):
    """[finding 4] The returncode!=0 branch of image_intake._default_puller —
    the one the old test never exercised. It used to build a bare `exit=`
    string, colliding with container_runner's ERR_EXIT_PREFIX, so a
    registry-side failure (bad credentials, deleted image, rate limit, our
    network) classified as FAULT_MINER and struck the miner for it."""
    from hope.backtest import image_intake as ii

    class _Failed:
        returncode = 1
        stdout = b""
        stderr = b"unauthorized: authentication required"

    monkeypatch.setattr(ii.subprocess, "run", lambda *a, **k: _Failed())
    ok, err = ii._default_puller("example.com/m@sha256:" + "0" * 64)
    assert not ok
    assert err.startswith(ERR_PULL_EXIT_PREFIX)
    assert not err.startswith(ERR_EXIT_PREFIX)     # the collision itself
    assert classify_failure(False, err) == FAULT_UNKNOWN

    # and end to end: a day carrying that string is excluded, never struck
    decision = observe_day({}, [], DAY, {HK: (False, err)}, P)
    assert decision.struck == () and decision.excluded == (HK,)
    assert decision.events[0].fault == FAULT_UNKNOWN


def test_failure_reason_names_the_budget_breach_without_touching_the_runner():
    assert failure_reason(False, f"{ERR_TIMEOUT_PREFIX}900s") == REASON_TIMEOUT
    # docker enforces --memory=1g by SIGKILL -> 137
    assert failure_reason(False, f"{ERR_EXIT_PREFIX}137: b''") == REASON_MEMORY
    assert failure_reason(False, f"{ERR_EXIT_PREFIX}1: b''") == REASON_CRASH


# ---- SAFETY: a day WE did not run can never strike anybody -----------------

def test_missing_shadow_day_yields_no_strike_and_no_observation(tmp_path):
    """The live 2026-07-30 case: 07-27/28/29/31 exist, 07-30 does not,
    because the shadow day is a manual script with no automated caller."""
    root = str(tmp_path)
    for present in ("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-31"):
        record_day(root, present,
                   ShadowModel("reference-v1", "img@sha256:a", "2026-07-27"),
                   RunResult(ok=True, predictions_out=3))

    assert subnet_ran(root, "2026-07-31") is True
    assert subnet_ran(root, "2026-07-30") is False        # the gap
    assert day_run_status(root, "2026-07-30") == {}       # never "all failed"

    # and folding that emptiness is a total no-op
    decision = observe_day({}, [], date(2026, 7, 30),
                           day_run_status(root, "2026-07-30"), P)
    assert decision.events == () and decision.struck == ()
    assert decision.states == {}


def test_day_that_ran_with_zero_models_is_distinguishable_from_no_run(tmp_path):
    root = str(tmp_path)
    run_shadow_day("2026-08-01", [{"episode_id": "e1"}], [],
                   runner=lambda m, e: RunResult(ok=True), root=root)
    assert subnet_ran(root, "2026-08-01") is True   # marker written
    assert day_run_status(root, "2026-08-01") == {}
    assert os.path.exists(os.path.join(root, "shadow", "2026-08-01", "_run.json"))


# ---- SAFETY: subnet faults are excluded from both sides --------------------

def test_docker_not_available_never_strikes():
    decision = observe_day({}, [], DAY, {HK: (False, ERR_DOCKER_UNAVAILABLE)}, P)
    assert decision.struck == ()
    assert decision.excluded == (HK,)
    assert [e.kind for e in decision.events] == [KIND_EXCLUDED]
    assert decision.events[0].fault == FAULT_SUBNET
    assert strikes_in_window(decision.events, HK, DAY, P.window_days) == 0


def test_subnet_fault_days_are_excluded_from_the_denominator_too():
    """A subnet-fault day is not a clean day: it must neither add a strike
    nor let a miner off one. Four miner-fault days with a subnet-fault day
    interleaved still evict on the fifth miner-fault day."""
    days = []
    for i in (10, 9, 8, 7):
        days.append((_d(i), _fail()))
    days.append((_d(6), {HK: (False, ERR_DOCKER_UNAVAILABLE)}))  # our fault
    days.append((_d(5), _fail()))                                # the 5th strike
    states, log, last = _run_days({}, [], days, P)
    assert last.evicted == (HK,)
    assert strikes_in_window(log, HK, _d(5), P.window_days) == 5


def test_pull_timeout_is_unruled_and_counts_toward_nothing():
    decision = observe_day({}, [], DAY,
                           {HK: (False, f"{ERR_PULL_TIMEOUT_PREFIX}600s")}, P)
    assert decision.struck == () and decision.excluded == (HK,)
    assert decision.events[0].fault == FAULT_UNKNOWN


# ---- strike accrual --------------------------------------------------------

def test_strikes_accrue_one_per_failed_day():
    days = [(_d(i), _fail()) for i in (4, 3, 2)]
    _, log, _ = _run_days({}, [], days, P)
    assert strikes_in_window(log, HK, DAY, P.window_days) == 3
    # scoped to HK: the healthy bystander logs its own clean days
    assert [e.kind for e in log if e.hotkey == HK] == [KIND_STRIKE] * 3


def test_timeout_and_nonzero_exit_both_strike():
    days = [(_d(1), {HK: (False, f"{ERR_TIMEOUT_PREFIX}900s (container killed)")}),
            (DAY, {HK: (False, f"{ERR_EXIT_PREFIX}137: b''")})]
    _, log, _ = _run_days({}, [], days, P)
    assert strikes_in_window(log, HK, DAY, P.window_days) == 2
    assert {e.reason for e in log} == {REASON_TIMEOUT, REASON_MEMORY}


def test_same_day_rerun_that_succeeds_is_not_a_strike(tmp_path):
    """record_day APPENDS; the last record is the operative run. A failed
    run followed by a successful re-run the same day must read clean."""
    root = str(tmp_path)
    model = ShadowModel(HK, "img@sha256:a", "2026-08-01")
    record_day(root, "2026-08-11", model,
               RunResult(ok=False, error=f"{ERR_EXIT_PREFIX}1: b'boom'"))
    record_day(root, "2026-08-11", model,
               RunResult(ok=True, predictions_out=5))

    runs = day_run_status(root, "2026-08-11")
    assert runs == {HK: (True, None)}
    decision = observe_day({}, [], DAY, runs, P)
    assert decision.struck == ()


def test_a_recorded_strike_is_retracted_by_a_same_day_recovery():
    """If the loop already recorded the failure before the re-run, the later
    observation supersedes it (last record wins per day)."""
    first = observe_day({}, [], DAY, _fail(), P)
    assert first.struck == (HK,)
    second = observe_day(first.states, first.events, DAY, _ok(), P)
    log = list(first.events) + list(second.events)
    assert strikes_in_window(log, HK, DAY, P.window_days) == 0


def test_reobserving_an_unchanged_day_appends_nothing():
    first = observe_day({}, [], DAY, _fail(), P)
    second = observe_day(first.states, first.events, DAY, _fail(), P)
    assert second.events == ()          # idempotent re-run of the daily loop


# ---- retraction is WHOLE: the eviction goes too ----------------------------

def _evict_then_recover_same_day(params=P):
    """Five failed days ending ON `DAY` -> eviction; then the operator's
    same-day re-run succeeds and the loop re-observes."""
    days = [(_d(4 - i), _fail()) for i in range(5)]
    states, log, last = _run_days({}, [], days, params)
    assert last.evicted == (HK,) and last.states[HK].evicted_on == DAY
    back = observe_day(states, log, DAY, _ok(), params)
    return states, list(log) + list(back.events), back


def test_a_retracted_strike_retracts_the_eviction_it_caused():
    """The documented safety property in full: 'a recovered day must never
    become a permanent strike'. Clearing evicted_on alone is not enough —
    evictions_total and last_eviction_on are the two fields with teeth."""
    _, _, back = _evict_then_recover_same_day()
    st = back.states[HK]
    assert back.retracted == (HK,)
    assert st.evicted_on is None
    assert st.evictions_total == 0              # was 1: a phantom offence
    assert st.last_eviction_on is None          # was DAY
    assert st.reinstate_not_before is None
    assert [e.kind for e in back.events][-1] == "eviction_retracted"


def test_a_retracted_eviction_does_not_wipe_the_real_strike_history():
    """`since=last_eviction_on` hard-excludes every day at or before the
    eviction. If a retracted eviction leaves last_eviction_on set, four
    genuine strikes vanish and the miner gets a fresh five chances."""
    _, log, back = _evict_then_recover_same_day()
    st = back.states[HK]
    assert strikes_in_window(log, HK, DAY, P.window_days,
                             since=st.last_eviction_on) == 4
    # and the fifth bad day evicts him again, on the real count
    after = observe_day(back.states, log, DAY + timedelta(days=1), _fail(), P)
    assert after.evicted == (HK,)
    ev = [e for e in after.events if e.kind == KIND_EVICTED][0]
    assert ev.detail["strikes"] == 5


def test_a_retracted_eviction_does_not_make_the_next_one_a_repeat():
    """_is_repeat reads evictions_total/last_eviction_on. A retracted
    eviction leaving them set costs the miner clause (b)'s instant re-entry
    and makes his next eviction serve the 14-day repeat cooldown."""
    _, log, back = _evict_then_recover_same_day()
    nxt = DAY + timedelta(days=1)
    after = observe_day(back.states, log, nxt, _fail(), P)
    st = after.states[HK]
    assert st.evictions_total == 1                       # this is his FIRST
    assert st.reinstate_not_before == nxt                # no cooldown served
    ev = [e for e in after.events if e.kind == KIND_EVICTED][0]
    assert ev.detail["repeat"] is False


def test_a_genuine_second_eviction_is_still_a_repeat_after_a_retraction():
    """Retraction must not become an amnesty machine: an eviction that was
    NOT retracted still counts, so the next one serves the cooldown."""
    params = ChronicFailureParams(strikes_to_evict=2, window_days=14,
                                  repeat_review_days=14)
    d0 = date(2026, 8, 1)
    states, log, first = _run_days(
        {}, [], [(d0, _fail()), (d0 + timedelta(days=1), _fail())], params)
    assert first.evicted == (HK,)
    back = observe_day(states, log, d0 + timedelta(days=2), _ok(), params)
    log = log + list(back.events)
    assert back.reinstated == (HK,) and back.retracted == ()
    assert back.states[HK].evictions_total == 1   # a real eviction survives

    states, log, second = _run_days(
        back.states, log,
        [(d0 + timedelta(days=5), _fail()), (d0 + timedelta(days=6), _fail())],
        params)
    assert second.evicted == (HK,)
    assert states[HK].evictions_total == 2
    assert states[HK].reinstate_not_before == \
        d0 + timedelta(days=6) + timedelta(days=14)


def test_an_eviction_is_also_retracted_when_the_rerun_hits_a_subnet_fault():
    """The re-run need not SUCCEED for the strike to go: a subnet-fault
    re-run supersedes the day too (last record wins), and it counts toward
    nothing — so the eviction it caused must unwind, but the miner is not
    re-admitted-by-clean-run either."""
    days = [(_d(4 - i), _fail()) for i in range(5)]
    states, log, last = _run_days({}, [], days, P)
    assert last.evicted == (HK,)
    back = observe_day(states, log, DAY,
                       {HK: (False, ERR_DOCKER_UNAVAILABLE)}, P)
    assert back.retracted == (HK,)
    assert back.excluded == (HK,)
    assert back.reinstated == ()                 # nothing to re-admit
    st = back.states[HK]
    assert (st.evicted_on, st.evictions_total, st.last_eviction_on) == \
        (None, 0, None)
    assert evicted_hotkeys(back.states, DAY) == frozenset()


def test_retraction_survives_a_round_trip_through_the_ledger(tmp_path):
    """The daily loop reloads state and log from disk between runs, so the
    reconstruction retract_eviction does must work off persisted events."""
    root = str(tmp_path)
    days = [(_d(4 - i), _fail()) for i in range(5)]
    states, log, last = _run_days({}, [], days, P)
    sl.save_eviction_states(root, states)
    sl.append_strike_events(root, log)

    reloaded_states = sl.load_eviction_states(root)
    reloaded_log = sl.load_strike_events(root)
    back = observe_day(reloaded_states, reloaded_log, DAY, _ok(), P)
    assert back.retracted == (HK,)
    assert back.states[HK].evictions_total == 0
    assert back.states[HK].last_eviction_on is None


# ---- the rolling window boundary ------------------------------------------

def test_window_boundary_fires_exactly_at_the_edge_and_not_one_short():
    """M=14 means the 14 calendar days ENDING ON today: today and the 13
    before. A fifth strike 14 days back is outside and must not evict; the
    same strike one day later is inside and must."""
    outside = [(_d(14), _fail())] + [(_d(i), _fail()) for i in (3, 2, 1, 0)]
    _, log_out, last_out = _run_days({}, [], outside, P)
    assert strikes_in_window(log_out, HK, DAY, P.window_days) == 4
    assert last_out.evicted == ()

    inside = [(_d(13), _fail())] + [(_d(i), _fail()) for i in (3, 2, 1, 0)]
    _, log_in, last_in = _run_days({}, [], inside, P)
    assert strikes_in_window(log_in, HK, DAY, P.window_days) == 5
    assert last_in.evicted == (HK,)


def test_four_strikes_never_evict_under_a_five_strike_policy():
    days = [(_d(i), _fail()) for i in (3, 2, 1, 0)]
    states, _, last = _run_days({}, [], days, P)
    assert last.evicted == ()
    assert evicted_hotkeys(states, DAY) == frozenset()


def test_future_dated_records_cannot_evict():
    log = [StrikeEvent(day=DAY + timedelta(days=k), hotkey=HK, kind=KIND_STRIKE,
                       fault=FAULT_MINER) for k in range(1, 6)]
    assert strikes_in_window(log, HK, DAY, P.window_days) == 0


def test_strikes_are_per_miner():
    days = [(_d(i), {HK: (False, f"{ERR_EXIT_PREFIX}1: b''"),
                     "minerB": (True, None)}) for i in (4, 3, 2, 1, 0)]
    states, log, last = _run_days({}, [], days, P)
    assert last.evicted == (HK,)
    assert strikes_in_window(log, "minerB", DAY, P.window_days) == 0
    assert evicted_hotkeys(states, DAY) == frozenset({HK})


# ---- eviction and re-entry -------------------------------------------------

def _evict_once(params=P, start=5):
    """Drive one miner to a first eviction; returns (states, log, day)."""
    days = [(_d(start + 4 - i), _fail()) for i in range(5)]
    states, log, last = _run_days({}, [], days, params)
    assert last.evicted == (HK,)
    return states, log, _d(start)


def test_first_eviction_returns_on_the_first_clean_run():
    states, log, ev_day = _evict_once()
    assert states[HK].evictions_total == 1
    # clause (b): no waiting period on a FIRST eviction
    assert states[HK].reinstate_not_before == ev_day
    assert evicted_hotkeys(states, ev_day) == frozenset({HK})

    back = observe_day(states, log, ev_day + timedelta(days=1), _ok(), P)
    assert back.reinstated == (HK,)
    assert back.states[HK].evicted_on is None
    assert evicted_hotkeys(back.states, ev_day + timedelta(days=1)) == frozenset()
    # scoped to HK: the healthy bystander logs its own clean day too
    assert [e.kind for e in back.events if e.hotkey == HK] == \
        [KIND_CLEAN, KIND_REINSTATED]
    # history survives re-entry — it is what makes the next one a repeat
    assert back.states[HK].evictions_total == 1
    assert back.states[HK].last_eviction_on == ev_day


def test_an_evicted_miner_does_not_return_on_a_subnet_fault_day():
    states, log, ev_day = _evict_once()
    nxt = ev_day + timedelta(days=1)
    still_out = observe_day(states, log, nxt,
                            {HK: (False, ERR_DOCKER_UNAVAILABLE)}, P)
    assert still_out.reinstated == ()
    assert evicted_hotkeys(still_out.states, nxt) == frozenset({HK})


def test_strikes_before_an_eviction_do_not_re_evict_on_return():
    """Without the `since` cut, a re-admitted miner would carry five old
    strikes and be evicted again by his very next bad day."""
    states, log, ev_day = _evict_once()
    back = observe_day(states, log, ev_day + timedelta(days=1), _ok(), P)
    log = list(log) + list(back.events)

    after = observe_day(back.states, log, ev_day + timedelta(days=2), _fail(), P)
    assert after.struck == (HK,)
    assert after.evicted == ()          # one strike, not six
    assert strikes_in_window(log + list(after.events), HK,
                             ev_day + timedelta(days=2), P.window_days,
                             since=after.states[HK].last_eviction_on) == 1


def test_repeat_eviction_serves_the_cooldown():
    """Second eviction: a clean run alone is not enough until
    last_eviction_on + repeat_review_days."""
    params = ChronicFailureParams(strikes_to_evict=2, window_days=14,
                                  repeat_review_days=14)
    d0 = date(2026, 8, 1)
    states, log, first = _run_days(
        {}, [], [(d0, _fail()), (d0 + timedelta(days=1), _fail())], params)
    assert first.evicted == (HK,)

    # first eviction -> straight back on the next clean run
    back = observe_day(states, log, d0 + timedelta(days=2), _ok(), params)
    log = log + list(back.events)
    assert back.reinstated == (HK,)

    # second eviction, 3 and 4 days later
    states, log, second = _run_days(
        back.states, log,
        [(d0 + timedelta(days=3), _fail()), (d0 + timedelta(days=4), _fail())],
        params)
    assert second.evicted == (HK,)
    ev2 = d0 + timedelta(days=4)
    st = states[HK]
    assert st.evictions_total == 2
    assert st.reinstate_not_before == ev2 + timedelta(days=14)

    # a clean run one day later does NOT re-admit …
    early = observe_day(states, log, ev2 + timedelta(days=1), _ok(), params)
    assert early.reinstated == ()
    assert evicted_hotkeys(early.states, ev2 + timedelta(days=1)) == frozenset({HK})

    # … and neither does one on the last day of the wait …
    day13 = ev2 + timedelta(days=13)
    late = observe_day(early.states, log, day13, _ok(), params)
    assert late.reinstated == ()

    # … but the first clean run on/after the review date does.
    day14 = ev2 + timedelta(days=14)
    done = observe_day(late.states, log, day14, _ok(), params)
    assert done.reinstated == (HK,)
    assert done.states[HK].evicted_on is None
    assert done.events[-1].detail["served_repeat_wait"] is True


def test_manual_reinstate_respects_the_cooldown():
    states = {HK: EvictionState(hotkey=HK, evicted_on=DAY, evictions_total=2,
                                last_eviction_on=DAY,
                                reinstate_not_before=DAY + timedelta(days=14))}
    unchanged, event = reinstate(states, HK, DAY + timedelta(days=3), P)
    assert event is None and unchanged[HK].evicted_on == DAY
    changed, event = reinstate(states, HK, DAY + timedelta(days=14), P)
    assert event is not None and changed[HK].evicted_on is None
    # a miner who is not evicted is a no-op, never an error
    assert reinstate({}, "ghost", DAY, P) == ({}, None)


def test_eviction_event_carries_the_numbers_that_produced_it():
    states, log, ev_day = _evict_once()
    ev = [e for e in log if e.kind == KIND_EVICTED][-1]
    assert ev.detail["strikes"] == 5
    assert ev.detail["strikes_to_evict"] == P.strikes_to_evict
    assert ev.detail["window_days"] == P.window_days
    assert ev.detail["repeat"] is False
    assert ev.detail["reinstate_not_before"] == str(ev_day)


def test_observe_day_is_deterministic_regardless_of_input_order():
    runs_a = {"m2": (False, f"{ERR_EXIT_PREFIX}1: b''"),
              "m1": (False, f"{ERR_EXIT_PREFIX}1: b''")}
    runs_b = {"m1": (False, f"{ERR_EXIT_PREFIX}1: b''"),
              "m2": (False, f"{ERR_EXIT_PREFIX}1: b''")}
    a = observe_day({}, [], DAY, runs_a, P)
    b = observe_day({}, [], DAY, runs_b, P)
    assert [(e.hotkey, e.kind) for e in a.events] == \
           [(e.hotkey, e.kind) for e in b.events]
    assert a.states == b.states


# ---- persistence -----------------------------------------------------------

def test_eviction_state_and_strike_log_round_trip(tmp_path):
    root = str(tmp_path)
    assert sl.load_eviction_states(root) == {}
    assert sl.load_strike_events(root) == []

    states, log, ev_day = _evict_once()
    sl.save_eviction_states(root, states)
    assert sl.append_strike_events(root, log) == len(log)

    assert sl.load_eviction_states(root) == states
    reloaded = sl.load_strike_events(root)
    assert reloaded == log
    # control files must be invisible to the standings reader
    assert sl.load_entries(root, as_of=DAY) == {}
    assert sl.compact(root, as_of=DAY) == 0
    assert os.path.exists(os.path.join(root, "standing", "_eviction_state.json"))
    assert os.path.exists(os.path.join(root, "standing", "_strikes.jsonl"))


def test_strike_log_is_append_only(tmp_path):
    root = str(tmp_path)
    sl.append_strike_events(root, [StrikeEvent(day=DAY, hotkey=HK,
                                               kind=KIND_STRIKE,
                                               fault=FAULT_MINER)])
    sl.append_strike_events(root, [StrikeEvent(day=DAY + timedelta(days=1),
                                               hotkey=HK, kind=KIND_CLEAN)])
    with open(os.path.join(root, "standing", "_strikes.jsonl")) as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert [ln["kind"] for ln in lines] == [KIND_STRIKE, KIND_CLEAN]
    assert sl.append_strike_events(root, []) == 0


# ---- the log does not grow without bound ([finding 7]) ---------------------

def _year_of_events(hotkey=HK, days=365, ending=DAY):
    return [StrikeEvent(day=ending - timedelta(days=i), hotkey=hotkey,
                        kind=KIND_CLEAN) for i in range(days)]


def test_a_windowed_read_drops_out_of_window_observations(tmp_path):
    """compact() skips underscore-prefixed control files by construction, so
    _strikes.jsonl was the one append-only file with no read-time cut and no
    compaction path at all. Every daily-loop run reloaded a year of history
    to answer a 14-day question."""
    root = str(tmp_path)
    sl.append_strike_events(root, _year_of_events())
    assert len(sl.load_strike_events(root)) == 365
    windowed = sl.load_strike_events(root, as_of=DAY, window_days=14)
    assert len(windowed) == 14
    assert min(e.day for e in windowed) == DAY - timedelta(days=13)


def test_a_windowed_read_never_drops_lifecycle_events(tmp_path):
    """Lifecycle events are what retract_eviction reconstructs
    evictions_total from — pruning them would corrupt state, not just an
    audit trail. They are O(evictions), not O(miners x days)."""
    root = str(tmp_path)
    old_eviction = StrikeEvent(day=DAY - timedelta(days=300), hotkey=HK,
                               kind=KIND_EVICTED, fault=FAULT_MINER)
    sl.append_strike_events(root, [old_eviction] + _year_of_events())
    windowed = sl.load_strike_events(root, as_of=DAY, window_days=14)
    assert [e for e in windowed if e.kind == KIND_EVICTED] == [old_eviction]


def test_compacting_the_strike_log_sheds_only_dead_observations(tmp_path):
    root = str(tmp_path)
    keep = StrikeEvent(day=DAY - timedelta(days=200), hotkey=HK,
                       kind=KIND_EVICTED, fault=FAULT_MINER)
    sl.append_strike_events(root, [keep] + _year_of_events())
    shed = sl.compact_strike_events(root, as_of=DAY, window_days=14)
    assert shed == 351                                  # 365 - 14 observations
    remaining = sl.load_strike_events(root)
    assert len(remaining) == 15                          # 14 clean + 1 evicted
    assert keep in remaining
    # idempotent: a second compaction has nothing left to shed
    assert sl.compact_strike_events(root, as_of=DAY, window_days=14) == 0


# ---- the flag --------------------------------------------------------------

def test_flag_is_off_by_default():
    assert chronic_failure_policy_enabled({}) is False
    assert chronic_failure_policy_enabled({"SN21_CHRONIC_FAILURE_POLICY": "on"})
    assert chronic_failure_policy_enabled({"SN21_CHRONIC_FAILURE_POLICY": "1"})
    assert not chronic_failure_policy_enabled({"SN21_CHRONIC_FAILURE_POLICY": "off"})


# ---- enforcement at the one filter point -----------------------------------

def _seed_standings(root, day, miners, days=14, per_day=25):
    """14 days x 25 episodes x 3 horizons per miner — 350 units of evidence
    mass, clearing the 250-prediction placement floor and the [D8] 14
    scored-day condition (same shape as test_daily_stream_weights)."""
    from hope.scoring.daily_score_flow import HorizonResult, day_flow
    for i in range(days):
        d = day - timedelta(days=days - 1 - i)
        results = []
        for ep in range(per_day):
            for hotkey, score in miners.items():
                for h in (7, 14, 28):
                    results.append(HorizonResult(f"e{i}-{ep}", h, hotkey, score, d))
        sl.append_entries(root, day_flow(results, d))


def _evicted_state(hotkey, day):
    return {hotkey: EvictionState(hotkey=hotkey, evicted_on=day,
                                  evictions_total=1, last_eviction_on=day,
                                  reinstate_not_before=day)}


def test_flag_off_leaves_the_weight_vector_untouched(tmp_path):
    """OFF must still be OFF at the money: the state says alpha is evicted,
    the weights must not know. (Eviction retracts MINER_MODEL_SPEC §2's
    published 'no additional punishment' — it ships with the amendment.)"""
    from hope.validator.daily_stream_weights import allocation_from_ledger

    off_root = str(tmp_path / "off")
    on_root = str(tmp_path / "on")
    for root in (off_root, on_root):
        _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4})
        sl.save_eviction_states(root, _evicted_state("alpha", DAY))

    off = allocation_from_ledger(off_root, DAY, 300, min_daily_episodes=0,
                                 environ={})
    assert off.evicted == ()
    assert set(off.weights) == {"alpha", "beta"}
    assert off.weights["alpha"] > off.weights["beta"]

    on = allocation_from_ledger(on_root, DAY, 300, min_daily_episodes=0,
                                environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})
    assert on.evicted == ("alpha",)
    assert set(on.weights) == {"beta"}
    # the curve renormalises over the survivors — no special-casing needed
    assert on.weights["beta"] == 1.0
    # standings are FACTS: the ledger entries are untouched either way
    assert set(sl.load_entries(on_root, as_of=DAY)) == {"alpha", "beta"}


def test_evicted_champion_vacates_the_seat(tmp_path):
    """Omission is not enough: champion_promotion's champ_avg-is-None branch
    would keep a crashing incumbent seated for the whole 7-day hold."""
    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4})
    seated = allocation_from_ledger(root, DAY, 300, min_daily_episodes=0,
                                    environ={})
    assert seated.promotion.state.champion == "alpha"

    sl.save_eviction_states(root, _evicted_state("alpha", DAY))
    nxt = DAY + timedelta(days=1)
    after = allocation_from_ledger(root, nxt, 300, min_daily_episodes=0,
                                   environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})
    assert "alpha" not in after.weights
    assert after.promotion.state.champion == "beta"   # seat refilled, not held

    with open(os.path.join(root, "standing", "_promotion_log.jsonl")) as f:
        kinds = [json.loads(ln)["type"] for ln in f if ln.strip()]
    assert "vacate" in kinds


def test_an_emergency_replacement_is_not_logged_as_a_cold_start(tmp_path):
    """[finding 3] vacate_seat and the reseat happen inside ONE
    allocation_from_ledger call, so the replacement of an evicted champion
    was emitted as `initial_seat` — indistinguishable from a cold start for
    any downstream reader (onchain_runner prints promoted_today=True for
    both). The reseat is deliberate (an empty seat means no live model at
    all), but it must be LABELLED as what it is."""
    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4})
    cold = allocation_from_ledger(root, DAY, 300, min_daily_episodes=0,
                                  environ={})
    assert cold.promotion.event["type"] == "initial_seat"   # a real cold start

    sl.save_eviction_states(root, _evicted_state("alpha", DAY))
    nxt = DAY + timedelta(days=1)
    after = allocation_from_ledger(root, nxt, 300, min_daily_episodes=0,
                                   environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})
    assert after.promotion.event["type"] == "seat_after_vacate"
    assert after.promotion.event["champion"] == "beta"
    assert after.promotion.event["reason"] == "chronic_failure_eviction"
    assert after.promotion.event["vacated_on"] == str(nxt)

    with open(os.path.join(root, "standing", "_promotion_log.jsonl")) as f:
        kinds = [json.loads(ln)["type"] for ln in f if ln.strip()]
    assert kinds == ["initial_seat", "vacate", "seat_after_vacate"]

    # the marker is consumed by the reseat: the NEXT cold start is a cold
    # start again, and the persisted state does not carry a stale vacancy
    assert after.promotion.state.vacated_on is None
    assert sl.load_promotion_state(root).vacated_on is None


def test_a_vacated_seat_that_cannot_be_refilled_stays_marked(tmp_path):
    """Nobody eligible survives the eviction: the seat stays empty and the
    vacancy marker persists, so whenever it IS refilled the log still says
    `seat_after_vacate` rather than pretending the subnet just started."""
    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"alpha": 0.9})
    allocation_from_ledger(root, DAY, 300, min_daily_episodes=0, environ={})
    sl.save_eviction_states(root, _evicted_state("alpha", DAY))
    nxt = DAY + timedelta(days=1)
    on = {"SN21_CHRONIC_FAILURE_POLICY": "on"}
    after = allocation_from_ledger(root, nxt, 300, min_daily_episodes=0,
                                   environ=on)
    assert after.promotion.state.champion is None
    assert sl.load_promotion_state(root).vacated_on == nxt

    # alpha comes back (state cleared); the reseat is still labelled honestly
    sl.save_eviction_states(root, {})
    back = allocation_from_ledger(root, nxt + timedelta(days=1), 300,
                                  min_daily_episodes=0, environ=on)
    assert back.promotion.event["type"] == "seat_after_vacate"
    assert back.promotion.state.champion == "alpha"


# ---- [finding 8] the declared blast radius, made loud -----------------------

def test_enforcement_emptying_the_earning_set_is_announced_not_patched(tmp_path, caplog):
    """Evicting the only placement-eligible model empties the whole weight
    vector — which on this subnet today is the entire subnet (reference-v1
    is the only model that has ever run in shadow). That is a GOVERNANCE
    call, not a bug to floor around, so the behaviour is unchanged and the
    event is announced instead."""
    import logging

    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"reference-v1": 0.9})
    sl.save_eviction_states(root, _evicted_state("reference-v1", DAY))

    with caplog.at_level(logging.WARNING,
                         logger="hope.validator.daily_stream_weights"):
        alloc = allocation_from_ledger(
            root, DAY, 300, min_daily_episodes=0,
            environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})

    # behaviour deliberately UNCHANGED
    assert alloc.weights == {}
    assert alloc.earning_set_size == 0
    assert alloc.evicted == ("reference-v1",)
    # …and loud
    assert alloc.enforcement_emptied_earning_set is True
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    msg = caplog.records[0].getMessage()
    assert "SN21_CHRONIC_FAILURE_POLICY" in msg
    assert "reference-v1" in msg
    # the counterfactual used to detect this must not leak into what is
    # persisted: it seats a champion, the real day does not
    assert alloc.promotion.state.champion is None
    assert sl.load_promotion_state(root).champion is None


def test_an_empty_earning_set_that_enforcement_did_not_cause_is_not_announced(
        tmp_path, caplog):
    """The warning must name a real cause. A day with nobody
    placement-eligible is empty with or without the flag, and must not be
    blamed on eviction."""
    import logging

    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    # 2 days x 1 episode: far below the 250-prediction placement floor
    _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4}, days=2, per_day=1)
    sl.save_eviction_states(root, _evicted_state("alpha", DAY))

    with caplog.at_level(logging.WARNING,
                         logger="hope.validator.daily_stream_weights"):
        alloc = allocation_from_ledger(
            root, DAY, 300, min_daily_episodes=0,
            environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})

    assert alloc.weights == {}
    assert alloc.enforcement_emptied_earning_set is False
    assert caplog.records == []


def test_a_gated_day_is_not_reported_as_an_enforcement_emptying(tmp_path, caplog):
    """[D3] withholds the weight update on thin volume; that empties the
    vector for a reason that has nothing to do with liveness."""
    import logging

    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4})
    sl.save_eviction_states(root, _evicted_state("alpha", DAY))

    with caplog.at_level(logging.WARNING,
                         logger="hope.validator.daily_stream_weights"):
        alloc = allocation_from_ledger(
            root, DAY, 10, min_daily_episodes=150,
            environ={"SN21_CHRONIC_FAILURE_POLICY": "on"})

    assert alloc.gated is True and alloc.weights == {}
    assert alloc.enforcement_emptied_earning_set is False
    assert caplog.records == []


def test_champion_seat_is_held_when_enforcement_is_off(tmp_path):
    from hope.validator.daily_stream_weights import allocation_from_ledger

    root = str(tmp_path)
    _seed_standings(root, DAY, {"alpha": 0.9, "beta": 0.4})
    allocation_from_ledger(root, DAY, 300, min_daily_episodes=0, environ={})
    sl.save_eviction_states(root, _evicted_state("alpha", DAY))
    after = allocation_from_ledger(root, DAY + timedelta(days=1), 300,
                                   min_daily_episodes=0, environ={})
    assert after.promotion.state.champion == "alpha"


# ---- regression: retraction must not resurrect a cancelled eviction ---------

def test_a_second_same_day_recovery_does_not_resurrect_the_first_eviction():
    """The append-only log keeps a retracted eviction's KIND_EVICTED line
    forever; KIND_EVICTION_RETRACTED is what cancels it. Reconstructing
    evictions_total from the raw KIND_EVICTED lines therefore counted evictions
    that had already been undone — so a miner who recovered same-day twice came
    back branded a repeat offender, with last_eviction_on pointing at the
    cancelled eviction. That date hard-excludes his genuine strike history via
    strikes_in_window(since=...), which is the precise damage retract_eviction
    exists to prevent."""
    hk = "alpha"
    events = [
        StrikeEvent(day=date(2026, 8, 5), hotkey=hk, kind=KIND_EVICTED,
                       fault=FAULT_MINER),
        StrikeEvent(day=date(2026, 8, 5), hotkey=hk,
                       kind=KIND_EVICTION_RETRACTED,
                       fault=FAULT_MINER),
        StrikeEvent(day=date(2026, 8, 20), hotkey=hk, kind=KIND_EVICTED,
                       fault=FAULT_MINER),
    ]
    st = EvictionState(hotkey=hk, evicted_on=date(2026, 8, 20),
                          evictions_total=1, last_eviction_on=date(2026, 8, 20))
    new, _ = retract_eviction(st, events, date(2026, 8, 20))
    assert new.evictions_total == 0
    assert new.last_eviction_on is None
    assert new.evicted_on is None


def test_a_genuine_prior_eviction_still_counts_after_a_retraction():
    """Control for the above: only RETRACTED evictions are discounted. A real
    earlier eviction must survive, or a retraction would launder history."""
    hk = "alpha"
    events = [
        StrikeEvent(day=date(2026, 8, 5), hotkey=hk, kind=KIND_EVICTED,
                       fault=FAULT_MINER),
        StrikeEvent(day=date(2026, 8, 20), hotkey=hk, kind=KIND_EVICTED,
                       fault=FAULT_MINER),
    ]
    st = EvictionState(hotkey=hk, evicted_on=date(2026, 8, 20),
                          evictions_total=2, last_eviction_on=date(2026, 8, 20))
    new, _ = retract_eviction(st, events, date(2026, 8, 20))
    assert new.evictions_total == 1
    assert new.last_eviction_on == date(2026, 8, 5)


# ---- ROB'S FLOOR: the field must never be emptied (ruled 2026-08-03) --------
#
# "We have to not evict all miners - we need at least 1 house model (Rabbia)."
#
# With the policy on, evicting the last placement-eligible model empties the
# weight vector and the subnet pays NOBODY. Not a distant edge case: the
# reference model is the only model that has ever run, so today the last model
# IS the only model — and five validators mirror our vector within the hour.

def test_the_only_model_is_never_evicted():
    """THE ONE ROB RULED ON. Five failed days, eviction fully earned, and it
    must still not happen because there is nobody else."""
    states, log, last = _run_days({}, [], [
        (_d(n), _fail(with_bystander=False)) for n in (4, 3, 2, 1, 0)
    ])
    assert last.evicted == ()
    assert last.withheld == (HK,)
    assert states[HK].evicted_on is None
    assert evicted_hotkeys(states, DAY) == frozenset()


def test_the_withheld_eviction_is_recorded_not_swallowed():
    """A blocked eviction must be visible. Silently skipping it would make the
    subnet look healthy while a model that earned eviction kept earning."""
    _, log, last = _run_days({}, [], [
        (_d(n), _fail(with_bystander=False)) for n in (4, 3, 2, 1, 0)
    ])
    withheld = [e for e in log if e.kind == KIND_EVICTION_WITHHELD]
    assert len(withheld) == 1
    assert withheld[0].hotkey == HK
    assert "below the floor" in withheld[0].reason


def test_the_strike_history_survives_so_eviction_can_land_later():
    """The floor delays the eviction, it does not forgive it. Once a second
    model exists the same strike record must still evict."""
    states, log, _ = _run_days({}, [], [
        (_d(n), _fail(with_bystander=False)) for n in (5, 4, 3, 2, 1)
    ])
    assert strikes_in_window(log, HK, DAY, P.window_days) >= P.strikes_to_evict
    # a second model appears, then HK fails again
    after = observe_day(states, log, DAY, _fail(), P)
    assert after.evicted == (HK,)


def test_eviction_proceeds_normally_when_someone_remains():
    """The guard must not become a blanket amnesty."""
    states, _, last = _run_days({}, [], [
        (_d(n), _fail()) for n in (4, 3, 2, 1, 0)
    ])
    assert last.evicted == (HK,)
    assert last.withheld == ()
    assert states[BYSTANDER].evicted_on is None


def test_the_last_survivor_of_a_larger_field_is_also_protected():
    """Not just 'one model total' — the floor is about what REMAINS. Evict
    them one at a time and the last one standing must still be spared, or the
    guard is trivially defeated by doing it slowly."""
    a, b = "model-a", "model-b"
    P2 = replace(P, strikes_to_evict=1)
    states, log, _ = _run_days({}, [], [
        (_d(2), {a: (False, f"{ERR_EXIT_PREFIX}1: b'boom'"), b: (True, None)}),
    ], params=P2)
    assert states[a].evicted_on is not None          # a is gone
    last = observe_day(states, log, _d(1),
                       {b: (False, f"{ERR_EXIT_PREFIX}1: b'boom'")}, P2)
    assert last.evicted == ()                        # b is all that is left
    assert last.withheld == (b,)


def test_the_house_model_is_never_evicted_even_with_others_present():
    """Rob's second requirement: a designated house model. Distinct from the
    survivor floor — this one holds even when the field is healthy."""
    P3 = replace(P, house_hotkey=HK)
    states, _, last = _run_days({}, [], [
        (_d(n), _fail()) for n in (4, 3, 2, 1, 0)
    ], params=P3)
    assert last.evicted == ()
    assert last.withheld == (HK,)
    assert "house model" in [e.reason for e in last.events
                             if e.kind == KIND_EVICTION_WITHHELD][0]


def test_house_hotkey_and_min_survivors_come_from_env():
    assert house_hotkey_from({}) is None
    assert house_hotkey_from({HOUSE_HOTKEY_ENV: "  rabbia  "}) == "rabbia"
    assert min_survivors_from({}) == 1
    assert min_survivors_from({MIN_SURVIVORS_ENV: "3"}) == 3


def test_min_survivors_cannot_be_set_to_zero():
    """Zero would reinstate exactly the failure Rob ruled against, so a deploy
    typo must not be able to switch his floor off."""
    assert min_survivors_from({MIN_SURVIVORS_ENV: "0"}) == 1
    assert min_survivors_from({MIN_SURVIVORS_ENV: "-5"}) == 1
    assert min_survivors_from({MIN_SURVIVORS_ENV: "none"}) == 1
