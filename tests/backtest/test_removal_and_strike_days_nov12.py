"""Day keys around removals and strikes, after a test sweep of 12 Nov 2026.

1. A catch-up run is stamped with the run day its basket belongs to, not the clock.
2. A removed miner's new model is seated and charged like any model on its first run.
3. The days a removed model sat out stay uncharged after the new model is folded in.
4. A basket written after a later one still counts toward the 40 days.
5. A cancelled charge is never struck again when its day is folded again.
6. The status feed counts the same 14 baskets eviction does.
7. With the flag on, nothing evicted before the rule's first day is enforced.
"""
from datetime import date, timedelta
from types import SimpleNamespace

from hope.backtest import unchanged_models as U
from hope.backtest.container_runner import ERR_EXIT_PREFIX
from hope.scoring import chronic_failure as cf
from hope.scoring.chronic_failure import ChronicFailureParams, EvictionState

A = "repo/a@sha256:" + "a" * 64
C = "repo/a@sha256:" + "c" * 64
ON = {U.SWITCH_ENV: "2026-11-16"}


def _m(ref):
    return SimpleNamespace(image_digest=ref)


def _ledger(monkeypatch, days):
    from hope.backtest import shadow
    monkeypatch.setattr(shadow, "shadow_days", lambda root: sorted(days))
    monkeypatch.setattr(U, "_ran_on", lambda root, day: days.get(day, {}))


# ---- 1. catch-up runs ---------------------------------------------------------------

def test_the_run_day_of_a_basket_is_the_next_day():
    from scripts.run_shadow_day_bd import _run_day
    assert _run_day("2026-11-15") == "2026-11-16"
    assert _run_day("2026-12-31") == "2027-01-01"


def test_a_model_on_its_40th_run_is_not_removed_by_a_late_catch_up(tmp_path, monkeypatch):
    # first run 8 Oct (basket 7 Oct): run 40 is 16 Nov, removal from 17 Nov
    _ledger(monkeypatch, {"2026-10-07": {"hk": A}})
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON) == {}       # run day, even if run on 18 Nov
    assert U.apply(root, {"hk": _m(A)}, "2026-11-17", environ=ON) == {"hk": "unchanged"}


# ---- 2 + 3. a removed miner's new model ------------------------------------------------

def test_a_new_model_is_seated_and_charged_on_its_first_run(tmp_path, monkeypatch):
    days = {"2026-08-01": {"hk": A}}
    _ledger(monkeypatch, days)
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON) == {"hk": "unchanged"}
    assert U.apply(root, {"hk": _m(A)}, "2026-11-17", environ=ON) == {"hk": "removed_digest"}
    # the run of 18 Nov runs his new model C; basket 17 Nov is not folded yet
    assert U.apply(root, {"hk": _m(C)}, "2026-11-18", environ=ON) == {}
    assert U.removed_hotkeys(root, "2026-11-18") == frozenset()               # seated, charged
    # the days A sat out stay uncharged, before and after C is folded
    assert U.removed_hotkeys(root, "2026-11-16") == {"hk"}
    assert U.removed_hotkeys(root, "2026-11-17") == {"hk"}
    days["2026-11-17"] = {"hk": C}
    U.refresh(root)
    assert U.removed_hotkeys(root, "2026-11-17") == {"hk"}
    assert U.removed_hotkeys(root, "2026-11-18") == frozenset()


