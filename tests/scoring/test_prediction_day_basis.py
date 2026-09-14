"""Rule amendment 2026-09-14 — "current model, current form".

Entries age from the day the prediction was made; a replaced model's entries
count at a quarter once the current model carries the placement floor's
worth of evidence. Announced first, applied from a date; unset, nothing
changes. Every input is a published document: the receipt (prediction day,
or the settle schedule for older receipts) and the audit (model_since).
"""

import json
import os
from datetime import date, timedelta

import pytest

from hope.scoring import standing_method
from hope.scoring.episode_average import (
    ScoredEpisode,
    age_basis_in_force,
    episode_weighted_average,
    prediction_basis_in_force,
    prior_mass_in_force,
    scored_prediction_count,
    window_in_force,
)
from hope.scoring.model_epoch import (
    apply_previous_model_discount,
    load_model_since,
    write_model_since,
)

RELATIVE = {"SN21_STANDING_MODE": "episode_relative",
            "SN21_STANDING_HALF_LIFE_DAYS": "7",
            "SN21_STANDING_WINDOW_DAYS": "28",
            "SN21_STANDING_PRIOR_MASS": "250"}
V2 = {**RELATIVE, "SN21_STANDING_AGE_BASIS": "prediction_day",
      "SN21_STANDING_AGE_BASIS_EFFECTIVE_FROM": "2026-09-15"}
DAY = date(2026, 9, 20)


class TestTheSwitch:
    def test_unset_is_the_settle_day_rule(self):
        assert age_basis_in_force(RELATIVE, DAY) == "settle_day"
        assert window_in_force(RELATIVE, DAY) == 28
        assert prior_mass_in_force(RELATIVE, DAY) == 250.0

    def test_the_effective_date_gates_it(self):
        assert prediction_basis_in_force(V2, date(2026, 9, 14)) is False
        assert prediction_basis_in_force(V2, date(2026, 9, 15)) is True
        assert window_in_force(V2, date(2026, 9, 14)) == 28
        assert window_in_force(V2, date(2026, 9, 15)) == 42
        assert prior_mass_in_force(V2, date(2026, 9, 15)) == 100.0

    def test_it_needs_the_relative_amendment_underneath(self):
        env = {k: v for k, v in V2.items() if k != "SN21_STANDING_MODE"}
        assert prediction_basis_in_force(env, DAY) is False

    def test_the_published_parameters_name_it(self):
        p = standing_method.method_params(V2, DAY)
        assert p["age_basis"] == "prediction_day"
        assert p["age_basis_effective_from"] == "2026-09-15"
        assert p["window_days"] == 42 and p["prior_mass"] == 100.0
        assert p["previous_model_weight"] == 0.25
        assert p["previous_model_threshold"] == 250.0
        before = standing_method.method_params(V2, date(2026, 9, 14))
        assert before["age_basis"] == "settle_day"
        assert before["previous_model_weight"] is None


