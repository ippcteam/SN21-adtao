"""One payer per model, against the evasion actually used.

THE HOLE
    The byte-identical fingerprint hashes the whole prediction object. Two
    runs of one model are therefore counted as separate payees the moment any
    field differs — including the interval bounds, which are the cheapest
    thing in the payload to perturb and which nobody has to get right to be
    paid. The hole was reported on 2026-08-07 and left to the behavioural
    layer, which needs calibrated thresholds and stays off until an operator
    sets them.

    Against real receipts the evasion is not hypothetical: pairs agree on
    every scored point estimate to full float precision across hundreds of
    predictions, and differ only on p10/p90.

THE TEST ADDED
    A second EXACT fingerprint over the point estimates alone. No tolerance,
    no parameters, nothing to calibrate wrong — but it compares what a model
    asserts rather than the envelope around it. It is flag-gated, because
    widening a published rule changes who earns.
"""

from hope.scoring.duplication import (
    MIN_DISTINCT_POINTS,
    fingerprints_from_receipt,
    point_estimates,
    point_fingerprints_enabled,
    point_fingerprints_from_receipt,
    prediction_collisions,
)


def _prediction(p50, p10=-1.0, p90=1.0):
    return {
        "cost_delta_pct": {"p10": p10, "p50": p50, "p90": p90},
        "goal_miss_probability": 0.35,
    }


def _entries(miner, values, jitter=0.0):
    return [
        {"miner": miner, "episode_id": f"e{i}", "horizon_days": 7,
         "prediction": _prediction(v, p10=-1.0 + jitter, p90=1.0 + jitter)}
        for i, v in enumerate(values)
    ]


VALUES = [((i * 37) % 100) / 100.0 - 0.5 for i in range(40)]


class TestPointEstimateExtraction:
    def test_interval_bounds_are_dropped_and_the_assertion_kept(self):
        assert point_estimates(_prediction(0.25)) == {
            "cost_delta_pct": 0.25, "goal_miss_probability": 0.35}

    def test_unknown_fields_are_kept_rather_than_silently_dropped(self):
        """A field we cannot classify is safer compared than ignored — a
        copier must not be able to hide behind a key we forgot about."""
        out = point_estimates({"novel_metric": 7, "m": {"p10": 0, "p50": 1, "p90": 2}})
        assert out == {"novel_metric": 7, "m": 1}

    def test_a_bare_scalar_survives(self):
        assert point_estimates(0.5) == 0.5


class TestTheEvasion:
    def test_jittered_bounds_defeat_the_byte_identical_test(self):
        """This is the hole, stated as a test: same model, noise on the
        bounds, two payees."""
        entries = _entries("a", VALUES) + _entries("b", VALUES, jitter=1e-9)
        prints = fingerprints_from_receipt(entries)
        assert prints["a"] != prints["b"]
        assert prediction_collisions(prints) == []

    def test_the_point_estimate_test_catches_it(self):
        entries = _entries("a", VALUES) + _entries("b", VALUES, jitter=1e-9)
        prints = point_fingerprints_from_receipt(entries)
        assert prints["a"] == prints["b"]
        groups = prediction_collisions(prints)
        assert len(groups) == 1
        assert len(groups[0].copies) == 1

    def test_byte_identical_copies_are_still_caught_by_both(self):
        """Widening the test must not lose the case it already handled."""
        entries = _entries("a", VALUES) + _entries("b", VALUES)
        assert prediction_collisions(fingerprints_from_receipt(entries))
        assert prediction_collisions(point_fingerprints_from_receipt(entries))


class TestItDoesNotPunishHonestModels:
    def test_genuinely_different_predictions_stay_separate(self):
        other = [v + 0.01 for v in VALUES]
        entries = _entries("a", VALUES) + _entries("b", other)
        prints = point_fingerprints_from_receipt(entries)
        assert prints["a"] != prints["b"]
        assert prediction_collisions(prints) == []

    def test_one_differing_prediction_is_enough_to_stay_separate(self):
        """The test is exact: agreement must be total. A model that differs
        anywhere is a different model."""
        nearly = list(VALUES)
        nearly[17] += 1e-12
        entries = _entries("a", VALUES) + _entries("b", nearly)
        prints = point_fingerprints_from_receipt(entries)
        assert prints["a"] != prints["b"]

    def test_a_flat_predictor_gets_no_point_fingerprint(self):
        """Two models that both answer 'no change' everywhere agree because
        the question was easy, not because one copied the other. Below the
        distinct-value floor they are not fingerprinted at all."""
        flat = [0.0] * 40
        prints = point_fingerprints_from_receipt(
            _entries("a", flat) + _entries("b", flat))
        assert prints == {}

    def test_just_enough_variety_is_fingerprinted(self):
        values = [i / 100.0 for i in range(MIN_DISTINCT_POINTS)] * 5
        prints = point_fingerprints_from_receipt(
            _entries("a", values) + _entries("b", values))
        assert set(prints) == {"a", "b"}

    def test_a_miner_with_no_predictions_is_not_a_copy_of_another(self):
        prints = point_fingerprints_from_receipt(
            [{"miner": "a", "episode_id": "e1", "horizon_days": 7,
              "prediction": None}] + _entries("b", VALUES))
        assert set(prints) == {"b"}


class TestItIsOffUntilSwitchedOn:
    def test_the_flag_is_off_by_default(self):
        assert point_fingerprints_enabled({}) is False

    def test_an_unrecognised_value_leaves_it_off(self):
        """A typo in a deploy variable must never widen a published rule."""
        for raw in ("", "0", "no", "off", "maybe", "TRUE!"):
            assert point_fingerprints_enabled(
                {"SN21_ONE_PAYER_POINT_ESTIMATES": raw}) is False

    def test_it_can_be_switched_on(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            assert point_fingerprints_enabled(
                {"SN21_ONE_PAYER_POINT_ESTIMATES": raw}) is True

    def test_the_detector_only_reads_point_indexes_when_enabled(self):
        import inspect

        from hope.validator import daily_stream_weights as m
        src = inspect.getsource(m.one_payer_suppression_from_receipts)
        assert "point_fingerprints_enabled(environ)" in src, (
            "widening the rule must be gated, like every other control that "
            "decides who is paid")
