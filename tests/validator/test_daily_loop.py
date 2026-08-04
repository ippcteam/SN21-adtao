"""The daily loop — the invoker the 2026-07-29 audit found missing.

Every step fail-soft, idempotent per day, injected fakes throughout.
"""

import json
import os
from datetime import date, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.scoring.collateral_floor import CaptureState
from hope.scoring.settle_day_flow import SettledHorizon
from hope.validator.daily_loop import (
    load_capture_states,
    read_day_volume,
    run_daily_loop,
    save_capture_states,
)

DAY = date(2026, 8, 11)
KEY = Ed25519PrivateKey.generate()


def _pred(p50=0.4, spread=0.3):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
            for m in ("cost_delta_pct", "conversions_delta_pct",
                      "efficiency_delta_pct")}


def _shadow(root, day="2026-07-27", hotkey="alpha", eps=("ep1",)):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{hotkey}.jsonl"), "a") as f:
        f.write(json.dumps({
            "day": day, "hotkey": hotkey,
            "predictions": {e: {"7": _pred()} for e in eps},
        }) + "\n")


def _outcomes(day):
    return [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day)]


def test_full_loop_all_steps_report(tmp_path):
    root = str(tmp_path)
    _shadow(root)
    summary = run_daily_loop(
        root, root, DAY,
        outcomes_provider=_outcomes,
        earnings_provider=lambda d: {"alpha": 100.0},
        key_loader=lambda: KEY,
        day_volume_provider=lambda d: 237,
        environ={"SN21_DAILY_STREAM_WEIGHTS": "on"},
    )
    assert summary["settle"]["entries_written"] == 1
    assert summary["capture"]["escrowed_alpha"] == 100.0  # below floor: all escrows
    assert summary["advisory"]["floors_total"] == 1
    assert summary["publish"]["published"] is True
    assert summary["weights"]["day_volume"] == 237
    # artifacts on disk
    assert read_day_volume(root, DAY) == 237
    assert os.path.exists(os.path.join(root, "collateral", f"compliance_{DAY}.json"))
    assert os.path.exists(os.path.join(root, f"intended_weights_{DAY}.json"))


def test_loop_is_safe_to_run_twice(tmp_path):
    root = str(tmp_path)
    _shadow(root)
    kwargs = dict(outcomes_provider=_outcomes, key_loader=lambda: KEY,
                  day_volume_provider=lambda d: 100, environ={})
    first = run_daily_loop(root, root, DAY, **kwargs)
    second = run_daily_loop(root, root, DAY, **kwargs)
    assert first["settle"]["entries_written"] == 1
    assert second["settle"]["entries_written"] == 0  # per-pair markers
    assert second["publish"]["skipped"] == "already published for this day"


def test_one_broken_step_never_silences_the_others(tmp_path):
    root = str(tmp_path)

    def exploding_outcomes(day):
        raise RuntimeError("db down")

    summary = run_daily_loop(
        root, root, DAY,
        outcomes_provider=exploding_outcomes,
        key_loader=lambda: KEY,
        environ={},
    )
    assert "error" in summary["settle"]
    assert "error" not in summary["capture"]
    assert summary["publish"]["skipped_reason"] == "pre_maturity"


def test_capture_states_persist_across_days(tmp_path):
    root = str(tmp_path)
    run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                   earnings_provider=lambda d: {"m1": 200.0}, environ={})
    run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                   earnings_provider=lambda d: {"m1": 200.0}, environ={})
    states = load_capture_states(root)
    # Capped at the floor IN FORCE ON DAY (2026-08-11). Under Rob's dated
    # schedule that is 150 a — the 10 Aug rung. It was 300 under the
    # superseded weekly ramp, which opened at 300 on day one.
    assert states["m1"].locked_alpha == 150.0
    # 400 earned across two runs, 150 escrowed to the floor, so 250 paid out
    # (was 100 when the floor was 300). The lower opening rung means miners
    # start being PAID sooner, which is the point of Rob starting at zero.
    assert states["m1"].total_paid_alpha == 250.0


def test_anchor_gated_off_by_default(tmp_path):
    root = str(tmp_path)
    _shadow(root)
    committed = []
    summary = run_daily_loop(
        root, root, DAY,
        outcomes_provider=_outcomes,
        key_loader=lambda: KEY,
        chain_committer=lambda h: committed.append(h) or "ok",
        environ={},  # SN21_ANCHOR_COMMITS unset
    )
    assert committed == []
    assert summary["publish"]["anchor"] == "off (SN21_ANCHOR_COMMITS unset)"


