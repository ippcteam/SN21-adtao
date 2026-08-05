"""Winner accuracy series — the roll-up behind the week-over-week chart.

The properties worth pinning are the ones that would let the chart lie:
a point that was interpolated rather than measured, a week attributed to
the wrong model, an entry counted in the wrong week, or a mean that
quietly includes somebody else's scores.
"""

from datetime import date

from hope.publication.winner_series import (
    HORIZONS,
    build_series,
    entries_by_week,
    iso_week,
    roll_up,
    week_bounds,
    winner_for_week,
)

W1 = "2026-W34"          # Mon 17 Aug – Sun 23 Aug 2026
W2 = "2026-W35"


def _entry(miner, horizon, score, finalized_on, episode="e1"):
    return {
        "episode_id": episode,
        "horizon_days": horizon,
        "miner": miner,
        "prediction": {"cost_delta_pct": {"p50": 0.0}},
        "components": {"quantile": 0.8, "direction": 1.0,
                       "coverage": 0.7, "goal": 0.6},
        "score": score,
        "finalized_on": finalized_on,
    }


def _receipt(*entries):
    return {"feed": "daily_receipt", "entries": list(entries)}


# ---- week arithmetic --------------------------------------------------------

def test_iso_week_and_bounds_round_trip():
    assert iso_week(date(2026, 8, 18)) == W1
    start, end = week_bounds(W1)
    assert start == date(2026, 8, 17) and end == date(2026, 8, 23)
    assert iso_week(start) == iso_week(end) == W1


def test_week_boundary_is_the_settle_date_not_the_receipt():
    """An entry belongs to the week its outcome FINALISED. A receipt whose
    publication slipped past Sunday must not drag its entries forward."""
    late_receipt = _receipt(
        _entry("hkA", 7, 0.8, "2026-08-23"),   # Sunday, week 34
        _entry("hkA", 7, 0.4, "2026-08-24"),   # Monday, week 35
    )
    grouped = entries_by_week([late_receipt])
    assert [e["score"] for e in grouped[W1]] == [0.8]
    assert [e["score"] for e in grouped[W2]] == [0.4]


def test_an_entry_with_no_settle_date_is_dropped_not_guessed():
    grouped = entries_by_week([_receipt(_entry("hkA", 7, 0.9, None))])
    assert grouped == {}


# ---- who the winner is ------------------------------------------------------

def test_winner_is_the_top_standing():
    assert winner_for_week({"hkA": 0.71, "hkB": 0.69}) == "hkA"


def test_ties_break_on_evidence_then_deterministically():
    tied = {"hkA": 0.70, "hkB": 0.70}
    # more scored entries wins the tie
    assert winner_for_week(tied, {"hkA": 10, "hkB": 400}) == "hkB"
    # with no evidence either way the answer is still stable, not dict order
    assert winner_for_week(tied) == winner_for_week(dict(reversed(tied.items())))


def test_no_standing_means_no_winner():
    assert winner_for_week({}) is None


# ---- the roll-up ------------------------------------------------------------

def test_point_is_the_winners_mean_and_counts_only_their_entries():
    receipts = [_receipt(
        _entry("hkA", 7, 0.80, "2026-08-18"),
        _entry("hkA", 7, 0.60, "2026-08-19"),
        _entry("hkB", 7, 0.10, "2026-08-19"),      # not the winner
    )]
    (point,) = roll_up(receipts, {W1: {"hkA": 0.7, "hkB": 0.2}})
    assert point.week == W1 and point.horizon_days == 7
    assert point.winner == "hkA"
    assert point.mean_score == 0.7            # (0.80 + 0.60) / 2, not 0.5
    assert point.entries == 2


def test_a_horizon_with_no_settled_entries_produces_no_point():
    """The 28-day line cannot exist before its first settlement. The roll-up
    must leave a gap rather than invent a point at zero."""
    receipts = [_receipt(_entry("hkA", 7, 0.8, "2026-08-18"))]
    points = roll_up(receipts, {W1: {"hkA": 0.7}})
    assert [p.horizon_days for p in points] == [7]
    assert all(p.horizon_days != 28 for p in points)


