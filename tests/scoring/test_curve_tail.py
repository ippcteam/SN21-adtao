"""The weight-curve tail is a published, switchable parameter.

Governance decision of 9 Sept 2026 (SN21_CURVE_TAIL_REVIEW): the tail moves
from 0.5 to 0.8 so that a 20-miner field is paid on chain, not just listed.
The mechanism follows the standing method: announced first, applied from a
published date forward, the published value until then, and the value in
force recorded in the audit so any day's vector can be recomputed.

What must never happen: a deploy of this code changing the curve by itself,
a typo in an environment variable paying an accidental curve, or the switch
landing on a different day from the one miners were told.
"""

from datetime import date

import pytest

from hope.scoring.standing_method import (
    CURVE_TAIL_DECAY_ENV,
    CURVE_TAIL_EFFECTIVE_FROM_ENV,
    PUBLISHED_TAIL_DECAY,
    curve_params_in_force,
    curve_tail_configured,
    curve_tail_decay,
    method_params,
)
from hope.scoring.weight_curve import CurveParams, curve_weights

ANNOUNCED = date(2026, 9, 15)


class TestNothingChangesUntilSet:
    def test_unset_is_the_published_tail(self):
        assert curve_tail_decay({}) == PUBLISHED_TAIL_DECAY == 0.5

    def test_the_default_params_are_the_published_curve(self):
        assert curve_params_in_force({}, date(2026, 9, 9)) == CurveParams(score_threshold=0.0)

    @pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-0.5", "1.5", "nan"])
    def test_an_unusable_value_falls_back_to_the_published_tail(self, raw):
        env = {CURVE_TAIL_DECAY_ENV: raw}
        assert curve_tail_configured(env) is None
        assert curve_tail_decay(env, date(2026, 9, 20)) == PUBLISHED_TAIL_DECAY

    def test_one_point_zero_is_allowed_and_means_a_flat_tail(self):
        assert curve_tail_configured({CURVE_TAIL_DECAY_ENV: "1.0"}) == 1.0


class TestTheSwitchLandsOnTheAnnouncedDay:
    ENV = {CURVE_TAIL_DECAY_ENV: "0.80",
           CURVE_TAIL_EFFECTIVE_FROM_ENV: ANNOUNCED.isoformat()}

    def test_the_day_before_pays_the_published_tail(self):
        assert curve_tail_decay(self.ENV, date(2026, 9, 14)) == 0.5

    def test_the_day_itself_and_after_pay_the_new_tail(self):
        assert curve_tail_decay(self.ENV, ANNOUNCED) == 0.8
        assert curve_tail_decay(self.ENV, date(2026, 10, 1)) == 0.8

    def test_no_date_means_immediately(self):
        assert curve_tail_decay({CURVE_TAIL_DECAY_ENV: "0.8"}, date(2026, 1, 1)) == 0.8

    def test_a_bad_date_does_not_hold_the_switch_back_silently(self):
        """A date that does not parse is treated as no date — the operator's
        intent to switch stands — rather than as 'never', which would look
        like a working switch that quietly never fires."""
        env = {CURVE_TAIL_DECAY_ENV: "0.8", CURVE_TAIL_EFFECTIVE_FROM_ENV: "soon"}
        assert curve_tail_decay(env, date(2026, 9, 9)) == 0.8

    def test_params_in_force_carry_the_tail_and_keep_everything_else(self):
        p = curve_params_in_force(self.ENV, ANNOUNCED)
        assert p.tail_decay == 0.8
        assert (p.top_share, p.second_share, p.third_share, p.max_earners) == (0.50, 0.25, 0.10, 20)


class TestTheAuditSaysWhichCurvePaid:
    def test_method_params_publish_the_tail_in_force_and_the_plan(self):
        env = {CURVE_TAIL_DECAY_ENV: "0.8",
               CURVE_TAIL_EFFECTIVE_FROM_ENV: ANNOUNCED.isoformat()}
        before = method_params(env, date(2026, 9, 14))
        assert before["curve_tail_decay"] == 0.5
        assert before["curve_tail_configured"] == 0.8
        assert before["curve_tail_effective_from"] == "2026-09-15"
        after = method_params(env, ANNOUNCED)
        assert after["curve_tail_decay"] == 0.8

    def test_unset_publishes_the_published_tail_and_no_plan(self):
        p = method_params({}, date(2026, 9, 9))
        assert p["curve_tail_decay"] == 0.5
        assert p["curve_tail_configured"] is None
        assert p["curve_tail_effective_from"] is None


class TestTheAllocationUsesIt:
    def test_the_allocation_assembles_the_curve_from_the_environment(self):
        import inspect

        from hope.validator import daily_stream_weights as m

        src = inspect.getsource(m.allocation_from_ledger)
        assert "curve_params = curve_params_in_force(environ, day)" in src
        assert "CurveParams(score_threshold=curve_score_threshold" not in src


# ---- the review's numbers, pinned ------------------------------------------

def _twenty():
    return {f"m{i:02d}": 1.0 - i * 0.01 for i in range(20)}


def _ranked(w):
    return [v for _, v in sorted(w.items(), key=lambda kv: -kv[1])]


class TestTheReviewsTwentyMinerTable:
    def test_live_half_tail_matches_the_review(self):
        r = _ranked(curve_weights(_twenty(), CurveParams(tail_decay=0.5)))
        assert r[0] == pytest.approx(0.5263, abs=5e-4)
        assert sum(r[:3]) == pytest.approx(0.895, abs=1e-3)
        assert sum(r[10:20]) == pytest.approx(0.0008, abs=1e-4)

    def test_point_eight_matches_the_review(self):
        r = _ranked(curve_weights(_twenty(), CurveParams(tail_decay=0.8)))
        assert r[0] == pytest.approx(0.4029, abs=5e-4)
        assert r[1] == pytest.approx(0.2015, abs=5e-4)
        assert r[2] == pytest.approx(0.0806, abs=5e-4)
        assert sum(r[:3]) == pytest.approx(0.685, abs=1e-3)
        assert sum(r[3:10]) == pytest.approx(0.255, abs=1e-3)
        assert sum(r[10:20]) == pytest.approx(0.060, abs=1e-3)
        assert r[19] == pytest.approx(0.0018, abs=1e-4)

    def test_every_listed_earner_is_paid_on_chain_at_point_eight(self):
        """Chain weights are 16-bit: a share that rounds to zero at 65535 is
        an unpaid seat. Under 0.5 ranks 17-20 round to zero; under 0.8 none do."""
        half = _ranked(curve_weights(_twenty(), CurveParams(tail_decay=0.5)))
        eight = _ranked(curve_weights(_twenty(), CurveParams(tail_decay=0.8)))
        assert sum(1 for v in half if round(v * 65535) == 0) == 4
        assert all(round(v * 65535) > 0 for v in eight)

    def test_the_podium_ratios_do_not_move(self):
        for tail in (0.5, 0.8):
            r = _ranked(curve_weights(_twenty(), CurveParams(tail_decay=tail)))
            assert r[0] / r[1] == pytest.approx(2.0)
            assert r[1] / r[2] == pytest.approx(2.5)

    def test_a_thin_field_pays_the_same_under_both_tails(self):
        three = {"a": 0.9, "b": 0.8, "c": 0.7}
        assert curve_weights(three, CurveParams(tail_decay=0.5)) == \
            curve_weights(three, CurveParams(tail_decay=0.8))
