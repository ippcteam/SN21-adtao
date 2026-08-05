"""W1 wiring tests — E1+D13+D7+D8 composed behind the daily-stream flag.

Covers the standing ledger (persistence for the daily path), the pure
allocation core (D3 gate semantics, curve application, promotion
separation), and the ledger-backed convenience entrypoint.
"""

from datetime import date, timedelta

import pytest

from hope.scoring import standing_ledger as sl
from hope.scoring.champion_promotion import PromotionState
from hope.scoring.daily_score_flow import (
    HorizonResult,
    WeightedEntry,
    day_flow,
)
from hope.scoring.episode_average import ScoredEpisode
from hope.scoring.weight_curve import CurveParams
from hope.validator.daily_stream_weights import (
    allocation_from_ledger,
    compute_daily_allocation,
    d3_min_daily_episodes,
    daily_stream_enabled,
)

DAY = date(2026, 7, 27)


def _entries_for(miner, n_days, score=0.8, per_day=30, end=DAY):
    """n_days of history, `per_day` entries each — enough for placement
    eligibility (>= 250 predictions) when n_days*per_day >= 250."""
    out = []
    for i in range(n_days):
        d = end - timedelta(days=n_days - 1 - i)
        out.extend(
            ScoredEpisode(score=score, scored_on=d, weight=1.0)
            for _ in range(per_day)
        )
    return out


# ---- flag/env helpers ------------------------------------------------------

def test_flag_and_gate_env_parsing():
    assert daily_stream_enabled({"SN21_DAILY_STREAM_WEIGHTS": "on"})
    assert not daily_stream_enabled({})
    assert d3_min_daily_episodes({"SN21_D3_MIN_DAILY_EPISODES": "150"}) == 150
    assert d3_min_daily_episodes({"SN21_D3_MIN_DAILY_EPISODES": "junk"}) == 0
    assert d3_min_daily_episodes({}) == 0


# ---- ledger ----------------------------------------------------------------

def test_ledger_round_trip(tmp_path):
    root = str(tmp_path)
    results = [
        HorizonResult("ep1", 7, "minerA", 0.9, DAY),
        HorizonResult("ep1", 14, "minerA", 0.7, DAY),
        HorizonResult("ep2", 7, "minerB", 0.5, DAY),
    ]
    entries = day_flow(results, DAY)
    written = sl.append_entries(root, entries)
    assert written == 3

    loaded = sl.load_entries(root, as_of=DAY)
    assert set(loaded) == {"minerA", "minerB"}
    assert len(loaded["minerA"]) == 2
    # blend weights survive the round trip
    assert loaded["minerA"][0].weight == pytest.approx(
        entries[0].weight
    )


def test_ledger_window_cut_and_compact(tmp_path):
    root = str(tmp_path)
    old_day = DAY - timedelta(days=400)
    sl.append_entries(root, [
        WeightedEntry(miner="m", score=0.9, weight=1.0, entered_on=old_day),
        WeightedEntry(miner="m", score=0.8, weight=1.0, entered_on=DAY),
    ])
    loaded = sl.load_entries(root, as_of=DAY)
    assert len(loaded["m"]) == 1  # old entry outside window not loaded

    shed = sl.compact(root, as_of=DAY)
    assert shed == 1
    # still loads identically after compaction
    assert len(sl.load_entries(root, as_of=DAY)["m"]) == 1


def test_ledger_rejects_path_traversal_hotkey(tmp_path):
    with pytest.raises(ValueError):
        sl.append_entries(str(tmp_path), [
            WeightedEntry(miner="../evil", score=0.5, weight=1.0, entered_on=DAY),
        ])


def test_promotion_state_round_trip(tmp_path):
    root = str(tmp_path)
    assert sl.load_promotion_state(root) == PromotionState()
    state = PromotionState(champion="A", challenger="B",
                           lead_started=DAY, last_observed=DAY)
    sl.save_promotion_state(root, state)
    assert sl.load_promotion_state(root) == state


# ---- pure allocation core --------------------------------------------------

def test_allocation_applies_curve_to_eligible_standings():
    entries = {
        "alpha": _entries_for("alpha", 10, score=0.9),   # 300 preds — eligible
        "beta": _entries_for("beta", 10, score=0.6),     # eligible, lower
        "coldstart": _entries_for("coldstart", 2, score=0.99),  # 60 preds — not eligible
    }
    alloc = compute_daily_allocation(
        entries, DAY, day_episode_volume=300,
        promotion_state=PromotionState(),
    )
    assert not alloc.gated
    assert set(alloc.standings) == {"alpha", "beta"}  # cold-start excluded
    assert alloc.weights["alpha"] > alloc.weights["beta"] > 0
    assert alloc.weights.get("coldstart", 0.0) == 0.0
    # curve ratios preserved (top/second renormalized over 2 earners;
    # governance ruling 2026-07-28 shares 50/25)
    assert alloc.weights["alpha"] / alloc.weights["beta"] == pytest.approx(
        0.50 / 0.25
    )
    assert alloc.earning_set_size == 2