def test_anchor_commits_when_flag_on(tmp_path):
    root = str(tmp_path)
    _shadow(root)
    committed = []
    summary = run_daily_loop(
        root, root, DAY,
        outcomes_provider=_outcomes,
        key_loader=lambda: KEY,
        chain_committer=lambda h: committed.append(h) or "in-block",
        environ={"SN21_ANCHOR_COMMITS": "true"},
    )
    assert len(committed) == 1
    assert len(committed[0]) == 32  # sha256 bytes
    assert summary["publish"]["anchor"].startswith("committed")


def test_read_day_volume_rejects_stale_day(tmp_path):
    root = str(tmp_path)
    run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                   day_volume_provider=lambda d: 300, environ={})
    assert read_day_volume(root, DAY) == 300
    # the [D3] gate must fail closed on a stale file, not reuse yesterday
    assert read_day_volume(root, DAY.replace(day=DAY.day + 1)) is None


def test_capture_state_round_trip(tmp_path):
    root = str(tmp_path)
    save_capture_states(root, {"m": CaptureState("m", locked_alpha=42.0)})
    assert load_capture_states(root)["m"].locked_alpha == 42.0


# ---- step 1c: liveness (chronic failure) ------------------------------------

def _shadow_run(root, day, hotkey, ok, error=None):
    from hope.backtest.container_runner import RunResult
    from hope.backtest.shadow import ShadowModel, record_day, record_run_marker
    record_day(root, str(day), ShadowModel(hotkey, "img@sha256:a", str(day)),
               RunResult(ok=ok, error=error))
    record_run_marker(root, str(day), episodes=10, models=1)


def test_liveness_skips_a_day_the_subnet_never_ran(tmp_path):
    """The 2026-07-30 case: no shadow day means WE did not run. No strike,
    no observation, no state written — for anybody."""
    root = str(tmp_path)
    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["strikes"] == 0
    assert "skipped" in summary["liveness"]
    from hope.scoring import standing_ledger as sl
    assert sl.load_strike_events(root) == []
    assert sl.load_eviction_states(root) == {}


# ---- step 1c: the day basis (the blocking 2026-08-01 finding) ---------------
#
# The shadow ledger is keyed by BASKET day and shadow_daily.sh runs
# BD-<yesterday>, so the loop's own day is never a shadow day. The old code
# asked subnet_ran(shadow_root, str(day)) and therefore skipped every real
# day forever. These tests fold the ledger's days, not the loop's.