class TestAgingFromThePredictionDay:
    def test_the_average_ages_an_entry_from_aged_from_when_set(self):
        old = ScoredEpisode(score=0.0, scored_on=DAY, weight=1.0,
                            aged_from=DAY - timedelta(days=35))
        fresh = ScoredEpisode(score=0.1, scored_on=DAY, weight=1.0)
        avg = episode_weighted_average([old, fresh], DAY, half_life_days=7,
                                       window_days=42, prior_mass=0)
        # the old entry carries 2^-5 of the fresh one's weight
        assert avg == pytest.approx(0.1 * 1 / (1 + 2 ** -5))
        assert scored_prediction_count([old, fresh], DAY, window_days=28) == 1.0

    def test_the_loader_dates_entries_by_the_receipts_predicted_on(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 7, 0.6, "2026-09-18", predicted_on="2026-09-10"),
            _e("b", "ep1", 7, 0.4, "2026-09-18", predicted_on="2026-09-10"),
        ])
        got = standing_method.load_relative_entries(root, DAY, environ=V2)
        [a] = got["a"]
        assert a.scored_on == date(2026, 9, 18)
        assert a.aged_from == date(2026, 9, 10)
        assert a.score == pytest.approx(0.1)

    def test_an_older_receipt_derives_the_day_from_the_settle_schedule(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 28, 0.6, "2026-09-18"),
            _e("b", "ep1", 28, 0.4, "2026-09-18"),
        ])
        got = standing_method.load_relative_entries(root, DAY, environ=V2)
        [a] = got["a"]
        assert a.aged_from == date(2026, 9, 18) - timedelta(days=28 + 3)

    def test_the_executors_basket_map_gives_the_exact_day(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 28, 0.6, "2026-09-18"),
            _e("b", "ep1", 28, 0.4, "2026-09-18"),
        ])
        d = os.path.join(root, "tkeys")
        os.makedirs(d)
        with open(os.path.join(d, "BD-2026-08-20.json"), "w") as f:
            json.dump({"ep1": "BUDGET_CHANGE"}, f)
        [a] = standing_method.load_relative_entries(root, DAY, environ=V2)["a"]
        assert a.aged_from == date(2026, 8, 20)

    def test_the_settling_window_is_the_platforms_two_days_unless_told_otherwise(self):
        from hope.scoring.episode_average import settle_lag_days
        assert settle_lag_days({}) == 3
        assert settle_lag_days({"SN21_OUTCOME_SETTLING_WINDOW_DAYS": "7"}) == 8
        assert settle_lag_days({"SN21_OUTCOME_SETTLING_WINDOW_DAYS": "x"}) == 3

    def test_the_window_is_applied_to_the_prediction_day(self, tmp_path):
        root = str(tmp_path)
        # settled 3 days ago but predicted 50 days ago: outside a 42-day window
        _receipt(root, "2026-09-17", [
            _e("a", "ep1", 28, 0.6, "2026-09-17", predicted_on="2026-08-01"),
            _e("b", "ep1", 28, 0.4, "2026-09-17", predicted_on="2026-08-01"),
        ])
        assert standing_method.load_relative_entries(root, DAY, environ=V2) == {}
        # and the settle-day rule still keeps it
        assert "a" in standing_method.load_relative_entries(root, DAY, environ=RELATIVE)

    def test_the_settle_day_rule_leaves_aged_from_unset(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 7, 0.6, "2026-09-18", predicted_on="2026-09-10"),
            _e("b", "ep1", 7, 0.4, "2026-09-18", predicted_on="2026-09-10"),
        ])
        [a] = standing_method.load_relative_entries(root, DAY, environ=RELATIVE)["a"]
        assert a.aged_from is None

    def test_absolute_scores_share_the_dating(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 7, 0.6, "2026-09-18", predicted_on="2026-09-10"),
            _e("b", "ep1", 7, 0.4, "2026-09-18", predicted_on="2026-09-10"),
        ])
        [a] = standing_method.load_relative_entries(root, DAY, environ=V2,
                                                    relative=False)["a"]
        assert a.score == 0.6 and a.aged_from == date(2026, 9, 10)


