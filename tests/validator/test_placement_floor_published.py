"""A hotkey under the placement floor is published with the evidence it has.

WHY
    A miner scored in the receipts and absent from every public row is a
    reported bug the day someone notices — and they did (7 Sept 2026, uid
    75: 292 settled entries, 48 of the 50 prediction-mass then needed, no
    row anywhere). The floor is a documented rule; a rule that hides the
    people it applies to is indistinguishable from a gap. So the allocation
    names them, with the number the gate used.
"""

from datetime import date

from hope.scoring.champion_promotion import PromotionState
from hope.scoring.episode_average import PLACEMENT_FLOOR_PREDICTIONS, ScoredEpisode
from hope.validator.daily_stream_weights import compute_daily_allocation

DAY = date(2026, 9, 7)


def _entries(spec):
    """spec: hotkey -> (score, prediction mass). Weight 1.0 per entry."""
    return {hk: [ScoredEpisode(score=s, scored_on=DAY) for _ in range(int(n))]
            for hk, (s, n) in spec.items()}


def _alloc(spec, **kw):
    return compute_daily_allocation(
        _entries(spec), DAY, day_episode_volume=500,
        promotion_state=PromotionState(), **kw)


class TestBelowFloorIsPublished:
    def test_the_unplaced_hotkey_is_named_with_its_evidence(self):
        floor = PLACEMENT_FLOOR_PREDICTIONS
        alloc = _alloc({"a": (0.8, floor + 50), "b": (0.7, floor + 50),
                        "young": (0.9, floor - 10)})
        placement = alloc.collapse_audit["placement"]
        assert placement["floor"] == floor
        assert placement["below"] == {
            "young": {"scored_predictions": float(floor - 10)}}
        # No standing, no seat: the floor still decides placement.
        assert "young" not in alloc.standings
        assert "young" not in alloc.weights

    def test_placed_hotkeys_are_not_listed_as_below(self):
        floor = PLACEMENT_FLOOR_PREDICTIONS
        alloc = _alloc({"a": (0.8, floor + 50), "b": (0.7, floor)})
        assert alloc.collapse_audit["placement"]["below"] == {}
        assert set(alloc.standings) == {"a", "b"}

    def test_the_control_states_its_bar_every_day(self):
        floor = PLACEMENT_FLOOR_PREDICTIONS
        pol = _alloc({"a": (0.8, floor + 50), "b": (0.7, floor + 50)}).collapse_audit["policies"]
        assert pol["placement_floor"] == {"floor": floor, "below": 0}
        pol = _alloc({"a": (0.8, floor + 50), "b": (0.7, floor + 50),
                      "y1": (0.5, 3), "y2": (0.5, 4)}).collapse_audit["policies"]
        assert pol["placement_floor"] == {"floor": floor, "below": 2}

    def test_an_evicted_hotkey_is_not_relisted_as_pending(self):
        floor = PLACEMENT_FLOOR_PREDICTIONS
        alloc = _alloc({"a": (0.8, floor + 50), "b": (0.7, floor + 50),
                        "gone": (0.9, 5)}, evicted=frozenset({"gone"}))
        assert alloc.collapse_audit["placement"]["below"] == {}
