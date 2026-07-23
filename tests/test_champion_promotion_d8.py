"""D8 promotion rule — pins all three conditions, hysteresis, streak resets."""
from datetime import date, timedelta

from hope.scoring.champion_promotion import (
    PromotionParams,
    PromotionState,
    observe_day,
)

D0 = date(2026, 8, 1)
SCORED = {"champ": 30, "chall": 30, "newbie": 5}


def run_days(state, days_specs):
    """days_specs: list of standings dicts, applied on consecutive days."""
    events = []
    for i, standings in enumerate(days_specs):
        dec = observe_day(state, D0 + timedelta(days=i), standings, SCORED)
        state = dec.state
        if dec.event:
            events.append(dec.event)
    return state, events


def seated():
    return PromotionState(champion="champ", last_observed=D0 - timedelta(days=1))


class TestConditions:
    def test_tie_keeps_incumbent(self):
        st, ev = run_days(seated(), [{"champ": 0.80, "chall": 0.80}] * 10)
        assert st.champion == "champ" and not ev

    def test_lead_below_margin_never_promotes(self):
        st, ev = run_days(seated(), [{"champ": 0.80, "chall": 0.83}] * 10)  # 3.75% < 5%
        assert st.champion == "champ" and not ev

    def test_margin_held_7_days_promotes(self):
        st, ev = run_days(seated(), [{"champ": 0.80, "chall": 0.85}] * 7)  # 6.25% lead
        assert st.champion == "chall"
        assert ev and ev[-1]["type"] == "promotion" and ev[-1]["held_days"] == 7

    def test_six_days_not_enough(self):
        st, ev = run_days(seated(), [{"champ": 0.80, "chall": 0.85}] * 6)
        assert st.champion == "champ" and not ev

    def test_streak_broken_by_one_bad_day_restarts(self):
        days = [{"champ": 0.80, "chall": 0.85}] * 6 \
             + [{"champ": 0.80, "chall": 0.81}] \
             + [{"champ": 0.80, "chall": 0.85}] * 6
        st, ev = run_days(seated(), days)
        assert st.champion == "champ" and not ev  # 6+6, never 7 consecutive

    def test_min_scored_days_blocks_young_model(self):
        st, ev = run_days(seated(), [{"champ": 0.80, "newbie": 0.99}] * 10)
        assert st.champion == "champ" and not ev  # newbie has 5 < 14 scored days

    def test_streak_is_per_challenger(self):
        # two challengers alternate clearing the margin — neither accumulates 7
        a = {"champ": 0.80, "chall": 0.85, "other": 0.70}
        b = {"champ": 0.80, "chall": 0.70, "other": 0.85}
        st, ev = run_days(seated(), [a, b, a, b, a, b, a, b, a, b],)
        assert st.champion == "champ" and not ev


class TestColdStart:
    def test_initial_seat_requires_min_scored_days(self):
        st = PromotionState()
        dec = observe_day(st, D0, {"newbie": 0.9}, {"newbie": 5})
        assert dec.state.champion is None
        dec2 = observe_day(dec.state, D0 + timedelta(days=1), {"newbie": 0.9}, {"newbie": 14})
        assert dec2.state.champion == "newbie"
        assert dec2.event["type"] == "initial_seat"


class TestNonConsecutiveObservation:
    def test_gap_in_observations_breaks_hold(self):
        st = seated()
        standings = {"champ": 0.80, "chall": 0.85}
        for i in range(6):
            st = observe_day(st, D0 + timedelta(days=i), standings, SCORED).state
        # day 6 skipped; day 7 observed — streak must restart, no promotion
        dec = observe_day(st, D0 + timedelta(days=7), standings, SCORED)
        assert dec.promoted is False
        assert dec.state.champion == "champ"
        assert dec.state.lead_started == D0 + timedelta(days=7)
