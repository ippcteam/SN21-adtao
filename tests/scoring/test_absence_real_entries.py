"""Absence charges stop once nothing real is left in the window (switched off
until announced; decision card for Sir Rob, 16 Oct 2026).

Penalty zeros are entries in the window too, so a hotkey that stopped running
never "aged out" and was charged for ever. With the rule, only a hotkey holding
at least one real scored entry is "on the board"; a playing hotkey is still
charged as before."""
import json
import os
from datetime import date

from hope.scoring import standing_ledger
from hope.scoring.absence_penalty import chargeable_hotkeys, compute_penalties, is_penalty_entry, needs_real_entries
from tests.scoring.test_absence_penalty import _shadow_day

DAY = date(2026, 10, 20)
ON = {"SN21_ABSENCE_NEEDS_REAL_ENTRIES_FROM": "2026-10-20"}


def _entries(root, hk, rows):
    os.makedirs(standing_ledger.standing_dir(root), exist_ok=True)
    with open(os.path.join(standing_ledger.standing_dir(root), f"{hk}.jsonl"), "a") as f:
        for score, weight, day in rows:
            f.write(json.dumps({"score": score, "weight": weight, "day": day}) + "\n")


def _ledger(root):
    _shadow_day(root, "2026-10-20", {"live": (100, 100)})                 # the subnet ran
    _entries(root, "gone", [(0.0, 1.0, "2026-10-1%d" % i) for i in range(10)])           # zeros only
    _entries(root, "fading", [(0.55, 0.35, "2026-10-01")] + [(0.0, 1.0, "2026-10-15")])  # one real left
    _entries(root, "player", [(0.0, 1.0, "2026-10-18")])                   # zeros only, but playing:
    _shadow_day(root, "2026-10-19", {"live": (100, 100), "player": (100, 0)})


def test_off_by_default_the_old_reading_charges_everyone_with_entries(tmp_path):
    root = str(tmp_path); _ledger(root)
    assert {"gone", "fading", "player"} <= chargeable_hotkeys(root, DAY, environ={})


def test_with_the_rule_only_real_entries_or_playing_count(tmp_path):
    root = str(tmp_path); _ledger(root)
    got = chargeable_hotkeys(root, DAY, environ=ON)
    assert "gone" not in got
    assert "fading" in got and "player" in got


def test_a_day_before_the_date_keeps_the_old_reading(tmp_path):
    root = str(tmp_path); _ledger(root)
    assert "gone" in chargeable_hotkeys(root, date(2026, 10, 19), environ=ON)
    assert needs_real_entries(DAY, {"SN21_ABSENCE_NEEDS_REAL_ENTRIES_FROM": "soon"}) is False


def test_the_charge_itself_follows(tmp_path):
    root = str(tmp_path); _ledger(root)
    charged = {p["hotkey"] for p in compute_penalties(root, DAY, environ=ON)}
    assert "gone" not in charged and {"fading", "player"} <= charged
    assert "gone" in {p["hotkey"] for p in compute_penalties(root, DAY, environ={})}


def test_penalty_signature():
    from types import SimpleNamespace as N
    assert is_penalty_entry(N(score=0.0, weight=1.0), 0.0)
    assert not is_penalty_entry(N(score=0.0, weight=0.35), 0.0)       # a real zero score
    assert not is_penalty_entry(N(score=0.6, weight=1.0), 0.0)