def test_liveness_observes_yesterdays_basket_not_the_loop_day(tmp_path):
    """PRODUCTION SHAPE: shadow_daily.sh wrote BD-<DAY-1>; the loop runs on
    DAY. The strike must land. Against the pre-fix code this returned
    {'skipped': 'subnet did not run this day', 'strikes': 0} and wrote
    nothing to the ledger."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    basket_day = DAY - timedelta(days=1)
    _shadow_run(root, basket_day, "alpha", ok=False,
                error=f"{ERR_EXIT_PREFIX}1: b'boom'")

    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["shadow_days_observed"] == [str(basket_day)]
    assert summary["liveness"]["struck"] == ["alpha"]
    assert summary["liveness"]["strikes"] == 1

    events = sl.load_strike_events(root)
    assert [(str(e.day), e.hotkey, e.kind) for e in events] == \
           [(str(basket_day), "alpha", "strike")]


def test_liveness_reaches_the_whole_live_ledger_shape_including_the_gap(tmp_path):
    """The live ledger on 2026-08-01: 07-27/28/29/31 present, 07-30 absent
    (manual script, missed a day). One loop run on 08-01 must observe the
    four days that exist, and never the one that does not."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    loop_day = date(2026, 8, 1)
    for d, ok in (("2026-07-27", True), ("2026-07-28", False),
                  ("2026-07-29", True), ("2026-07-31", False)):
        _shadow_run(root, d, "reference-v1", ok=ok,
                    error=None if ok else f"{ERR_EXIT_PREFIX}1: b''")

    summary = run_daily_loop(root, root, loop_day,
                             outcomes_provider=lambda d: [], environ={})
    assert summary["liveness"]["shadow_days_observed"] == [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-31"]
    assert summary["liveness"]["strikes"] == 2
    assert [str(e.day) for e in sl.load_strike_events(root)] == [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-31"]
    # 07-30 never appears: absence is OUR non-run, never a miner failure
    assert "2026-07-30" not in {str(e.day) for e in sl.load_strike_events(root)}


def test_liveness_does_not_refold_old_days_on_the_next_run(tmp_path):
    """Catch-up must not turn into a rewind: re-observing an ancient CLEAN
    day against today's state would reinstate a currently evicted miner."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    start = date(2026, 8, 1)
    # A healthy BYSTANDER throughout. Rob ruled 2026-08-03 that the field
    # must never be emptied, so a LONE model can never be evicted — the
    # eviction is withheld and recorded instead. An eviction test therefore
    # needs somebody left standing.
    _shadow_run(root, start, "alpha", ok=True)            # a clean day first
    _shadow_run(root, start, "bystander", ok=True)
    for i in range(1, 6):                                  # then five failures
        _shadow_run(root, start + timedelta(days=i), "alpha", ok=False,
                    error=f"{ERR_EXIT_PREFIX}1: b''")
        _shadow_run(root, start + timedelta(days=i), "bystander", ok=True)

    first = run_daily_loop(root, root, start + timedelta(days=6),
                           outcomes_provider=lambda d: [], environ={})
    assert first["liveness"]["evicted"] == ["alpha"]
    assert sl.load_eviction_states(root)["alpha"].evicted_on == \
        start + timedelta(days=5)

    # next day: nothing new in the ledger -> only the last observed day is
    # eligible for a re-fold, and it is still a strike, so nothing changes.
    second = run_daily_loop(root, root, start + timedelta(days=7),
                            outcomes_provider=lambda d: [], environ={})
    assert second["liveness"]["shadow_days_observed"] == \
        [str(start + timedelta(days=5))]
    assert second["liveness"]["events_written"] == 0
    assert second["liveness"]["reinstated"] == []
    assert sl.load_eviction_states(root)["alpha"].evicted_on == \
        start + timedelta(days=5)


def test_liveness_lookback_bounds_the_cold_start_fold(tmp_path):
    """A cold ledger reaches back `liveness_lookback_days` and no further —
    a shadow day older than that is not silently re-litigated."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX

    root = str(tmp_path)
    old = DAY - timedelta(days=30)
    recent = DAY - timedelta(days=2)
    for d in (old, recent):
        _shadow_run(root, d, "alpha", ok=False, error=f"{ERR_EXIT_PREFIX}1: b''")

    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             liveness_lookback_days=14, environ={})
    assert summary["liveness"]["shadow_days_observed"] == [str(recent)]


def test_liveness_ignores_a_shadow_day_after_the_loop_day(tmp_path):
    """Never let a future-dated basket evict anybody."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX

    root = str(tmp_path)
    _shadow_run(root, DAY + timedelta(days=1), "alpha", ok=False,
                error=f"{ERR_EXIT_PREFIX}1: b''")
    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["strikes"] == 0
    assert "skipped" in summary["liveness"]


def test_liveness_policy_numbers_come_from_the_environment(tmp_path):
    """[finding 5] N/M/K are overridable at deploy time, so ratifying Rob's
    numbers is a setting and not a code edit. Two failed days evict only
    because SN21_CHRONIC_STRIKES=2."""
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    for i in (2, 1):
        _shadow_run(root, DAY - timedelta(days=i), "alpha", ok=False,
                    error=f"{ERR_EXIT_PREFIX}1: b''")
        # see above: without a survivor Rob's floor withholds the eviction
        _shadow_run(root, DAY - timedelta(days=i), "bystander", ok=True)

    default_run = run_daily_loop(root, root, DAY,
                                 outcomes_provider=lambda d: [], environ={})
    assert default_run["liveness"]["evicted"] == []   # 2 strikes, policy is 5

    root2 = str(tmp_path / "override")
    for i in (2, 1):
        _shadow_run(root2, DAY - timedelta(days=i), "alpha", ok=False,
                    error=f"{ERR_EXIT_PREFIX}1: b''")
        # see above: without a survivor Rob's floor withholds the eviction
        _shadow_run(root2, DAY - timedelta(days=i), "bystander", ok=True)
    over = run_daily_loop(root2, root2, DAY, outcomes_provider=lambda d: [],
                          environ={"SN21_CHRONIC_STRIKES": "2"})
    assert over["liveness"]["evicted"] == ["alpha"]
    ev = [e for e in sl.load_strike_events(root2) if e.kind == "evicted"][0]
    assert ev.detail["strikes_to_evict"] == 2


def test_a_shadow_record_with_no_ok_field_is_not_a_failed_run(tmp_path):
    """[finding 6] The repo's own prediction-only record shape carries no
    `ok`. bool(None) made it a failed run and the loop recorded an
    `excluded` observation against the miner for it."""
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    basket_day = DAY - timedelta(days=1)
    _shadow(root, day=str(basket_day))          # writes {day,hotkey,predictions}

    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["excluded"] == []
    assert summary["liveness"]["events_written"] == 0
    assert sl.load_strike_events(root) == []


def test_liveness_records_a_strike_from_a_real_execution_failure(tmp_path):
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    _shadow_run(root, DAY, "alpha", ok=False,
                error=f"{ERR_EXIT_PREFIX}1: b'boom'")
    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["struck"] == ["alpha"]
    assert summary["liveness"]["evicted"] == []      # 1 strike, not 5
    assert summary["liveness"]["enforced"] is False  # flag off by default
    events = sl.load_strike_events(root)
    assert [e.kind for e in events] == ["strike"]
    assert events[0].hotkey == "alpha" and events[0].fault == "miner"


def test_liveness_does_not_double_count_a_rerun_of_the_loop(tmp_path):
    from hope.backtest.container_runner import ERR_EXIT_PREFIX
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    _shadow_run(root, DAY, "alpha", ok=False, error=f"{ERR_EXIT_PREFIX}1: b''")
    kwargs = dict(outcomes_provider=lambda d: [], environ={})
    run_daily_loop(root, root, DAY, **kwargs)
    second = run_daily_loop(root, root, DAY, **kwargs)
    assert second["liveness"]["events_written"] == 0
    assert len(sl.load_strike_events(root)) == 1


def test_liveness_never_strikes_a_subnet_side_failure(tmp_path):
    from hope.backtest.container_runner import ERR_DOCKER_UNAVAILABLE
    from hope.scoring import standing_ledger as sl

    root = str(tmp_path)
    _shadow_run(root, DAY, "alpha", ok=False, error=ERR_DOCKER_UNAVAILABLE)
    summary = run_daily_loop(root, root, DAY, outcomes_provider=lambda d: [],
                             environ={})
    assert summary["liveness"]["struck"] == []
    assert summary["liveness"]["excluded"] == ["alpha"]
    assert [e.kind for e in sl.load_strike_events(root)] == ["excluded"]


def test_liveness_failure_never_silences_the_other_steps(tmp_path, monkeypatch):
    root = str(tmp_path)
    _shadow(root, day=str(DAY))
    monkeypatch.setattr("hope.backtest.shadow.day_run_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    summary = run_daily_loop(root, root, DAY, outcomes_provider=_outcomes,
                             key_loader=lambda: KEY, environ={})
    assert "error" in summary["liveness"]
    assert summary["publish"]["published"] is True


def test_vertical_series_step_tags_and_stores(tmp_path, monkeypatch):
    """J3 wiring: settled entries get Jayesh's vertical tag + real components."""
    from datetime import date as _date
    from hope.validator.daily_loop import run_daily_loop
    from hope.scoring.vertical_error_series import load_entries
    from hope.scoring.settle_day_flow import SettledHorizon
    from hope.backtest.shadow import ShadowModel, record_day
    from hope.backtest.container_runner import RunResult

    shadow_root = str(tmp_path / "shadow")
    ledger_root = str(tmp_path / "ledger")
    day = _date(2026, 8, 11)

    preds = {"ep1": {"7": {"cost_delta_pct": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
                           "conversions_delta_pct": {"p10": 0.0, "p50": 0.1, "p90": 0.2},
                           "efficiency_delta_pct": {"p10": -0.1, "p50": 0.0, "p90": 0.1}}}}
    record_day(shadow_root, "2026-07-27",
               ShadowModel("minerA", "img@sha256:x", "2026-07-27"),
               RunResult(ok=True, predictions_out=1, error=None,
                         predictions=preds))

    outcomes = [SettledHorizon(episode_id="ep1", horizon_days=7,
                               cost_delta_pct=0.25, conversions_delta_pct=0.1,
                               efficiency_delta_pct=0.0,
                               finalized_on=day)]
    summary = run_daily_loop(
        shadow_root, ledger_root, day,
        outcomes_provider=lambda d: outcomes,
        vertical_map_provider=lambda ids: {"ep1": "ecommerce"},
    )
    vs = summary["vertical_series"]
    assert vs["entries_appended"] == 1 and vs["untagged"] == 0
    stored = load_entries(ledger_root)[0]
    assert stored["vertical"] == "ecommerce"
    # real components, not the blended-score fallback
    assert stored["pinball_component"] != stored["score"] or \
           stored["direction_component"] != stored["score"]
