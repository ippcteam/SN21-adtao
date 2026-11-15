"""A copy set's seat goes to a member that can be paid (MrCyRex, 13 Nov 2026).

orphaned_payees gives a set its senior back only when every member is a
suppressed copy, so a seat holder that cannot be paid (deregistered, a second
seat on its coldkey, under the alpha hold) still blocked everyone else: on
every published day 9-15 Nov at least one group's seat holder could not be
paid while another member could. From SN21_PAYABLE_SEAT_FROM the seat passes
to the most senior member that can be paid; before that the audit names who
would take it and nothing moves.
"""
from datetime import date

from hope.publication.allocation_audit import build_document
from hope.scoring.champion_promotion import PromotionState
from hope.scoring.duplication import CopyGroup
from hope.scoring.episode_average import ScoredEpisode
from hope.validator.daily_stream_weights import (
    cannot_be_paid,
    compute_daily_allocation,
    payable_seat_switch_on,
    payable_seats,
)

DAY = date(2026, 11, 20)
AUTHOR, COPY1, COPY2, OTHER, SIB = "5Author", "5Copy1", "5Copy2", "5Other", "5Sib"
SENIOR = {AUTHOR: "2026-10-01", COPY1: "2026-10-05", COPY2: "2026-10-09"}


def _entries(**standings):
    return {hk: [ScoredEpisode(score=s, scored_on=DAY) for _ in range(300)]
            for hk, s in standings.items()}


def _alloc(on, **kw):
    standings = kw.pop("standings", {AUTHOR: 0.80, COPY1: 0.80, COPY2: 0.80, OTHER: 0.70})
    return compute_daily_allocation(
        _entries(**standings), DAY, day_episode_volume=500,
        promotion_state=PromotionState(),
        lineage_groups=[CopyGroup(original=AUTHOR, copies=(COPY1, COPY2), kind="same_lineage",
                                  evidence="test")],
        one_payer_stats={"seniority": SENIOR},
        payable_seat_on=on, **kw)


CK = {AUTHOR: "ckA", COPY1: "ck1", COPY2: "ck2", OTHER: "ckO"}


def test_deregistered_seat_passes_to_the_most_senior_payable_member():
    ck = {k: v for k, v in CK.items() if k != AUTHOR}          # author left the metagraph
    on = _alloc(True, coldkey_of=ck)
    assert COPY1 in on.weights and COPY2 not in on.weights and AUTHOR not in on.weights
    passed = on.collapse_audit["payable_seat"]["passed"]
    assert passed == [{"to": COPY1, "instead_of": [{"hotkey": AUTHOR, "why": "deregistered"}],
                       "members": 3}]


def test_before_the_date_nothing_moves_and_the_audit_says_who_would():
    ck = {k: v for k, v in CK.items() if k != AUTHOR}
    off = _alloc(False, coldkey_of=ck)
    assert COPY1 not in off.weights and COPY2 not in off.weights
    assert off.collapse_audit["payable_seat"] == {
        "on": False, "passed": off.collapse_audit["payable_seat"]["passed"]}
    assert off.collapse_audit["payable_seat"]["passed"][0]["to"] == COPY1


def test_a_payable_seat_holder_keeps_the_seat():
    on = _alloc(True, coldkey_of=CK)
    assert AUTHOR in on.weights and COPY1 not in on.weights
    assert on.collapse_audit["payable_seat"]["passed"] == []


def test_under_the_alpha_hold_counts_when_enforced_only():
    alpha = {AUTHOR: 10.0, COPY1: 5000.0, COPY2: 5000.0, OTHER: 5000.0}
    on = _alloc(True, coldkey_of=CK, alpha_of=alpha, alpha_floor=1000.0, alpha_hold_enforced=True)
    assert COPY1 in on.weights and AUTHOR not in on.weights
    assert on.collapse_audit["payable_seat"]["passed"][0]["instead_of"][0]["why"] == "under the alpha hold"
    observing = _alloc(True, coldkey_of=CK, alpha_of=alpha, alpha_floor=1000.0, alpha_hold_enforced=False)
    assert AUTHOR in observing.weights and COPY1 not in observing.weights


def test_a_second_coldkey_seat_holder_passes_to_a_payable_member():
    # The author's coldkey keeps its better-standing sibling, so the author's
    # own seat is lost to the cap; the copy set still gets one earner.
    standings = {AUTHOR: 0.80, COPY1: 0.80, COPY2: 0.80, OTHER: 0.70, SIB: 0.90}
    on = _alloc(True, standings=standings, coldkey_of={**CK, SIB: "ckA"})
    assert SIB in on.weights and COPY1 in on.weights and AUTHOR not in on.weights
    assert on.collapse_audit["payable_seat"]["passed"][0]["instead_of"][0]["why"] == "second seat on a coldkey"


def test_nobody_payable_leaves_the_set_alone():
    passed = payable_seats([(AUTHOR, [COPY1])], {COPY1},
                           {AUTHOR: "deregistered", COPY1: "deregistered"}, SENIOR)
    assert passed == []


def test_order_is_the_precedence_in_force_then_lineage_then_hotkey():
    passed = payable_seats([(AUTHOR, [COPY2, COPY1])], {COPY1, COPY2},
                           {AUTHOR: "deregistered"}, seniority={COPY2: "2026-09-01", COPY1: "2026-10-01"})
    assert passed[0]["to"] == COPY2
    passed = payable_seats([(AUTHOR, [COPY2, COPY1])], {COPY1, COPY2},
                           {AUTHOR: "deregistered"}, seniority={}, preferred=[COPY2])
    assert passed[0]["to"] == COPY2


def test_stand_down_tests_count_as_passed():
    # Gating everyone would empty the curve, so the allocation stands the test
    # down; cannot_be_paid must not call them unpayable either.
    placements = {AUTHOR: 0.8, COPY1: 0.8}
    out = cannot_be_paid([AUTHOR, COPY1], placements, scored_days={}, tenure_min=7)
    assert out == {}
    out = cannot_be_paid([AUTHOR, COPY1], placements, alpha_of={AUTHOR: 1.0, COPY1: 1.0},
                         alpha_floor=1000.0, alpha_hold_enforced=True)
    assert out == {}
    # An alpha the chain did not report is kept, as the hold keeps it.
    out = cannot_be_paid([AUTHOR, COPY1], {**placements, OTHER: 0.7}, alpha_of={COPY1: 1.0, OTHER: 5000.0},
                         alpha_floor=1000.0, alpha_hold_enforced=True)
    assert out == {COPY1: "under the alpha hold"}


def test_switch_reads_its_date():
    assert not payable_seat_switch_on({}, DAY)
    assert not payable_seat_switch_on({"SN21_PAYABLE_SEAT_FROM": "nonsense"}, DAY)
    assert payable_seat_switch_on({"SN21_PAYABLE_SEAT_FROM": "2026-11-20"}, DAY)
    assert not payable_seat_switch_on({"SN21_PAYABLE_SEAT_FROM": "2026-11-21"}, DAY)


def test_published_audit_carries_both_fields():
    ck = {k: v for k, v in CK.items() if k != AUTHOR}
    doc = build_document(DAY, _alloc(False, coldkey_of=ck).collapse_audit)
    assert doc["payable_seat"]["on"] is False
    assert doc["payable_seat"]["passed"][0]["to"] == COPY1
    assert doc["payee_restored"] == []
