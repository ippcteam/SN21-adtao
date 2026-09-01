"""A miner who simply placed outside the paid set must be told so.

WHY THIS EXISTS
    Every unfunded row carried a reason because some control had acted on it
    — coldkey cap, one-payer, lineage, tenure. A miner that scored, was
    suppressed by nothing, and placed below the paid set is acted on by no
    control, so its row published blank: no tier, no policies, no
    explanation. On BD-2026-09-01 that was 2 of 120 rows.

    Blank reads as an oversight. It is in fact the ordinary outcome of a
    fixed-size paid set, and saying so is the difference between a miner who
    can check the rule and a miner who files a complaint.

    What is pinned here is mostly what must NOT happen: the reason must not
    reach a miner that never scored, must not reach a funded miner, must not
    overwrite a real control's note, and must not appear on a day when
    nobody was paid at all.
"""

from __future__ import annotations

import pytest

from hope.reporting.aggregator import _explain_below_the_cut
from hope.reporting.payload import MinerResult, PolicyOutcome


def _row(uid, *, tier=None, status="scored", policies=(), score=0.5):
    return MinerResult(
        uid=uid,
        hotkey="5" + f"{uid:047d}",
        score=score,
        status=status,
        tier=tier,
        met_baseline=True,
        policies=list(policies),
    )


EARNING = {"5" + f"{1:047d}"}


def _reasons(rows):
    return {r.uid: [p.control for p in r.policies] for r in rows}


class TestTheRowThatNeededIt:
    def test_a_scored_unfunded_unsuppressed_row_gets_the_reason(self):
        out = _explain_below_the_cut([_row(2)], EARNING)
        assert _reasons(out) == {2: ["earning_cut"]}

    def test_the_wording_says_the_score_stands(self):
        """The miner's next question is 'have I lost something' — answer it
        in the same sentence."""
        out = _explain_below_the_cut([_row(2)], EARNING)
        detail = out[0].policies[0].detail
        assert "Ranked below the earning cut" in detail
        assert "score stands" in detail
        assert "not a penalty" in detail

    def test_it_carries_no_counterparty(self):
        """No other hotkey holds this miner's seat — there is no seat."""
        out = _explain_below_the_cut([_row(2)], EARNING)
        assert out[0].policies[0].counterparty is None


class TestWhoMustNotGetIt:
    def test_not_a_funded_miner(self):
        out = _explain_below_the_cut([_row(1, tier="elite")], EARNING)
        assert _reasons(out) == {1: []}

    def test_not_a_miner_that_never_scored(self):
        """It did not lose a ranking contest it never entered. Saying so
        would paper over the real reason with a plausible one."""
        out = _explain_below_the_cut([_row(3, status="disqualified_not_in_epoch")], EARNING)
        assert _reasons(out) == {3: []}

    def test_it_never_overwrites_a_real_control(self):
        suppressed = _row(4, policies=[PolicyOutcome(
            control="one_payer", detail="Already earning under an earlier "
                                        "submission.", counterparty="5xyz")])
        out = _explain_below_the_cut([suppressed], EARNING)
        assert _reasons(out) == {4: ["one_payer"]}
        assert out[0].policies[0].counterparty == "5xyz"

    @pytest.mark.parametrize("empty", [None, set()])
    def test_not_on_a_day_when_nobody_was_paid(self, empty):
        """A held or gated day pays nobody from a new vector. Telling a
        hundred miners they ranked below a cut that was never applied
        contradicts the day's own commentary."""
        out = _explain_below_the_cut([_row(2), _row(3)], empty)
        assert _reasons(out) == {2: [], 3: []}


class TestTheWholeField:
    def test_a_realistic_day_leaves_nobody_unexplained(self):
        rows = [
            _row(1, tier="elite"),                       # funded
            _row(2, policies=[PolicyOutcome(control="coldkey_cap",
                                            detail="seat held")]),
            _row(3, policies=[PolicyOutcome(control="tenure",
                                            detail="too few days")]),
            _row(4),                                     # the blank one
            _row(5, status="disqualified_not_in_epoch"),                  # never scored
        ]
        out = _explain_below_the_cut(rows, EARNING)
        unexplained = [r.uid for r in out
                       if r.tier is None and not r.policies
                       and r.status == "scored"]
        assert unexplained == [], (
            "a scored, unsuppressed, unfunded row still has no reason")

    def test_row_count_and_identity_are_untouched(self):
        """This adds a reason; it must not add, drop, or reorder miners."""
        rows = [_row(1, tier="elite"), _row(2), _row(3, status="disqualified_not_in_epoch")]
        out = _explain_below_the_cut(rows, EARNING)
        assert [r.uid for r in out] == [1, 2, 3]
        assert [r.score for r in out] == [r.score for r in rows]
        assert [r.tier for r in out] == [r.tier for r in rows]

    def test_the_control_name_is_accepted_by_the_contract(self):
        """PolicyOutcome.control is a closed Literal — an unlisted value
        raises at validation and would fail the publish, not the row."""
        assert PolicyOutcome(control="earning_cut", detail="x").control == "earning_cut"
