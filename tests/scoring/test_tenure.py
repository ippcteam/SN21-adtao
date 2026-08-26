"""Earning-set tenure gate — the curve pays a track record, not a lucky day."""

from datetime import date

from hope.scoring.episode_average import ScoredEpisode
from hope.scoring.tenure import (
    DEFAULT_MIN_DAYS,
    scored_days_by_hotkey,
    short_tenure_hotkeys,
    tenure_gate_enabled,
    tenure_min_days,
)
from hope.validator.daily_stream_weights import compute_daily_allocation
from hope.scoring.champion_promotion import PromotionState


def _eps(day_scores):
    """[(day, score), ...] -> ScoredEpisode list."""
    return [ScoredEpisode(score=s, scored_on=d, weight=1.0)
            for d, s in day_scores]


def _days(hotkey_days):
    """{hk: n_days} -> entries with 300 entries spread over n distinct days
    (the placement floor needs >= 250 predictions; tenure counts DAYS)."""
    out = {}
    for hk, n in hotkey_days.items():
        eps = []
        for i in range(300):
            eps.append((date(2026, 8, (i % max(n, 1)) + 1), 0.6))
        out[hk] = _eps(eps)
    return out


# ------------------------------------------------------------- flags ----

def test_gate_is_off_by_default():
    assert tenure_gate_enabled({}) is False
    assert tenure_gate_enabled({"SN21_TENURE_GATE": "1"}) is True


def test_min_days_default_and_malformed_values_keep_default():
    assert tenure_min_days({}) == DEFAULT_MIN_DAYS
    assert tenure_min_days({"SN21_TENURE_MIN_DAYS": "banana"}) == DEFAULT_MIN_DAYS
    assert tenure_min_days({"SN21_TENURE_MIN_DAYS": "0"}) == DEFAULT_MIN_DAYS
    assert tenure_min_days({"SN21_TENURE_MIN_DAYS": "10"}) == 10


# ------------------------------------------------------- pure counting ----

def test_distinct_days_counted_not_entries():
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    counts = scored_days_by_hotkey([
        (str(d1), ["A", "A", "B"]),   # duplicates within a day collapse
        (str(d2), ["A"]),
    ])
    assert counts == {"A": 2, "B": 1}


def test_exactly_min_days_passes_and_one_less_is_gated():
    scored = {"vet": 7, "junior": 6, "newborn": 0}
    short = short_tenure_hotkeys(scored, ["vet", "junior", "newborn"], 7)
    assert short == frozenset({"junior", "newborn"})


def test_unknown_candidate_counts_as_zero_days():
    assert short_tenure_hotkeys({}, ["ghost"], 7) == frozenset({"ghost"})


def test_exempt_hotkey_is_never_gated():
    short = short_tenure_hotkeys({"house": 1}, ["house"], 7,
                                 exempt_hotkeys=frozenset({"house"}))
    assert short == frozenset()


# -------------------------------------------------- allocation-level ----

def test_one_day_wonder_is_unpaid_and_veteran_earns():
    day = date(2026, 8, 26)
    entries = _days({"wonder": 1, "veteran": 10})
    entries["wonder"] = _eps([(date(2026, 8, 25), 0.99)] * 300)  # one great day
    alloc = compute_daily_allocation(
        entries, day, 100, PromotionState(), tenure_min=7)
    assert alloc.weights.get("wonder", 0.0) == 0.0
    assert alloc.weights.get("veteran", 0.0) > 0.0
    assert "wonder" in alloc.collapse_audit["tenure_gated"]["hotkeys"]


def test_standings_are_untouched_by_the_gate():
    day = date(2026, 8, 26)
    entries = _days({"wonder": 1, "veteran": 10})
    alloc = compute_daily_allocation(
        entries, day, 100, PromotionState(), tenure_min=7)
    assert "wonder" in alloc.standings   # still on the leaderboard


def test_gate_stands_down_rather_than_empty_the_curve():
    day = date(2026, 8, 26)
    entries = _days({"a": 2, "b": 3})       # nobody has 7 days
    alloc = compute_daily_allocation(
        entries, day, 100, PromotionState(), tenure_min=7)
    assert alloc.collapse_audit["tenure_gated"]["stood_down"] is True
    assert sum(alloc.weights.values()) > 0  # everyone still paid


def test_zero_min_days_means_gate_absent():
    day = date(2026, 8, 26)
    entries = _days({"wonder": 1, "veteran": 10})
    alloc = compute_daily_allocation(
        entries, day, 100, PromotionState(), tenure_min=0)
    assert "tenure_gated" not in alloc.collapse_audit
    assert alloc.weights.get("wonder", 0.0) > 0.0