def test_a_week_with_no_standing_yields_nothing_rather_than_zero():
    """'Nobody had placed yet' and 'the winner scored zero' are different
    claims. A chart that renders the first as the second is wrong."""
    receipts = [_receipt(_entry("hkA", 7, 0.8, "2026-08-18"))]
    assert roll_up(receipts, {}) == []


def test_the_winner_can_change_between_weeks():
    receipts = [_receipt(
        _entry("hkA", 7, 0.90, "2026-08-18"),
        _entry("hkB", 7, 0.20, "2026-08-18"),
        _entry("hkA", 7, 0.30, "2026-08-25"),
        _entry("hkB", 7, 0.85, "2026-08-25"),
    )]
    points = roll_up(receipts, {W1: {"hkA": 0.9, "hkB": 0.2},
                                W2: {"hkA": 0.3, "hkB": 0.85}})
    by_week = {p.week: p for p in points}
    assert by_week[W1].winner == "hkA" and by_week[W1].mean_score == 0.9
    assert by_week[W2].winner == "hkB" and by_week[W2].mean_score == 0.85


def test_every_horizon_rolls_up_independently():
    receipts = [_receipt(
        _entry("hkA", 7, 0.80, "2026-08-18"),
        _entry("hkA", 14, 0.60, "2026-08-19"),
        _entry("hkA", 28, 0.40, "2026-08-20"),
    )]
    points = roll_up(receipts, {W1: {"hkA": 0.7}})
    assert {p.horizon_days: p.mean_score for p in points} == {7: 0.8, 14: 0.6, 28: 0.4}
    assert set(HORIZONS) == {7, 14, 28}


def test_roll_up_is_deterministic_and_ordered():
    receipts = [_receipt(
        _entry("hkA", 14, 0.5, "2026-08-25"),
        _entry("hkA", 7, 0.8, "2026-08-18"),
        _entry("hkA", 7, 0.6, "2026-08-25"),
    )]
    standings = {W1: {"hkA": 0.7}, W2: {"hkA": 0.7}}
    first = [p.as_dict() for p in roll_up(receipts, standings)]
    second = [p.as_dict() for p in roll_up(receipts, standings)]
    assert first == second
    assert [(p["week"], p["horizon_days"]) for p in first] == [
        (W1, 7), (W2, 7), (W2, 14),
    ]


def test_a_null_score_is_not_counted_as_a_zero():
    receipts = [_receipt(
        _entry("hkA", 7, 0.8, "2026-08-18"),
        _entry("hkA", 7, None, "2026-08-19"),
    )]
    (point,) = roll_up(receipts, {W1: {"hkA": 0.7}})
    assert point.mean_score == 0.8 and point.entries == 1


# ---- the document -----------------------------------------------------------

def test_series_document_reports_its_own_coverage():
    receipts = [_receipt(
        _entry("hkA", 7, 0.8, "2026-08-18"),
        _entry("hkA", 7, 0.6, "2026-08-25"),
    )]
    doc = build_series(receipts, {W1: {"hkA": 0.7}, W2: {"hkA": 0.7}})
    assert doc["feed"] == "winner_accuracy_series"
    assert doc["era"] == "daily"
    assert doc["weeks_covered"] == 2
    assert doc["first_week"] == W1 and doc["last_week"] == W2
    assert doc["points"][0]["week_start"] == "2026-08-17"


def test_empty_input_is_an_empty_series_not_a_crash():
    doc = build_series([], {})
    assert doc["points"] == [] and doc["weeks_covered"] == 0
    assert doc["first_week"] is None and doc["last_week"] is None


def test_point_carries_the_n_behind_its_mean():
    """A mean over one entry is not a trend. The count travels with the point
    so the chart can say so rather than drawing it like any other."""
    receipts = [_receipt(_entry("hkA", 7, 0.8, "2026-08-18"))]
    doc = build_series(receipts, {W1: {"hkA": 0.7}})
    assert doc["points"][0]["entries"] == 1
