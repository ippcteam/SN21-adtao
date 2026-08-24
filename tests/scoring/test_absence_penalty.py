"""absence_penalty — absence must never beat participation, and there is no exit.

Each test states the exploit it kills. Fixtures use the real shadow-record
and standing-ledger formats, so these tests break if either format drifts.
"""

import json
import os
from datetime import date

from hope.scoring.absence_penalty import (
    DEFAULT_PENALTY_SCORE,
    absence_penalty_enabled,
    apply_penalties,
    compute_penalties,
    penalty_log,
)
from hope.scoring import standing_ledger
from hope.scoring.episode_average import episode_weighted_average

DAY = date(2026, 8, 24)


def _shadow_day(root, day, coverage, ran=True):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    if ran:
        open(os.path.join(d, "_run.json"), "w").write("{}")
    for hk, (eps, preds) in coverage.items():
        with open(os.path.join(d, f"{hk}.jsonl"), "w") as f:
            f.write(json.dumps({"episodes_in": eps,
                                "predictions_out": preds}) + "\n")


def _standing(root, hk, n, score, day):
    os.makedirs(standing_ledger.standing_dir(root), exist_ok=True)
    with open(os.path.join(standing_ledger.standing_dir(root),
                           f"{hk}.jsonl"), "a") as f:
        for _ in range(n):
            f.write(json.dumps({"score": score, "weight": 1.0,
                                "day": day}) + "\n")


def test_subnet_down_charges_nobody(tmp_path):
    root = str(tmp_path)
    _shadow_day(root, "2026-08-24", {"alice": (100, 100)}, ran=False)
    # no _run.json AND no .jsonl counted -> subnet_ran False? our helper
    # wrote alice's file, so remove it to model a truly absent day
    os.remove(os.path.join(root, "shadow", "2026-08-24", "alice.jsonl"))
    assert compute_penalties(root, DAY) == []


def test_full_coverage_untouched(tmp_path):
    root = str(tmp_path)
    _shadow_day(root, "2026-08-24", {"alice": (100, 100)})
    assert compute_penalties(root, DAY) == []


def test_no_threshold_to_duck_under(tmp_path):
    # 80% coverage passes the old participation bar — the SCORE penalty
    # still charges the 20 skipped episodes. Selective skipping never pays.
    root = str(tmp_path)
    _shadow_day(root, "2026-08-24", {"alice": (100, 80)})
    pen = compute_penalties(root, DAY)
    assert pen == [{"day": "2026-08-24", "hotkey": "alice", "missed": 20}]


def test_fully_absent_active_miner_owes_the_whole_day(tmp_path):
    root = str(tmp_path)
    _shadow_day(root, "2026-08-23", {"bob": (90, 90)})      # bob played yesterday
    _shadow_day(root, "2026-08-24", {"alice": (100, 100)})  # today: only alice
    pen = compute_penalties(root, DAY)
    assert pen == [{"day": "2026-08-24", "hotkey": "bob", "missed": 100}]


def test_no_exit_and_coast(tmp_path):
    # carol has NO recent shadow presence at all — but she is on the board
    # (in-window standing entries), so going dark still bleeds her. This is
    # the hole that let a miner freeze a high average by quitting.
    root = str(tmp_path)
    _standing(root, "carol", 50, 0.62, "2026-08-18")
    _shadow_day(root, "2026-08-24", {"alice": (100, 100)})
    pen = compute_penalties(root, DAY)
    assert pen == [{"day": "2026-08-24", "hotkey": "carol", "missed": 100}]


def test_apply_is_idempotent_and_logged(tmp_path):
    root = str(tmp_path)
    _shadow_day(root, "2026-08-24", {"alice": (100, 60)})
    r1 = apply_penalties(root, DAY)
    assert r1["penalty_entries"] == 40
    r2 = apply_penalties(root, DAY)
    assert r2["penalty_entries"] == 0
    log = penalty_log(root)
    assert len(log) == 1 and log[0]["missed"] == 40
    assert log[0]["score"] == DEFAULT_PENALTY_SCORE


def test_catchup_covers_trailing_days(tmp_path):
    root = str(tmp_path)
    _shadow_day(root, "2026-08-24", {"alice": (50, 0)})
    _shadow_day(root, "2026-08-25", {"alice": (60, 60)})
    _shadow_day(root, "2026-08-26", {"alice": (70, 70)})
    r = apply_penalties(root, date(2026, 8, 26))
    # only the 24th owed anything, and the sweep caught it
    assert r["penalty_entries"] == 50
    assert {p["day"] for p in penalty_log(root)} == {"2026-08-24"}


def test_absence_actually_drags_the_standing(tmp_path):
    # The point of the whole rule: a frozen 0.62 average must MOVE DOWN on
    # an uncovered day.
    root = str(tmp_path)
    _standing(root, "carol", 100, 0.62, "2026-08-18")
    _shadow_day(root, "2026-08-24", {"alice": (100, 100)})
    before = episode_weighted_average(
        standing_ledger.load_entries(root, as_of=DAY)["carol"], as_of=DAY)
    apply_penalties(root, DAY)
    after = episode_weighted_average(
        standing_ledger.load_entries(root, as_of=DAY)["carol"], as_of=DAY)
    assert after < before
    assert after < 0.55  # 100 old at 0.62 + 100 penalties at 0.30, decayed


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SN21_ABSENCE_PENALTY", raising=False)
    assert absence_penalty_enabled(os.environ) is False
    monkeypatch.setenv("SN21_ABSENCE_PENALTY", "1")
    assert absence_penalty_enabled(os.environ) is True


def test_never_charges_before_the_effective_date(tmp_path):
    # Rob announced 24 Aug. The catch-up sweep must not reach behind it,
    # no matter when the flag flips.
    root = str(tmp_path)
    _shadow_day(root, "2026-08-23", {"alice": (50, 0)})
    from hope.scoring.absence_penalty import compute_penalties as cp
    assert cp(root, date(2026, 8, 23)) == []
