"""The eviction rule as published (MINER_MODEL_SPEC §5, notice of 2026-11-10, in force
from the run of 2026-11-17), after a miner's review of 12 Nov 2026:

1. Nothing evicted before the rule's first day is enforced or counts as a previous
   eviction; only strikes inside the published 14-day window count.
2. A failure on our side, or one that cannot be attributed to the model, is never a
   strike; a cancelled absence charge removes the strike for that day.
3. Every eviction returns after 7 days, on a clean run.
4. The registration status feed shows each hotkey's strikes and any eviction.
"""
from datetime import date, timedelta

from hope.backtest.container_runner import ERR_EXIT_PREFIX, ERR_TIMEOUT_PREFIX
from hope.scoring import chronic_failure as cf
from hope.scoring.chronic_failure import (
    FAULT_MINER,
    FAULT_SUBNET,
    FAULT_UNKNOWN,
    KIND_EVICTED,
    KIND_PRE_RULE_CLEARED,
    ChronicFailureParams,
    EvictionState,
    StrikeEvent,
    apply_cancellations,
    classify_failure,
    evicted_hotkeys,
    observe_day,
    params_from_env,
    strikes_in_window,
)

HK, OTHER = "5Miner", "5Other"
RULE = date(2026, 11, 16)            # first observed (basket) day, served by the run of 17 Nov
P = ChronicFailureParams(min_survivors=1, effective_from=RULE)
CRASH = (False, f"{ERR_EXIT_PREFIX}1: Traceback (most recent call last)")
CLEAN = (True, None)


def _fold(states, log, days, params=P):
    last = None
    for d, runs in days:
        last = observe_day(states, log, d, runs, params)
        states = last.states
        log = log + list(last.events)
    return states, log, last


def _days(start, n, runs):
    return [(start + timedelta(days=i), runs) for i in range(n)]


# ---- 1. a fresh start on the rule's first day -----------------------------------------

def test_the_rule_date_comes_from_the_environment():
    assert params_from_env({cf.EFFECTIVE_FROM_ENV: "2026-11-16"}).effective_from == RULE
    assert params_from_env({cf.EFFECTIVE_FROM_ENV: "soon"}).effective_from is None
    assert params_from_env({}).effective_from is None


def test_an_eviction_from_before_the_rule_is_cleared_on_its_first_day():
    states = {HK: EvictionState(hotkey=HK, evicted_on=date(2026, 8, 20), evictions_total=2,
                                last_eviction_on=date(2026, 8, 20),
                                reinstate_not_before=date(2026, 8, 27))}
    assert evicted_hotkeys(states, RULE) == {HK}                  # as stored today
    states, log, last = _fold(states, [], [(RULE, {OTHER: CLEAN})])
    assert evicted_hotkeys(states, RULE) == frozenset()
    assert states[HK] == EvictionState(hotkey=HK)
    assert [e.kind for e in last.events if e.hotkey == HK] == [KIND_PRE_RULE_CLEARED]
    _, _, again = _fold(states, log, [(RULE + timedelta(days=1), {OTHER: CLEAN})])
    assert not [e for e in again.events if e.hotkey == HK]       # cleared once


def test_before_the_rule_day_nothing_is_cleared():
    states = {HK: EvictionState(hotkey=HK, evicted_on=date(2026, 8, 20), evictions_total=1,
                                last_eviction_on=date(2026, 8, 20))}
    states, _, _ = _fold(states, [], [(RULE - timedelta(days=1), {OTHER: CLEAN})])
    assert states[HK].evicted_on == date(2026, 8, 20)


def test_strikes_in_the_published_window_count_on_the_first_day():
    """§5: on 17 Nov the window reaches back to the run of 3 Nov."""
    states, log, _ = _fold({}, [], _days(RULE - timedelta(days=4), 4, {HK: CRASH, OTHER: CLEAN}),
                           params=ChronicFailureParams(min_survivors=1))       # recorded, off
    _, _, last = _fold(states, log, [(RULE, {HK: CRASH, OTHER: CLEAN})])
    assert last.evicted == (HK,)


def test_an_old_eviction_no_longer_cuts_the_window_or_makes_a_repeat():
    old = date(2026, 11, 10)
    states = {HK: EvictionState(hotkey=HK, evicted_on=None, evictions_total=1,
                                last_eviction_on=old)}
    log = [StrikeEvent(day=old, hotkey=HK, kind=KIND_EVICTED, fault=FAULT_MINER)]
    log += [StrikeEvent(day=old - timedelta(days=i), hotkey=HK, kind=cf.KIND_STRIKE,
                        fault=FAULT_MINER) for i in range(1, 3)]
    days = _days(old + timedelta(days=1), RULE.toordinal() - old.toordinal() + 2, {HK: CRASH, OTHER: CLEAN})
    _, log2, _ = _fold(states, log, days)
    evs = [e for e in log2 if e.kind == KIND_EVICTED and e.day >= RULE]
    assert evs and evs[0].detail["repeat"] is False
    assert evs[0].detail["reinstate_not_before"] == str(evs[0].day + timedelta(days=7))


# ---- 2. failures that are not the model's ---------------------------------------------

