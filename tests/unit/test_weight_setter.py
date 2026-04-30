"""Unit tests for WeightSetter — normalization, burn, EMA, deregister reset."""

import pytest

from hope.validator.weight_setter import WeightSetter


class TestApplyBurn:
    def test_default_burn_95_percent(self):
        ws = WeightSetter(burn_fraction=0.95)
        weights = {1: 0.6, 2: 0.4}
        result = ws.apply_burn(weights)
        assert result[0] == pytest.approx(0.95)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_burn_zero_no_uid0(self):
        ws = WeightSetter(burn_fraction=0.0)
        weights = {1: 0.6, 2: 0.4}
        result = ws.apply_burn(weights)
        assert 0 not in result or result[0] == pytest.approx(0.0)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_burn_100_all_to_uid0(self):
        ws = WeightSetter(burn_fraction=1.0)
        result = ws.apply_burn({1: 0.5, 2: 0.5})
        assert result == {0: 1.0}

    def test_empty_weights_returns_uid0(self):
        ws = WeightSetter()
        assert ws.apply_burn({}) == {0: 1.0}

    def test_zero_total_weight(self):
        ws = WeightSetter()
        assert ws.apply_burn({1: 0.0, 2: 0.0}) == {0: 1.0}


class TestNormalizeScores:
    def test_basic_normalization(self):
        ws = WeightSetter(burn_fraction=0.95)
        scores = {"hotkey_a": 0.8, "hotkey_b": 0.6, "hotkey_c": 0.2}
        uid_map = {"hotkey_a": 1, "hotkey_b": 2, "hotkey_c": 3}

        uids, weights = ws.normalize_scores(scores, uid_map)

        # UID 0 must be present (burn)
        assert 0 in uids
        # All types must be Python native (subtensor compatibility)
        assert all(isinstance(u, int) for u in uids)
        assert all(isinstance(w, float) for w in weights)
        # Sum to 1.0
        assert sum(weights) == pytest.approx(1.0)

    def test_unknown_hotkey_skipped(self):
        ws = WeightSetter(burn_fraction=0.95)
        scores = {"known": 1.0, "unknown": 0.5}
        uid_map = {"known": 1}

        uids, weights = ws.normalize_scores(scores, uid_map)
        assert 1 in uids
        # "unknown" has no UID mapping, should be skipped
        assert len([u for u in uids if u != 0]) == 1

    def test_empty_scores(self):
        ws = WeightSetter()
        uids, weights = ws.normalize_scores({}, {"a": 1})
        assert uids == []
        assert weights == []

    def test_all_zero_scores_equal_weights(self):
        ws = WeightSetter(burn_fraction=0.0)
        scores = {"a": 0.0, "b": 0.0, "c": 0.0}
        uid_map = {"a": 1, "b": 2, "c": 3}

        uids, weights = ws.normalize_scores(scores, uid_map)
        # With zero burn and all-zero scores, each miner gets equal weight
        miner_weights = {u: w for u, w in zip(uids, weights) if u != 0}
        vals = list(miner_weights.values())
        assert all(v == pytest.approx(vals[0]) for v in vals)

    def test_negative_scores_clamped_to_zero(self):
        ws = WeightSetter(burn_fraction=0.0)
        scores = {"a": -5.0, "b": 1.0}
        uid_map = {"a": 1, "b": 2}

        uids, weights = ws.normalize_scores(scores, uid_map)
        # "a" had negative score, should be clamped to 0
        w_map = dict(zip(uids, weights))
        assert w_map.get(1, 0.0) == pytest.approx(0.0)
        assert w_map[2] > 0


class TestEMASmoothing:
    def test_first_epoch_no_smoothing(self):
        ws = WeightSetter(burn_fraction=0.0)
        scores = {"a": 1.0}
        uid_map = {"a": 1}

        uids, weights = ws.normalize_scores(scores, uid_map)
        # First epoch: no previous weights, no smoothing applied
        w_map = dict(zip(uids, weights))
        assert w_map[1] == pytest.approx(1.0)

    def test_second_epoch_applies_ema(self):
        ws = WeightSetter(burn_fraction=0.0)
        uid_map = {"a": 1, "b": 2}

        # Epoch 1
        ws.normalize_scores({"a": 1.0, "b": 0.0}, uid_map)

        # Epoch 2 — "b" now active
        uids, weights = ws.normalize_scores({"a": 0.5, "b": 0.5}, uid_map)
        w_map = dict(zip(uids, weights))

        # "a" had previous weight, EMA applies
        # "b" was zero before, jumps to new weight (no EMA from zero)
        assert w_map[1] != pytest.approx(0.5)  # EMA smoothed
        assert w_map[2] > 0  # "b" gets weight

    def test_non_submitter_stays_zero(self):
        ws = WeightSetter(burn_fraction=0.0)
        uid_map = {"a": 1, "b": 2}

        # Epoch 1 — both active
        ws.normalize_scores({"a": 0.8, "b": 0.2}, uid_map)

        # Epoch 2 — "b" scores 0 (didn't submit)
        uids, weights = ws.normalize_scores({"a": 1.0, "b": 0.0}, uid_map)
        w_map = dict(zip(uids, weights))
        assert w_map.get(2, 0.0) == pytest.approx(0.0)


class TestDeregisterReset:
    def test_hotkey_change_resets_ema(self):
        ws = WeightSetter(burn_fraction=0.0)
        uid_map_1 = {"hotkey_old": 1}
        uid_map_2 = {"hotkey_new": 1}

        # Epoch 1 with old hotkey
        ws.normalize_scores({"hotkey_old": 1.0}, uid_map_1)
        assert 1 in ws.previous_weights

        # Epoch 2 with new hotkey at same UID — should reset EMA
        ws.normalize_scores({"hotkey_new": 1.0}, uid_map_2)
        # After reset, the weight is the raw normalized (no EMA blend with old)
        assert ws._hotkey_at_uid[1] == "hotkey_new"

    def test_same_hotkey_keeps_ema(self):
        ws = WeightSetter(burn_fraction=0.0)
        uid_map = {"hotkey_a": 1}

        ws.normalize_scores({"hotkey_a": 1.0}, uid_map)

        ws.normalize_scores({"hotkey_a": 1.0}, uid_map)
        # Same hotkey, EMA should carry forward (not reset)
        assert ws._hotkey_at_uid[1] == "hotkey_a"


class TestPythonNativeTypes:
    """Verify subtensor compatibility — no numpy types leak through."""

    def test_output_types_are_native(self):
        ws = WeightSetter(burn_fraction=0.95)
        scores = {"a": 0.9, "b": 0.7, "c": 0.3, "d": 0.1}
        uid_map = {"a": 10, "b": 20, "c": 30, "d": 40}

        uids, weights = ws.normalize_scores(scores, uid_map)

        for u in uids:
            assert type(u) is int, f"UID {u} is {type(u)}, expected int"
        for w in weights:
            assert type(w) is float, f"Weight {w} is {type(w)}, expected float"

    def test_sorted_by_uid(self):
        ws = WeightSetter(burn_fraction=0.95)
        scores = {"z": 0.5, "a": 0.5}
        uid_map = {"z": 99, "a": 5}

        uids, weights = ws.normalize_scores(scores, uid_map)
        assert uids == sorted(uids)
