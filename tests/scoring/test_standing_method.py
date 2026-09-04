"""Standing method (rule amendment 2026-09-04): env-gated, defaults identical
to the rule in force; episode-relative entries from receipts; shrinkage."""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

from hope.scoring import standing_ledger, standing_method
from hope.scoring.daily_score_flow import WeightedEntry
from hope.scoring.episode_average import (
    ScoredEpisode,
    episode_weighted_average,
    half_life_from_env,
    prior_mass_from_env,
    standing,
)


def _receipt(root, day, entries):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{day}.json"), "w") as f:
        json.dump({"document": {"day": day, "metrics": {"entries": entries}},
                   "sha256": "x"}, f)


def _e(miner, ep, h, score, fo):
    return {"miner": miner, "episode_id": ep, "horizon_days": h,
            "score": score, "finalized_on": fo}


class TestDefaultsReproduceTheRuleInForce:
    def test_env_defaults(self):
        env = {}
        assert standing_method.standing_mode(env) == "absolute"
        assert half_life_from_env(env) == 12.0
        assert prior_mass_from_env(env) == 0.0
        assert standing_method.method_params(env) == {
            "mode": "absolute", "half_life_days": 12.0, "prior_mass": 0.0, "window_days": 35,
            "promotion_margin_abs": None, "curve_score_threshold": 0.0}

    def test_absolute_mode_reads_the_ledger(self, tmp_path):
        root = str(tmp_path)
        standing_ledger.append_entries(root, [
            WeightedEntry(miner="A", score=0.6, weight=0.2, entered_on=date(2026, 9, 1)),
            WeightedEntry(miner="B", score=0.4, weight=0.2, entered_on=date(2026, 9, 1)),
        ])
        got = standing_method.load_standing_entries(root, date(2026, 9, 4), environ={})
        want = standing_ledger.load_entries(root, as_of=date(2026, 9, 4))
        assert got == want

    def test_plain_average_unchanged_without_prior(self):
        eps = [ScoredEpisode(0.6, date(2026, 9, 4), 1.0), ScoredEpisode(0.4, date(2026, 9, 4), 1.0)]
        assert episode_weighted_average(eps, date(2026, 9, 4), half_life_days=12, prior_mass=0) == pytest.approx(0.5)

    def test_bad_env_values_fall_back(self):
        assert half_life_from_env({"SN21_STANDING_HALF_LIFE_DAYS": "zero"}) == 12.0
        assert prior_mass_from_env({"SN21_STANDING_PRIOR_MASS": "-5"}) == 0.0
        assert standing_method.standing_mode({"SN21_STANDING_MODE": "banana"}) == "absolute"


class TestShrinkage:
    def test_thin_evidence_sits_near_the_prior(self):
        eps = [ScoredEpisode(0.9, date(2026, 9, 4), 1.0)]
        # one unit of evidence at 0.9 against a prior mass of 9 at 0.0 -> 0.09
        assert episode_weighted_average(eps, date(2026, 9, 4), half_life_days=12,
                                        prior_mass=9, prior_value=0.0) == pytest.approx(0.09)

    def test_no_evidence_is_no_standing_even_with_a_prior(self):
        assert episode_weighted_average([], date(2026, 9, 4), half_life_days=12, prior_mass=250) is None

    def test_standing_uses_env_prior(self, monkeypatch):
        monkeypatch.setenv("SN21_STANDING_PRIOR_MASS", "1")
        monkeypatch.setenv("SN21_STANDING_HALF_LIFE_DAYS", "7")
        eps = [ScoredEpisode(0.5, date(2026, 9, 4), 1.0)]
        assert standing(eps, date(2026, 9, 4))["average"] == pytest.approx(0.25)


class TestEpisodeRelativeFromReceipts:
    @pytest.fixture
    def root(self, tmp_path):
        root = str(tmp_path)
        _receipt(root, "2026-09-01", [
            _e("A", "ep1", 7, 0.8, "2026-09-01"), _e("B", "ep1", 7, 0.6, "2026-09-01"),
            _e("C", "ep1", 7, 0.4, "2026-09-01"),
            _e("A", "ep2", 14, 0.5, "2026-09-01"), _e("B", "ep2", 14, 0.5, "2026-09-01"),
        ])
        _receipt(root, "2026-07-01", [_e("A", "old", 7, 0.99, "2026-07-01")])   # outside window
        os.makedirs(standing_ledger.standing_dir(root), exist_ok=True)
        with open(os.path.join(standing_ledger.standing_dir(root), "_absence_penalties.jsonl"), "w") as f:
            f.write(json.dumps({"day": "2026-09-02", "hotkey": "C", "missed": 3, "score": 0.0}) + "\n")
        standing_ledger.record_cancellation(root, "2026-09-02", "C", 1, 0.0, 1.0, "operator fault")
        return root

    def test_relative_values_and_weights(self, root):
        got = standing_method.load_relative_entries(root, date(2026, 9, 4))
        a = sorted((round(e.score, 6), e.weight) for e in got["A"])
        # ep1 field mean 0.6 -> A +0.2 (w 0.20); ep2 mean 0.5 -> A 0.0 (w 0.35)
        assert a == [(0.0, 0.35), (0.2, 0.2)]
        assert sorted(round(e.score, 6) for e in got["B"]) == [0.0, 0.0]
        assert "old" not in {e.scored_on.isoformat() for e in got["A"]}
        assert all(e.scored_on >= date(2026, 7, 31) for e in got["A"])

    def test_absence_enters_at_floor_minus_field(self, root):
        got = standing_method.load_relative_entries(root, date(2026, 9, 4))
        c = got["C"]
        absences = [e for e in c if e.weight == 1.0]
        # 3 charged, 1 cancelled -> 2 remain; field level = mean(0.6, 0.5) = 0.55
        assert len(absences) == 2
        assert all(e.score == pytest.approx(0.0 - 0.55) for e in absences)
        assert all(e.scored_on == date(2026, 9, 2) for e in absences)

    def test_mode_switch_selects_receipts(self, root, monkeypatch):
        env = {"SN21_STANDING_MODE": "episode_relative"}
        got = standing_method.load_standing_entries(root, date(2026, 9, 4), environ=env)
        assert set(got) == {"A", "B", "C"}
        assert standing_method.relative_enabled(env)

    def test_cache_invalidates_when_a_receipt_changes(self, root):
        first = standing_method.load_relative_entries(root, date(2026, 9, 4))
        _receipt(root, "2026-09-03", [_e("A", "ep3", 7, 0.9, "2026-09-03"), _e("B", "ep3", 7, 0.1, "2026-09-03")])
        second = standing_method.load_relative_entries(root, date(2026, 9, 4))
        assert len(second["A"]) == len(first["A"]) + 1

    def test_field_means(self):
        m = standing_method.field_means([_e("A", "e", 7, 1.0, "d"), _e("B", "e", 7, 0.0, "d"), _e("A", "e", 14, 0.3, "d")])
        assert m == {("e", 7): 0.5, ("e", 14): 0.3}