def test_our_side_and_unattributable_failures_are_never_strikes():
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}1: unshare: write failed /proc/self/uid_map") == FAULT_SUBNET
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}137: killed (SIGKILL) after 412s, before the CPU limit") \
        == FAULT_UNKNOWN
    assert classify_failure(False, f"{ERR_TIMEOUT_PREFIX}120s (container killed)") == FAULT_SUBNET   # our wall cut it
    assert classify_failure(False, "pull_failed: 403 denied") == FAULT_SUBNET
    assert classify_failure(False, "disk_low: 1.2 GB free") == FAULT_SUBNET
    assert classify_failure(False, "sandbox_not_available") == FAULT_SUBNET
    # still the model's own
    assert classify_failure(False, f"{ERR_TIMEOUT_PREFIX}900s: CPU time limit reached") == FAULT_MINER
    assert classify_failure(False, f"{ERR_TIMEOUT_PREFIX}960s (wall)") == FAULT_MINER
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}1: Traceback") == FAULT_MINER
    assert classify_failure(False, f"{ERR_EXIT_PREFIX}137: b''") == FAULT_MINER


def test_the_sandbox_wording_is_the_one_recognised():
    from hope.backtest.ns_sandbox import killed_child_error
    err = killed_child_error(-9, "", 300.0, 900)
    assert cf.KILLED_BEFORE_CPU_LIMIT in err and classify_failure(False, err) == FAULT_UNKNOWN
    assert classify_failure(False, killed_child_error(-9, "", 905.0, 900)) == FAULT_MINER


def _five_strikes_then_evicted():
    days = _days(RULE, 5, {HK: CRASH, OTHER: CLEAN})
    states, log, last = _fold({}, [], days)
    assert last.evicted == (HK,) and states[HK].evicted_on == RULE + timedelta(days=4)
    return states, log


def test_a_cancelled_charge_removes_its_strike_and_the_eviction_it_caused():
    states, log = _five_strikes_then_evicted()
    undo = apply_cancellations(states, log, [(RULE + timedelta(days=2), HK)], P)
    assert undo.retracted == (HK,)
    assert undo.states[HK].evicted_on is None and undo.states[HK].evictions_total == 0
    log2 = log + list(undo.events)
    assert strikes_in_window(log2, HK, RULE + timedelta(days=4), 14) == 4
    again = apply_cancellations(undo.states, log2, [(RULE + timedelta(days=2), HK)], P)
    assert again.events == ()                                  # idempotent


def test_a_cancellation_that_leaves_enough_strikes_keeps_the_eviction():
    days = _days(RULE, 6, {HK: CRASH, OTHER: CLEAN})
    states, log, _ = _fold({}, [], days)                       # evicted on day 5, struck on day 6 too
    undo = apply_cancellations(states, log, [(RULE + timedelta(days=5), HK)], P)
    assert undo.retracted == () and undo.states[HK].evicted_on == RULE + timedelta(days=4)


def test_a_cancellation_on_a_clean_day_changes_nothing():
    states, log, _ = _fold({}, [], [(RULE, {HK: CLEAN, OTHER: CLEAN})])
    assert apply_cancellations(states, log, [(RULE, HK)], P).events == ()


# ---- 3. return after 7 days, on a clean run -------------------------------------------

def test_a_first_eviction_waits_7_days_then_needs_a_clean_run():
    states, log = _five_strikes_then_evicted()
    evicted_on = states[HK].evicted_on
    assert states[HK].reinstate_not_before == evicted_on + timedelta(days=7)
    clean = {HK: CLEAN, OTHER: CLEAN}
    states, log, last = _fold(states, log, _days(evicted_on + timedelta(days=1), 6, clean))
    assert states[HK].evicted_on == evicted_on               # clean runs inside the 7 days: still out
    states, log, last = _fold(states, log, [(evicted_on + timedelta(days=7), {HK: CRASH, OTHER: CLEAN})])
    assert states[HK].evicted_on == evicted_on               # day 7 but not clean: still out
    states, log, last = _fold(states, log, [(evicted_on + timedelta(days=8), clean)])
    assert last.reinstated == (HK,) and states[HK].evicted_on is None


# ---- 4. visible in the registration status feed ---------------------------------------

def test_the_feed_shows_strikes_and_the_return_date():
    from hope.publication.registration_status_feed import add_liveness
    states, log = _five_strikes_then_evicted()
    rows = [{"hotkey": HK}, {"hotkey": OTHER}, {"hotkey": "5New"}]
    # The run of 21 Nov has seen baskets 16-20 Nov; dates are run days.
    out = add_liveness(rows, log, states, RULE + timedelta(days=5), P)
    assert out[0]["strikes"] == 5 and out[0]["evicted_on"] == "2026-11-21"
    assert out[0]["returns_from"] == "2026-11-28"
    assert out[1] == {"hotkey": OTHER, "strikes": 0}
    assert out[2] == {"hotkey": "5New", "strikes": 0}


def test_the_feed_hides_an_eviction_from_before_the_rule():
    from hope.publication.registration_status_feed import add_liveness
    states = {HK: EvictionState(hotkey=HK, evicted_on=date(2026, 9, 1), evictions_total=1,
                                last_eviction_on=date(2026, 9, 1), reinstate_not_before=date(2026, 9, 1))}
    out = add_liveness([{"hotkey": HK}], [], states, date(2026, 11, 13), P)
    assert out == [{"hotkey": HK, "strikes": 0}]
