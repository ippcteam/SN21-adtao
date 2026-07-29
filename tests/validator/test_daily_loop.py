"""The daily loop — the invoker the 2026-07-29 audit found missing.

Every step fail-soft, idempotent per day, injected fakes throughout.
"""

import json
import os
from datetime import date

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
    assert states["m1"].locked_alpha == 300.0  # capped at floor
    assert states["m1"].total_paid_alpha == 100.0


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
