"""The submission deadline must be RESTART-STABLE and Monday-anchored.

Regression guard for the bug where serve.py computed the deadline as
`now + PREDICTION_DEADLINE_HOURS`, so every API restart slid the close forward
(a Jun-12 restart pushed W24's Mon-05:00-UTC close to the following Friday).
"""
from datetime import datetime, timedelta, timezone

from hope.constants import MINING_CLOSE_HOUR_UTC
from hope.validator.epoch_manager import next_mining_close


def _at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_lands_on_monday_0500_utc():
    # Sat 2026-06-13 09:16 → next close is Mon 2026-06-15 05:00 UTC
    close = next_mining_close(_at(2026, 6, 13, 9, 16))
    assert close == _at(2026, 6, 15, 5, 0)
    assert close.weekday() == 0  # Monday
    assert close.hour == MINING_CLOSE_HOUR_UTC


def test_restart_stable_across_the_week():
    # Every restart between the open and the close must yield the SAME close —
    # this is the property the old now+156h computation violated.
    closes = {
        next_mining_close(_at(2026, 6, 9, 18, 0)),   # Tue
        next_mining_close(_at(2026, 6, 11, 3, 30)),  # Thu
        next_mining_close(_at(2026, 6, 13, 9, 16)),  # Sat
        next_mining_close(_at(2026, 6, 15, 4, 59)),  # Mon, 1 min before close
    }
    assert closes == {_at(2026, 6, 15, 5, 0)}


def test_old_rolling_computation_would_have_drifted():
    # Demonstrates the bug: now+156h gives a different answer per restart time.
    a = _at(2026, 6, 12, 15, 28) + timedelta(hours=156)
    b = _at(2026, 6, 13, 9, 16) + timedelta(hours=156)
    assert a != b                       # drifts with restart time
    assert a.weekday() != 0             # Jun 19 is a Friday, not Monday


def test_past_this_weeks_close_targets_next_monday():
    # Mon 06:00 (just after the 05:00 close) → next week's Monday, not today.
    close = next_mining_close(_at(2026, 6, 15, 6, 0))
    assert close == _at(2026, 6, 22, 5, 0)
