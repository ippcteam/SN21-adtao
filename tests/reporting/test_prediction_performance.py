"""The cumulative Prediction Performance document — cutoff, rollups, cells.

The page's three promises, each pinned here:
  1. Only rich-era episodes count — cutoff decided by BASKET day, and an
     episode whose basket is unknown is dropped VISIBLY, never silently.
  2. Every entry lands in exactly one rollup group; unmatched families go
     to "other" and UNKNOWN stays its own visible group.
  3. A cell's best-model seat needs MIN_N_FOR_BEST entries, the winner
     column reflects the injected leaderboard #1, and the solid flag turns
     on the 500-score line.
"""

from __future__ import annotations

import pytest

from hope.reporting.prediction_performance import (
    build_performance_document,
    group_of,
)


def entry(eid, h, miner, score, components=None, tkey=None):
    e = {"episode_id": eid, "horizon_days": h, "miner": miner, "score": score,
         "components": components}
    if tkey is not None:
        e["transition_key"] = tkey
    return e


COMP = {"quantile": 0.8, "direction": 1.0, "coverage": 0.5, "goal": 0.6}


def build(entries, **kw):
    defaults = dict(
        key_of=kw.pop("key_of", {}),
        basket_day_of=kw.pop("basket_day_of", {}),
        as_of="2026-09-02",
        winner=kw.pop("winner", None),
        uid_of=kw.pop("uid_of", {}),
    )
    return build_performance_document(entries, **defaults, **kw)


class TestCutoff:
    def test_pre_cutoff_and_unknown_basket_are_dropped_visibly(self):
        doc = build(
            [entry("old", 7, "m1", 0.5), entry("new", 7, "m1", 0.9),
             entry("lost", 7, "m1", 0.7)],
            key_of={"old": "BUDGET:up", "new": "BUDGET:up",
                    "lost": "BUDGET:up"},
            basket_day_of={"old": "2026-08-10", "new": "2026-08-25"},
        )
        assert doc["totals"]["entries"] == 1
        assert doc["totals"]["dropped_before_cutoff"] == 1
        assert doc["totals"]["dropped_no_basket_day"] == 1

    def test_unknown_horizon_is_dropped_visibly(self):
        doc = build([entry("e", 3, "m1", 0.5)],
                    basket_day_of={"e": "2026-08-25"})
        assert doc["totals"]["entries"] == 0
        assert doc["totals"]["dropped_unknown_horizon"] == 1


class TestRollups:
    @pytest.mark.parametrize("tkey,expected", [
        ("BUDGET:up_large", "budget"),
        ("BID_SWITCH:manual_cpc->maximize_conversions", "bid_switch"),
        ("TARGET:maximize_conversions:up", "bid_target"),
        ("NEGATIVE_KEYWORD_ADD", "negative_keyword"),
        ("KEYWORD_ADD", "targeting"),
        ("GEO_CHANGE", "targeting"),
        ("CRITERION_BID_CHANGE", "targeting"),
        ("CAMPAIGN_PAUSE", "pause_enable"),
        ("AD_CREATE", "ads_assets"),
        ("ASSET_CHANGE", "ads_assets"),
        ("COMPOSITE:AD_CREATE+2", "combined"),
        ("UNKNOWN", "unlabelled"),
        (None, "unlabelled"),
        ("SOMETHING_NEW", "other"),
    ])
    def test_every_family_lands_in_exactly_one_group(self, tkey, expected):
        assert group_of(tkey) == expected

    def test_detail_rows_sit_under_their_group_with_a_group_rollup(self):
        doc = build(
            [entry("a", 7, "m1", 0.4), entry("b", 7, "m1", 0.8)],
            key_of={"a": "BUDGET:up", "b": "BUDGET:down_large"},
            basket_day_of={"a": "2026-08-25", "b": "2026-08-25"},
        )
        (g,) = doc["groups"]
        assert g["key"] == "budget"
        assert {r["key"] for r in g["rows"]} == {"BUDGET:up",
                                                 "BUDGET:down_large"}
        assert g["cells"]["7"]["n"] == 2
        assert g["cells"]["7"]["field_mean"] == 0.6

    def test_receipt_embedded_key_wins_over_the_map(self):
        doc = build(
            [entry("a", 7, "m1", 0.5, tkey="AD_CREATE")],
            key_of={"a": "BUDGET:up"},
            basket_day_of={"a": "2026-08-25"},
        )
        assert doc["groups"][0]["key"] == "ads_assets"


class TestCells:
    def test_best_needs_min_n_and_winner_reflects_injection(self):
        entries = ([entry(f"e{i}", 7, "strong", 0.9) for i in range(5)]
                   + [entry("x", 7, "lucky", 1.0)]
                   + [entry(f"w{i}", 7, "win", 0.7) for i in range(5)])
        doc = build(
            entries,
            key_of={str(e["episode_id"]): "BUDGET:up" for e in entries},
            basket_day_of={str(e["episode_id"]): "2026-08-25"
                           for e in entries},
            winner="win",
            uid_of={"strong": 7, "win": 154},
        )
        cell = doc["groups"][0]["cells"]["7"]
        assert cell["best"] == {"uid": 7, "mean": 0.9, "n": 5}
        assert cell["winner"] == {"uid": 154, "mean": 0.7, "n": 5}
        assert doc["winner_uid"] == 154

    def test_components_average_only_over_entries_that_carry_them(self):
        doc = build(
            [entry("a", 7, "m", 0.5, components=COMP),
             entry("b", 7, "m", 0.7)],
            key_of={"a": "BUDGET:up", "b": "BUDGET:up"},
            basket_day_of={"a": "2026-08-25", "b": "2026-08-25"},
        )
        cell = doc["groups"][0]["cells"]["7"]
        assert cell["components_n"] == 1
        assert cell["components"]["direction"] == 1.0

    def test_solid_flag_turns_on_the_500_line(self):
        entries = [entry(f"e{i}", 7, "m", 0.5) for i in range(500)]
        doc = build(
            entries,
            key_of={str(e["episode_id"]): "BUDGET:up" for e in entries},
            basket_day_of={str(e["episode_id"]): "2026-08-25"
                           for e in entries},
        )
        assert doc["groups"][0]["cells"]["7"]["solid"] is True

    def test_below_500_shows_but_is_marked_early(self):
        doc = build(
            [entry("a", 7, "m", 0.5)],
            key_of={"a": "BUDGET:up"},
            basket_day_of={"a": "2026-08-25"},
        )
        cell = doc["groups"][0]["cells"]["7"]
        assert cell["n"] == 1 and cell["solid"] is False