class TestThePreviousModelDiscount:
    SINCE = {"m": date(2026, 9, 10)}

    def _entries(self, current_mass, previous=3):
        eps = [ScoredEpisode(score=0.5, scored_on=DAY, weight=1.0,
                             aged_from=date(2026, 9, 1)) for _ in range(previous)]
        eps += [ScoredEpisode(score=0.5, scored_on=DAY, weight=1.0,
                              aged_from=date(2026, 9, 12))
                for _ in range(int(current_mass))]
        return {"m": eps}

    def test_a_model_that_has_shown_nothing_sheds_nothing(self):
        out, stats = apply_previous_model_discount(
            self._entries(current_mass=0), self.SINCE, DAY, 42, 0.25, 250)
        assert all(e.weight == 1.0 for e in out["m"])
        assert stats["hotkeys_discounted"] == []

    def test_the_discount_ramps_linearly_with_the_current_models_mass(self):
        # half the floor's evidence: half-way from 1.0 to 0.25
        out, stats = apply_previous_model_discount(
            self._entries(current_mass=125), self.SINCE, DAY, 42, 0.25, 250)
        old = sorted(e.weight for e in out["m"])[:3]
        assert old == pytest.approx([0.625, 0.625, 0.625])
        assert stats["factor"] == {"m": 0.625}

    def test_at_and_above_the_threshold_the_old_entries_count_at_the_fraction(self):
        for mass in (250, 400):
            out, stats = apply_previous_model_discount(
                self._entries(current_mass=mass), self.SINCE, DAY, 42, 0.25, 250)
            weights = sorted(e.weight for e in out["m"])
            assert weights[:3] == [0.25, 0.25, 0.25] and set(weights[3:]) == {1.0}
            assert stats["hotkeys_discounted"] == ["m"]
            assert stats["factor"] == {"m": 0.25}
            assert stats["entries_discounted"] == 3

    def test_a_hotkey_with_no_known_model_is_left_alone(self):
        out, stats = apply_previous_model_discount(
            self._entries(current_mass=300), {}, DAY, 42, 0.25, 250)
        assert all(e.weight == 1.0 for e in out["m"])

    def test_only_entries_inside_the_window_count_toward_the_mass(self):
        eps = self._entries(current_mass=300)
        eps["m"] = [ScoredEpisode(e.score, e.scored_on, e.weight,
                                  aged_from=date(2026, 7, 1))
                    if e.aged_from == date(2026, 9, 12) else e for e in eps["m"]]
        out, _ = apply_previous_model_discount(eps, self.SINCE, DAY, 42, 0.25, 250)
        assert all(e.weight == 1.0 for e in out["m"])

    def test_the_loader_applies_it_end_to_end(self, tmp_path):
        root = str(tmp_path)
        entries = []
        for i in range(260):
            entries.append(_e("m", f"new{i}", 7, 0.6, "2026-09-19", predicted_on="2026-09-12", weight=1.0))
            entries.append(_e("x", f"new{i}", 7, 0.4, "2026-09-19", predicted_on="2026-09-12", weight=1.0))
        entries.append(_e("m", "old", 7, 0.1, "2026-09-19", predicted_on="2026-09-01", weight=1.0))
        entries.append(_e("x", "old", 7, 0.9, "2026-09-19", predicted_on="2026-09-01", weight=1.0))
        _receipt(root, "2026-09-19", entries)
        stats: dict = {}
        got = standing_method.load_relative_entries(
            root, DAY, environ=V2, model_since={"m": date(2026, 9, 10)}, stats=stats)
        old = [e for e in got["m"] if e.aged_from == date(2026, 9, 1)]
        assert [e.weight for e in old] == [0.25]
        assert stats["previous_model"]["hotkeys_discounted"] == ["m"]
        assert stats["age_basis"] == "prediction_day"


def _shadow(root, day, hotkey, digest):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_run.json"), "w") as f:
        json.dump({"day": day}, f)
    with open(os.path.join(d, f"{hotkey}.jsonl"), "a") as f:
        f.write(json.dumps({"day": day, "hotkey": hotkey, "image_digest": digest,
                            "ok": True, "predictions": {}}) + "\n")


