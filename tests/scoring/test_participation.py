"""Option A's participation gate.

The two behaviours that matter most are the ones that would be invisible if
they were wrong: a model that runs but delivers nothing must NOT count as
submitting, and a day WE failed to ship must never be charged to a miner.
Both are pinned here against real incidents rather than imagined ones.
"""

import pytest

from hope.scoring.participation import (
    DECAY_ENV,
    MIN_COVERAGE_ENV,
    MISSED,
    PENDING_ROB_MIN_COVERAGE,
    SUBMITTED,
    SUBNET_DOWN,
    ZERO_AT_ENV,
    ParticipationParams,
    bridge_multiplier,
    day_verdict,
    params_from_env,
)

P = ParticipationParams()


# ---- what counts as submitting ----------------------------------------------

def test_a_running_model_that_delivers_nothing_has_NOT_submitted():
    """THE ONE THAT MATTERS. Verified 2026-08-03 with real containers: a model
    printing rubbish exits cleanly and is recorded ok=True with zero
    predictions. Gating on 'it ran' would pay a container that does nothing —
    exactly the free-riding Option A's gate exists to stop."""
    assert day_verdict(700, 0, subnet_ran=True, params=P) == MISSED


def test_full_and_half_coverage_submit_and_a_token_does_not():
    assert day_verdict(700, 700, True, P) == SUBMITTED
    assert day_verdict(700, 350, True, P) == SUBMITTED     # exactly the bar
    assert day_verdict(700, 349, True, P) == MISSED        # a hair under
    assert day_verdict(700, 1, True, P) == MISSED          # one prediction


# ---- our failures are never theirs ------------------------------------------

def test_a_day_the_subnet_did_not_run_is_excluded_not_missed():
    """2026-08-03: a worker died at 04:11, the changelog never synced, and no
    bundle was produced. A naive gate would have marked ~120 hotkeys absent for
    OUR failure — and five validators mirror our vector within the hour, so it
    would have propagated subnet-wide before anyone looked."""
    assert day_verdict(0, 0, subnet_ran=False, params=P) == SUBNET_DOWN
    # even a miner who somehow delivered is still on an excluded day
    assert day_verdict(700, 700, subnet_ran=False, params=P) == SUBNET_DOWN


def test_an_empty_bundle_is_excluded_not_missed():
    """A thin day with no episodes leaves nothing to predict, so failing to
    predict it is not a failure. Rob's own weekend rule already says thin days
    must not punish anybody."""
    assert day_verdict(0, 0, subnet_ran=True, params=P) == SUBNET_DOWN


def test_subnet_down_days_do_not_break_a_run_of_misses():
    """A day we did not ship tells us nothing about whether the miner would
    have shown up, so it must neither punish NOR launder a miss."""
    assert bridge_multiplier([MISSED, SUBNET_DOWN, MISSED], P) == \
        bridge_multiplier([MISSED, MISSED], P)


# ---- the decay ---------------------------------------------------------------

def test_a_participating_miner_keeps_full_weight():
    assert bridge_multiplier([SUBMITTED] * 5, P) == 1.0
    assert bridge_multiplier([], P) == 1.0          # nothing observed yet


def test_weight_decays_with_consecutive_misses_then_zeroes():
    assert bridge_multiplier([SUBMITTED, MISSED], P) == 0.5
    assert bridge_multiplier([SUBMITTED, MISSED, MISSED], P) == 0.25
    assert bridge_multiplier([MISSED, MISSED, MISSED], P) == 0.0


def test_recovery_restores_full_weight_immediately():
    """The bridge is a participation test, not a memory of past sins — history
    is what scoring is for. A miner who missed Monday and has submitted every
    day since is participating."""
    assert bridge_multiplier([MISSED, MISSED, SUBMITTED], P) == 1.0


# ---- Rob's numbers are configuration ----------------------------------------

def test_defaults_are_the_pending_proposals():
    p = params_from_env({})
    assert p.min_coverage == PENDING_ROB_MIN_COVERAGE == 0.50
    assert (p.decay_per_miss, p.misses_to_zero) == (0.5, 3)


def test_ratifying_robs_numbers_is_an_env_change_not_a_code_change():
    p = params_from_env({MIN_COVERAGE_ENV: "0.8", DECAY_ENV: "0.25",
                         ZERO_AT_ENV: "2"})
    assert (p.min_coverage, p.decay_per_miss, p.misses_to_zero) == (0.8, 0.25, 2)
    # and the override GOVERNS the arithmetic, not just the dataclass
    assert day_verdict(100, 70, True, p) == MISSED      # 70% < 80% bar
    assert day_verdict(100, 70, True, P) == SUBMITTED   # but clears the 50% one


@pytest.mark.parametrize("bad", ["", "   ", "abc", "5.0", "-0.2", "0"])
def test_a_malformed_or_out_of_range_coverage_keeps_the_proposal(bad):
    """A deploy typo must never silently change what miners are paid. Note 5.0
    is KEPT AT THE PROPOSAL rather than clamped to 1.0 — clamping would hide
    the typo behind plausible behaviour."""
    assert params_from_env({MIN_COVERAGE_ENV: bad}).min_coverage == \
        PENDING_ROB_MIN_COVERAGE