class TestWindowAndPromotionMargin:
    def test_window_env(self, monkeypatch):
        from hope.scoring.episode_average import window_from_env
        assert window_from_env({}) == 35
        assert window_from_env({"SN21_STANDING_WINDOW_DAYS": "28"}) == 28
        assert window_from_env({"SN21_STANDING_WINDOW_DAYS": "x"}) == 35

    def test_window_env_cuts_entries(self, monkeypatch):
        monkeypatch.setenv("SN21_STANDING_WINDOW_DAYS", "28")
        old = ScoredEpisode(0.9, date(2026, 8, 1), 1.0)     # 34 days old
        new = ScoredEpisode(0.5, date(2026, 9, 1), 1.0)
        assert episode_weighted_average([old, new], date(2026, 9, 4), half_life_days=7, prior_mass=0) == pytest.approx(0.5)

    def test_method_params_publish_window_and_margin(self):
        env = {"SN21_STANDING_MODE": "episode_relative", "SN21_STANDING_HALF_LIFE_DAYS": "7",
               "SN21_STANDING_PRIOR_MASS": "250", "SN21_STANDING_WINDOW_DAYS": "28",
               "SN21_PROMOTION_MARGIN_ABS": "0.01"}
        assert standing_method.method_params(env) == {
            "mode": "episode_relative", "half_life_days": 7.0, "prior_mass": 250.0,
            "window_days": 28, "promotion_margin_abs": 0.01, "curve_score_threshold": -1.0}
        assert standing_method.promotion_margin_abs({}) is None

    def test_absolute_margin_replaces_relative_test(self):
        from hope.scoring.champion_promotion import PromotionParams, PromotionState, observe_day
        state = PromotionState(champion="champ", last_observed=date(2026, 9, 3))
        # relative standings near zero: champion 0.005, challenger 0.012
        standings = {"champ": 0.005, "chal": 0.012}
        days = {"champ": 30, "chal": 30}
        rel = observe_day(state, date(2026, 9, 4), standings, days, PromotionParams())
        # 0.012 >= 0.005 * 1.05 -> the relative test is trivially met near zero
        assert rel.state.challenger == "chal"
        absp = observe_day(state, date(2026, 9, 4), standings, days, PromotionParams(margin_abs=0.01))
        assert absp.state.challenger is None          # 0.012 < 0.005 + 0.01
        absp2 = observe_day(state, date(2026, 9, 4), {"champ": 0.005, "chal": 0.02}, days, PromotionParams(margin_abs=0.01))
        assert absp2.state.challenger == "chal"


class TestTopTwentyEarnUnderTheRelativeStanding:
    def test_threshold_follows_the_mode(self):
        assert standing_method.curve_score_threshold({}) == 0.0
        assert standing_method.curve_score_threshold({"SN21_STANDING_MODE": "episode_relative"}) == -1.0
        assert standing_method.curve_score_threshold({"SN21_STANDING_MODE": "episode_relative",
                                                      "SN21_CURVE_SCORE_THRESHOLD": "-0.5"}) == -0.5
        assert standing_method.method_params({"SN21_STANDING_MODE": "episode_relative"})["curve_score_threshold"] == -1.0

    def test_curve_pays_twenty_by_rank_with_negative_standings(self):
        from hope.scoring.weight_curve import CurveParams, curve_weights
        standings = {f"m{i:02d}": 0.03 - 0.004 * i for i in range(25)}   # 8 above the field, 17 below
        paid_default = {m for m, w in curve_weights(standings, CurveParams()).items() if w > 0}
        paid_relative = {m for m, w in curve_weights(standings, CurveParams(score_threshold=-1.0)).items() if w > 0}
        assert len(paid_default) == 8                 # a 0.0 threshold reads "the field" and cuts the set
        assert len(paid_relative) == 20               # the published cap, by rank
        w = curve_weights(standings, CurveParams(score_threshold=-1.0))
        top = sorted(w.items(), key=lambda kv: -kv[1])
        assert top[0][1] == pytest.approx(0.50 / sum([0.50, 0.25, 0.10] + [0.10 * 0.5 ** i for i in range(1, 18)]))
        assert top[1][1] / top[0][1] == pytest.approx(0.5) and top[2][1] / top[1][1] == pytest.approx(0.4)