def test_switch_off_writes_no_day_list(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    U.apply(root, {"hk": _m(A)}, "2026-11-15", environ=ON)
    assert U.load(root)["by_day"] == {}


# ---- 4. a late-written basket ------------------------------------------------------------

def test_a_basket_written_after_a_later_one_still_counts(tmp_path, monkeypatch):
    days = {"2026-10-02": {"hk": A}}
    _ledger(monkeypatch, days)
    root = str(tmp_path)
    U.refresh(root)
    days["2026-10-01"] = {"hk": A}
    rec = U.refresh(root)
    assert rec["hotkeys"]["hk"]["since"] == "2026-10-01"
    assert rec["digests"][U.bare_digest(A)] == "2026-10-01"
    assert U.refresh(root) == rec                                              # folded once


def test_a_late_basket_never_switches_the_current_digest(tmp_path, monkeypatch):
    days = {"2026-10-05": {"hk": C}}
    _ledger(monkeypatch, days)
    root = str(tmp_path)
    U.refresh(root)
    days["2026-10-01"] = {"hk": A}
    rec = U.refresh(root)
    assert rec["hotkeys"]["hk"] == {"digest": U.bare_digest(C), "since": "2026-10-05"}
    assert rec["digests"][U.bare_digest(A)] == "2026-10-01"


# ---- 5. a cancelled charge on the last observed day -------------------------------------

RULE = date(2026, 11, 16)
P = ChronicFailureParams(min_survivors=1, effective_from=RULE)
CRASH = (False, f"{ERR_EXIT_PREFIX}1: Traceback (most recent call last)")
CLEAN = (True, None)


def test_a_cancelled_day_folded_again_is_not_struck_again():
    states, log = {}, []
    for i in range(5):
        dec = cf.observe_day(states, log, RULE + timedelta(days=i), {"hk": CRASH, "o": CLEAN}, P)
        states, log = dec.states, log + list(dec.events)
    last = RULE + timedelta(days=4)
    assert states["hk"].evicted_on == last
    cancelled = [(last, "hk")]
    undo = cf.apply_cancellations(states, log, cancelled, P)
    states, log = undo.states, log + list(undo.events)
    assert undo.retracted == ("hk",)
    # the next run folds its last observed day again, with the same crash record
    for _ in range(2):
        dec = cf.observe_day(states, log, last, {"hk": CRASH, "o": CLEAN}, P, cancelled=cancelled)
        states, log = dec.states, log + list(dec.events)
        assert dec.evicted == () and dec.struck == () and dec.events == ()
        assert states["hk"].evicted_on is None
    assert cf.strikes_in_window(log, "hk", last, 14) == 4


def test_a_cancellation_for_another_day_does_not_hide_a_strike():
    dec = cf.observe_day({}, [], RULE, {"hk": CRASH, "o": CLEAN}, P,
                         cancelled=[(RULE - timedelta(days=1), "hk")])
    assert dec.struck == ("hk",)


# ---- 6. the feed's window ------------------------------------------------------------------

def test_the_feed_counts_the_baskets_eviction_counts():
    from hope.publication.registration_status_feed import add_liveness
    log = [cf.StrikeEvent(day=RULE - timedelta(days=13 - i), hotkey="hk", kind=cf.KIND_STRIKE,
                          fault=cf.FAULT_MINER) for i in (0, 5, 9, 13)]
    basket = RULE                     # the run of 17 Nov has seen basket 16 Nov
    assert cf.strikes_in_window(log, "hk", basket, 14) == 4
    out = add_liveness([{"hotkey": "hk"}], log, {}, basket + timedelta(days=1), P)
    assert out[0]["strikes"] == 4


# ---- 7. enforcement and the rule's first day -------------------------------------------------

def test_flag_on_never_enforces_an_eviction_from_before_the_rule():
    from hope.validator.daily_stream_weights import enforced_evictions
    old = EvictionState(hotkey="old", evicted_on=date(2026, 8, 20), evictions_total=1,
                        last_eviction_on=date(2026, 8, 20))
    new = EvictionState(hotkey="new", evicted_on=RULE + timedelta(days=1), evictions_total=1,
                        last_eviction_on=RULE + timedelta(days=1))
    states = {"old": old, "new": new}
    day = RULE + timedelta(days=3)
    assert enforced_evictions(states, day, {cf.EFFECTIVE_FROM_ENV: "2026-11-16"}) == {"new"}
    assert enforced_evictions(states, day, {}) == {"old", "new"}          # no date: as before


# ---- 8. a clean exit that answered nothing --------------------------------------------------

def test_a_clean_exit_with_no_answers_is_a_strike_and_never_ends_an_eviction():
    runs = {"empty": (True, None), "some": (True, None), "crash": CRASH, "nocov": (True, None),
            "none_given": (True, None)}
    cov = {"empty": (120, 0), "some": (120, 3), "crash": (120, 0), "none_given": (0, 0)}
    out = cf.with_delivery(runs, cov)
    assert out["empty"][0] is False and out["empty"][1].startswith(cf.ERR_NOTHING_USABLE)
    assert cf.classify_failure(*out["empty"]) == cf.FAULT_MINER
    assert cf.failure_reason(*out["empty"]) == cf.REASON_NOTHING_USABLE
    assert out["some"] == (True, None) and out["nocov"] == (True, None)
    assert out["none_given"] == (True, None) and out["crash"] == CRASH
    # an evicted model past its 7 days does not return on such a run
    st = {"empty": EvictionState(hotkey="empty", evicted_on=RULE, evictions_total=1,
                                 last_eviction_on=RULE, reinstate_not_before=RULE + timedelta(days=7))}
    dec = cf.observe_day(st, [], RULE + timedelta(days=8), cf.with_delivery({"empty": (True, None), "o": CLEAN},
                                                                           {"empty": (120, 0)}), P)
    assert dec.reinstated == () and dec.states["empty"].evicted_on == RULE


# ---- 9. copy-group precedence belongs to the model, not the hotkey --------------------------

def test_an_older_hotkey_committing_a_newer_miners_model_does_not_take_its_seat(tmp_path, monkeypatch):
    from hope.scoring.duplication import prediction_collisions
    from hope.validator import daily_stream_weights as W
    X = "repo/x@sha256:" + "e" * 64
    days = {"2026-08-01": {"old": X}, "2026-11-01": {"author": A, "old": X},
            "2026-11-05": {"author": A, "old": A}}
    _ledger(monkeypatch, days)
    root = str(tmp_path)
    U.refresh(root)
    assert U.model_first_days(root) == {"author": "2026-11-01", "old": "2026-11-05"}
    receipts = {"old": "2026-08-02", "author": "2026-11-02"}       # first receipt day, any model
    today = {"author": "fp", "old": "fp"}
    off = W.model_seniority(root, receipts, {}, "2026-11-06")
    assert prediction_collisions(today, precedence=off)[0].original == "old"     # as before
    env = {W.MODEL_SENIORITY_FROM_ENV: "2026-11-06"}
    on = W.model_seniority(root, receipts, env, "2026-11-06")
    assert prediction_collisions(today, precedence=on)[0].original == "author"
    assert W.model_seniority(root, receipts, env, "2026-11-05") == receipts       # before its date
    # a hotkey with no record of its current model comes last
    assert W.model_seniority(root, {**receipts, "new": "2026-11-06"}, env, "2026-11-06")["new"][0] == "9999-12-31"


def test_an_old_record_gains_the_per_hotkey_runs_on_refresh(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-10-01": {"hk": A}})
    root = str(tmp_path)
    U.save(root, {"through": "2026-10-01", "hotkeys": {"hk": {"digest": U.bare_digest(A), "since": "2026-10-01"}},
                  "digests": {U.bare_digest(A): "2026-10-01"}, "removed": {"x": {"removed_on": "2026-11-16"}},
                  "by_day": {"2026-11-16": ["z"]}})
    rec = U.refresh(root)
    assert rec["runs"] == {"hk": {U.bare_digest(A): "2026-10-01"}}
    assert rec["removed"] == {"x": {"removed_on": "2026-11-16"}} and rec["by_day"] == {"2026-11-16": ["z"]}


# ---- 10. a later pass over a served run day never rewrites its lists --------------------------

def test_a_rerun_after_a_new_commit_keeps_the_removed_miner_exempt(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON) == {"hk": "unchanged"}
    # hk commits C; the pipeline comes back for the same run day (a kill, a retry, a supplement)
    U.apply(root, {"hk": _m(C)}, "2026-11-16", environ=ON)
    assert U.removed_hotkeys(root, "2026-11-16") == {"hk"}


def test_a_late_retry_of_an_older_basket_does_not_backdate_a_removal(tmp_path, monkeypatch):
    days = {"2026-08-01": {"hk": A}}
    _ledger(monkeypatch, days)
    root = str(tmp_path)
    U.apply(root, {"hk": _m(C)}, "2026-11-17", environ=ON)       # hk ran C on run 17
    U.apply(root, {"hk": _m(A)}, "2026-11-17", environ=ON)       # a late pass sees another model
    assert U.removed_hotkeys(root, "2026-11-17") == frozenset()


def test_a_removed_miner_whose_new_model_waits_for_admission_stays_exempt(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON)
    U.apply(root, {}, "2026-11-17", environ=ON)                   # not in the registry that run
    assert U.removed_hotkeys(root, "2026-11-17") == {"hk"}


def test_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON, save_it=False) == {"hk": "unchanged"}
    assert U.load(root) is None


# ---- 11. "nothing usable" counts from the rule's first day ------------------------------------

def test_nothing_usable_is_not_applied_before_the_rule_day(tmp_path, monkeypatch):
    import inspect

    from hope.validator import daily_loop
    src = inspect.getsource(daily_loop)
    assert "d >= params.effective_from" in src and "cf.with_delivery(runs" in src


def test_a_catch_up_of_an_earlier_run_day_runs_a_model_removed_later(tmp_path, monkeypatch):
    _ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-17", environ=ON) == {"hk": "unchanged"}
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON) == {"hk": "unchanged"}  # day 40+ already
    rec = U.load(root)
    rec["removed"][U.bare_digest(A)]["removed_on"] = "2026-11-18"
    U.save(root, rec)
    assert U.to_remove(U.load(root), {"hk": _m(A)}, "2026-11-17") == {"hk": "unchanged"}
    assert U.to_remove(U.empty() | {"removed": rec["removed"]}, {"hk": _m(A)}, "2026-11-17") == {}