class TestModelSinceFile:
    def test_round_trip(self, tmp_path):
        root = str(tmp_path)
        n = write_model_since(root, {"a": {"digest": "sha256:1", "since": "2026-09-07"},
                                     "b": {"digest": "sha256:2", "since": "2026-08-20T10:00:00"},
                                     "c": {"digest": "sha256:3", "since": None}})
        assert n == 2
        assert load_model_since(root) == {"a": date(2026, 9, 7), "b": date(2026, 8, 20)}
        raw = json.load(open(os.path.join(root, "model_since.json")))
        assert raw["a"]["changes"] == 0

    def test_the_boundary_is_the_first_day_the_current_digest_ran(self, tmp_path):
        from hope.scoring.model_epoch import model_since_from_shadow
        root = str(tmp_path)
        for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
            _shadow(root, day, "a", "sha256:old")
        for day in ("2026-09-04", "2026-09-05"):
            _shadow(root, day, "a", "sha256:new")
        _shadow(root, "2026-09-05", "b", "sha256:b")
        out = model_since_from_shadow(root)
        assert out["a"] == {"digest": "sha256:new", "since": "2026-09-04", "changes": 1}
        assert out["b"] == {"digest": "sha256:b", "since": "2026-09-05", "changes": 0}

    def test_repeated_changes_inside_the_window_keep_the_earliest_boundary(self, tmp_path):
        """Re-committing after every bad stretch must not restart the discount."""
        from hope.scoring.model_epoch import model_since_from_shadow
        root = str(tmp_path)
        _shadow(root, "2026-09-01", "a", "sha256:v1")
        _shadow(root, "2026-09-02", "a", "sha256:v2")
        _shadow(root, "2026-09-03", "a", "sha256:v2")
        _shadow(root, "2026-09-04", "a", "sha256:v3")
        _shadow(root, "2026-09-05", "a", "sha256:v3")
        out = model_since_from_shadow(root)
        assert out["a"] == {"digest": "sha256:v3", "since": "2026-09-02", "changes": 2}

    def test_a_digest_unchanged_across_the_window_has_no_boundary_inside_it(self, tmp_path):
        from hope.scoring.model_epoch import model_since_from_shadow
        root = str(tmp_path)
        for day in ("2026-09-03", "2026-09-04", "2026-09-05"):
            _shadow(root, day, "a", "sha256:same")
        out = model_since_from_shadow(root)
        assert out["a"]["changes"] == 0 and out["a"]["since"] == "2026-09-03"
        # the loader then discounts nothing: every entry in the window is current
        from hope.scoring.model_epoch import apply_previous_model_discount
        eps = {"a": [ScoredEpisode(0.5, DAY, 1.0, aged_from=date(2026, 9, 4)) for _ in range(300)]}
        out2, stats = apply_previous_model_discount(eps, {"a": date(2026, 9, 3)}, DAY, 42, 0.25, 250)
        assert stats["hotkeys_discounted"] == []

    def test_a_change_outside_the_window_does_not_count(self, tmp_path):
        from hope.scoring.model_epoch import model_since_from_shadow
        root = str(tmp_path)
        _shadow(root, "2026-07-01", "a", "sha256:old")
        for day in ("2026-09-04", "2026-09-05"):
            _shadow(root, day, "a", "sha256:new")
        out = model_since_from_shadow(root, window_days=42)
        assert out["a"]["changes"] == 0 and out["a"]["since"] == "2026-09-04"

    def test_a_hotkey_that_did_not_run_today_keeps_its_previous_entry(self, tmp_path):
        from hope.scoring.model_epoch import model_since_from_shadow
        root = str(tmp_path)
        _shadow(root, "2026-09-05", "a", "sha256:new")
        previous = {"gone": {"digest": "sha256:g", "since": "2026-08-01", "changes": 1}}
        out = model_since_from_shadow(root, previous)
        assert out["gone"]["since"] == "2026-08-01"
        assert out["a"]["since"] == "2026-09-05"

    def test_no_shadow_ledger_keeps_whatever_was_known(self, tmp_path):
        from hope.scoring.model_epoch import model_since_from_shadow
        assert model_since_from_shadow(str(tmp_path), {"a": {"digest": "x", "since": "2026-09-01"}}) == \
            {"a": {"digest": "x", "since": "2026-09-01"}}
        assert model_since_from_shadow(str(tmp_path)) == {}

    def test_missing_file_means_no_boundary(self, tmp_path):
        assert load_model_since(str(tmp_path)) == {}

    def test_load_standing_entries_reads_it_from_the_root(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-18", [
            _e("a", "ep1", 7, 0.6, "2026-09-18", predicted_on="2026-09-10"),
            _e("b", "ep1", 7, 0.4, "2026-09-18", predicted_on="2026-09-10"),
        ])
        write_model_since(root, {"a": {"digest": "sha256:1", "since": "2026-09-05"}})
        stats: dict = {}
        standing_method.load_standing_entries(root, DAY, environ=V2, stats=stats)
        assert stats["age_basis"] == "prediction_day"
        assert "previous_model" in stats


class TestTheReceiptCarriesThePredictionDay:
    def test_entries_gain_predicted_on_when_the_map_is_given(self):
        from hope.publication.receipt_feed import build_receipt_metrics
        from hope.scoring.daily_score_flow import HorizonResult
        r = HorizonResult(episode_id="ep1", horizon_days=7, miner="a",
                          score=0.5, finalized_on=date(2026, 9, 18),
                          resolution="HIGH")
        metrics = build_receipt_metrics([], {"ep1": {"a": {"7": {"p50": 1}}}},
                                        [r], {}, predicted_on_map={"ep1": "2026-09-10"})
        [entry] = metrics["entries"]
        assert entry["predicted_on"] == "2026-09-10"
        without = build_receipt_metrics([], {"ep1": {"a": {"7": {"p50": 1}}}}, [r], {})
        assert "predicted_on" not in without["entries"][0]