def test_d3_gate_holds_weights_but_still_observes_promotion():
    entries = {"alpha": _entries_for("alpha", 14, score=0.9)}
    alloc = compute_daily_allocation(
        entries, DAY, day_episode_volume=40,
        promotion_state=PromotionState(),
        min_daily_episodes=150,
    )
    assert alloc.gated
    assert alloc.weights == {}          # weight update withheld
    assert alloc.standings              # standings still computed
    # promotion still observed: alpha has 14 scored days -> seated
    assert alloc.promotion.promoted
    assert alloc.promotion.state.champion == "alpha"


def test_gate_disabled_when_minimum_is_zero():
    entries = {"alpha": _entries_for("alpha", 10)}
    alloc = compute_daily_allocation(
        entries, DAY, day_episode_volume=0,
        promotion_state=PromotionState(),
        min_daily_episodes=0,
    )
    assert not alloc.gated
    assert alloc.weights["alpha"] == 1.0


def test_promotion_is_separate_from_weights():
    """[D8]: a challenger can lead the curve without taking the champion
    seat — promotion needs margin + hold, weights follow standings now."""
    state = PromotionState(champion="incumbent")
    entries = {
        "incumbent": _entries_for("incumbent", 14, score=0.80),
        "challenger": _entries_for("challenger", 14, score=0.82),  # < 5% lead
    }
    alloc = compute_daily_allocation(
        entries, DAY, day_episode_volume=300, promotion_state=state,
    )
    # curve pays the better standing immediately…
    assert alloc.weights["challenger"] > alloc.weights["incumbent"]
    # …but the seat does not move (margin not cleared)
    assert not alloc.promotion.promoted
    assert alloc.promotion.state.champion == "incumbent"


def test_curve_ceiling_bounds_earning_set():
    entries = {
        f"m{i:02d}": _entries_for(f"m{i:02d}", 10, score=0.5 + i * 0.01)
        for i in range(30)
    }
    alloc = compute_daily_allocation(
        entries, DAY, day_episode_volume=300,
        promotion_state=PromotionState(),
        curve_params=CurveParams(max_earners=20),
    )
    assert alloc.earning_set_size == 20  # hard ceiling ([D7])


# ---- ledger-backed entrypoint ----------------------------------------------

def test_allocation_from_ledger_end_to_end(tmp_path):
    root = str(tmp_path)
    # 14 days x 25 episodes of finalised horizon results for two miners —
    # 350 units of evidence mass (blend weights sum to 1.0 per episode),
    # clearing the 250-prediction placement floor
    for i in range(14):
        d = DAY - timedelta(days=13 - i)
        results = []
        for ep in range(25):
            results.append(HorizonResult(f"e{i}-{ep}", 7, "alpha", 0.9, d))
            results.append(HorizonResult(f"e{i}-{ep}", 14, "alpha", 0.9, d))
            results.append(HorizonResult(f"e{i}-{ep}", 28, "alpha", 0.9, d))
            results.append(HorizonResult(f"e{i}-{ep}", 7, "beta", 0.4, d))
            results.append(HorizonResult(f"e{i}-{ep}", 14, "beta", 0.4, d))
            results.append(HorizonResult(f"e{i}-{ep}", 28, "beta", 0.4, d))
        sl.append_entries(root, day_flow(results, d))

    alloc = allocation_from_ledger(root, DAY, day_episode_volume=300,
                                   min_daily_episodes=0)
    assert not alloc.gated
    assert alloc.weights["alpha"] > alloc.weights["beta"]
    # initial champion seated (14 scored days) and state persisted
    assert alloc.promotion.promoted
    assert sl.load_promotion_state(root).champion == "alpha"

    # next day: state reloads, no re-seat event
    alloc2 = allocation_from_ledger(root, DAY + timedelta(days=1),
                                    day_episode_volume=300,
                                    min_daily_episodes=0)
    assert alloc2.promotion.state.champion == "alpha"
    assert not alloc2.promotion.promoted


# ---- runner pair-building (allowlist-aware) ---------------------------------

def test_pairs_for_uids_respects_allowlist():
    from hope.validator.daily_stream_weights import pairs_for_uids
    weights = {"a": 0.5, "b": 0.3, "c": 0.2, "ghost": 0.1}
    uid_map = {"a": 1, "b": 2, "c": 3}
    # no restriction
    assert pairs_for_uids(weights, uid_map) == [(1, 0.5), (2, 0.3), (3, 0.2)]
    # allowlist restricts (audit 2026-07-29: was silently bypassed)
    assert pairs_for_uids(weights, uid_map, allowed_uids={1, 3}) == [
        (1, 0.5), (3, 0.2)]
    # empty allowlist = nothing passes (restriction, not None)
    assert pairs_for_uids(weights, uid_map, allowed_uids=set()) == []


def test_pairs_for_uids_drops_zero_and_unmapped():
    from hope.validator.daily_stream_weights import pairs_for_uids
    assert pairs_for_uids({"a": 0.0, "b": None, "x": 0.4}, {"a": 1, "b": 2}) == []
