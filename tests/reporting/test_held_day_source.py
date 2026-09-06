"""A held day's report pairs the live vector with the audit that produced it."""
from hope.reporting.held_day import live_allocation

A, B, C = "5A" + "a" * 46, "5B" + "b" * 46, "5C" + "c" * 46


def _intent(weights, audit):
    return {"weights": weights, "collapse_audit": audit}


class TestLiveAllocation:
    def test_a_paying_day_uses_its_own_vector_and_audit(self):
        own = _intent({A: 0.7, B: 0.3}, {"suppressed": [C]})
        live = live_allocation("2026-09-06", own, [("2026-09-05", _intent({C: 1.0}, {"suppressed": [A]}))])
        assert live.earning_set == {A, B} and live.collapse_audit == {"suppressed": [C]}
        assert live.held is False and live.source_day == "2026-09-06"

    def test_a_held_day_takes_vector_and_audit_from_the_same_earlier_day(self):
        today = _intent({}, {"suppressed": [A], "coldkey_cap": {"dropped": [B]}})   # today's controls
        yesterday = _intent({A: 0.6, B: 0.4}, {"suppressed": [C]})
        live = live_allocation("2026-09-06", today, [("2026-09-05", yesterday)])
        assert live.held is True and live.source_day == "2026-09-05"
        assert live.earning_set == {A, B}
        assert live.collapse_audit == {"suppressed": [C]}          # NOT today's, which excludes A and B
        assert live.notes == ("day held — reporting the live vector from 2026-09-05 (2 earning)",)

    def test_skips_earlier_days_that_paid_nobody(self):
        live = live_allocation("2026-09-06", _intent({}, {}), [
            ("2026-09-05", _intent({}, {"x": 1})), ("2026-09-04", _intent({C: 1.0}, {"y": 2}))])
        assert live.earning_set == {C} and live.collapse_audit == {"y": 2} and live.source_day == "2026-09-04"

    def test_never_borrows_from_the_future_or_today(self):
        live = live_allocation("2026-09-06", _intent({}, {"own": 1}), [
            ("2026-09-07", _intent({A: 1.0}, {})), ("2026-09-06", _intent({B: 1.0}, {}))])
        assert live.earning_set == frozenset() and live.held is False and live.collapse_audit == {"own": 1}