def _receipt(root, day, entries):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{day}.json"), "w") as f:
        json.dump({"document": {"day": day, "metrics": {"entries": entries}},
                   "sha256": "x"}, f)


def _e(miner, ep, h, score, fo, predicted_on=None, weight=None):
    out = {"miner": miner, "episode_id": ep, "horizon_days": h,
           "score": score, "finalized_on": fo}
    if predicted_on:
        out["predicted_on"] = predicted_on
    if weight is not None:
        out["weight"] = weight
    return out


class TestTheDryRun:
    """The amendment computes beside the rule in force before its date is
    announced, so the switch changes nothing that has not been seen."""

    def test_preview_ranks_under_both_rules_without_touching_either(self, tmp_path):
        root = str(tmp_path)
        entries = []
        # a and b share 300 fresh episodes; b also has an old bad month
        for i in range(300):
            entries.append(_e("a", f"n{i}", 7, 0.6, "2026-09-19", predicted_on="2026-09-12", weight=1.0))
            entries.append(_e("b", f"n{i}", 7, 0.6, "2026-09-19", predicted_on="2026-09-12", weight=1.0))
        for i in range(300):
            entries.append(_e("a", f"o{i}", 28, 0.6, "2026-09-19", predicted_on="2026-08-15", weight=1.0))
            entries.append(_e("b", f"o{i}", 28, 0.2, "2026-09-19", predicted_on="2026-08-15", weight=1.0))
        _receipt(root, "2026-09-19", entries)
        write_model_since(root, {"b": {"digest": "sha256:new", "since": "2026-09-10"}})
        env = {**RELATIVE, "SN21_STANDING_AGE_BASIS_PREVIEW": "1"}
        assert standing_method.preview_enabled(env)
        out = standing_method.standing_preview(root, DAY, env, placement_floor=50)
        assert out["current"]["window_days"] == 28 and out["preview"]["window_days"] == 42
        assert out["preview"]["age_basis"] == "prediction_day"
        # under the rule in force b's bad month lands fresh: b below a
        assert out["hotkeys"]["b"]["current_rank"] == 2
        # under the preview b's old entries are aged and discounted: closer to a
        assert out["hotkeys"]["b"]["preview_relative"] > out["hotkeys"]["b"]["current_relative"]
        assert out["preview"]["previous_model"]["hotkeys_discounted"] == ["b"]
        # and nothing about the rule in force moved
        assert age_basis_in_force(env, DAY) == "settle_day"

    def test_off_by_default(self):
        assert standing_method.preview_enabled({}) is False


class TestTheDryRunWithControls:
    def test_it_reports_the_paid_set_under_both_rules_and_writes_nothing(self, tmp_path):
        root = str(tmp_path)
        entries = []
        for i in range(300):
            for hk, score in (("a", 0.6), ("b", 0.5), ("c", 0.4)):
                entries.append(_e(hk, f"n{i}", 7, score, "2026-09-19",
                                  predicted_on="2026-09-12", weight=1.0))
        _receipt(root, "2026-09-19", entries)
        env = {**RELATIVE, "SN21_DAILY_STREAM_WEIGHTS": "1",
               "SN21_PLACEMENT_FLOOR_PREDICTIONS": "50"}
        before = sorted(os.listdir(root))
        # a thin day: the hold would withhold the vector under both rules, and
        # the preview ignores it so the paid comparison is not empty
        env["SN21_D3_MIN_DAILY_EPISODES"] = "150"
        out = standing_method.standing_preview(root, DAY, env, with_controls=True,
                                               coldkey_of={"a": "ck1", "b": "ck2", "c": "ck3"},
                                               alpha_of={"a": 900.0, "b": 900.0, "c": 900.0},
                                               alpha_floor=700.0, day_episode_volume=20)
        assert out["with_controls"] is True
        s = out["summary"]
        assert set(s["paid_now"]) == {"a", "b", "c"} and set(s["paid_preview"]) == {"a", "b", "c"}
        assert s["paid_seats_changed"] == 0
        assert out["hotkeys"]["a"]["paid_now"] is True and out["hotkeys"]["a"]["paid_preview"] is True
        # dry run: no promotion state, no events, nothing new on the disk
        assert sorted(os.listdir(root)) == before
