"""The by-type page must receive real transition keys, not UNKNOWN.

WHY THIS EXISTS
    miner_quickstart.md promises miners their win/lose "by change type". On
    2026-09-01 every one of the day's 31,073 scored entries carried
    transition_key=UNKNOWN, because the provider walked the shadow store for
    payload-shaped records — and the shadow store holds per-miner prediction
    rows, while the payloads themselves were fetched over HTTP each morning
    and never written to disk. It scanned real files for a shape they do not
    have, found nothing, and failed soft to {} with no error anywhere.

    The fix captures the map at resolve time, when the payloads are in hand,
    as one small JSON per basket; the provider reads those maps. What is
    pinned here:

      * the map is keyed on the SAME episode_id the scorer uses (the
        top-level id fetch_basket_payloads injects), so the two key spaces
        cannot diverge again;
      * settle can label an episode from a basket 15-36 days earlier, so the
        provider must union across basket files, not read one day;
      * fail-soft survives: a bad file degrades to partial, never raises.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.run_daily_pipeline import (
    _transition_key_provider,
    transition_key_map_from_payloads,
    write_transition_key_map,
)


def _payload(eid, tkey):
    """The exact shape fetch_basket_payloads produces: top-level episode_id
    injected, transition_key inside action_bundle.bundle_summary."""
    return {
        "episode_id": eid,
        "account_state": {},
        "action_bundle": {"bundle_summary": {"transition_key": tkey,
                                             "action_count": 1}},
    }


class TestMapExtraction:
    def test_keys_on_the_injected_top_level_episode_id(self):
        m = transition_key_map_from_payloads(
            [_payload("01aac5c225b8dc1e", "BUDGET:up_large")])
        assert m == {"01aac5c225b8dc1e": "BUDGET:up_large"}

    def test_a_payload_without_a_key_is_left_out_not_invented(self):
        p = _payload("abc", None)
        assert transition_key_map_from_payloads([p]) == {}

    def test_composite_keys_pass_through_verbatim(self):
        m = transition_key_map_from_payloads(
            [_payload("x", "COMPOSITE:AD_CREATE+2")])
        assert m["x"] == "COMPOSITE:AD_CREATE+2"

    def test_garbage_entries_do_not_raise(self):
        assert transition_key_map_from_payloads(
            [None, "str", {}, {"episode_id": "y"}]) == {}


class TestPersistence:
    def test_write_then_provide_round_trip(self, tmp_path):
        write_transition_key_map(str(tmp_path), "BD-2026-09-01",
                                 [_payload("e1", "BUDGET:up_small"),
                                  _payload("e2", "KEYWORD:add")])
        provider = _transition_key_provider(str(tmp_path))
        assert provider(["e1", "e2"]) == {"e1": "BUDGET:up_small",
                                          "e2": "KEYWORD:add"}

    def test_empty_payloads_write_nothing(self, tmp_path):
        n = write_transition_key_map(str(tmp_path), "BD-x", [])
        assert n == 0
        assert not (tmp_path / "tkeys" / "BD-x.json").exists()

    def test_rewrite_is_atomic_no_tmp_left_behind(self, tmp_path):
        write_transition_key_map(str(tmp_path), "BD-a", [_payload("e", "T:x")])
        files = os.listdir(tmp_path / "tkeys")
        assert files == ["BD-a.json"]


class TestProviderSemantics:
    def test_union_across_baskets(self, tmp_path):
        """An entry settling today may come from a basket 15-36 days old —
        one day's map is never enough."""
        write_transition_key_map(str(tmp_path), "BD-2026-08-01",
                                 [_payload("old", "BUDGET:down_small")])
        write_transition_key_map(str(tmp_path), "BD-2026-09-01",
                                 [_payload("new", "ASSET:change")])
        provider = _transition_key_provider(str(tmp_path))
        assert provider(["old", "new"]) == {"old": "BUDGET:down_small",
                                            "new": "ASSET:change"}

    def test_unknown_ids_stay_absent_so_daily_loop_buckets_them(self, tmp_path):
        write_transition_key_map(str(tmp_path), "BD-a", [_payload("e", "T:x")])
        provider = _transition_key_provider(str(tmp_path))
        out = provider(["e", "never-seen"])
        assert out == {"e": "T:x"}          # missing id absent, not invented

    def test_no_tkeys_dir_returns_empty_not_error(self, tmp_path):
        assert _transition_key_provider(str(tmp_path))(["x"]) == {}

    def test_a_corrupt_file_degrades_to_partial(self, tmp_path):
        write_transition_key_map(str(tmp_path), "BD-good", [_payload("g", "T:g")])
        (tmp_path / "tkeys" / "BD-bad.json").write_text("{not json")
        provider = _transition_key_provider(str(tmp_path))
        assert provider(["g"]) == {"g": "T:g"}

    def test_ids_are_matched_as_strings(self, tmp_path):
        """The scorer stringifies ids; the provider must too, or an int id
        from a payload silently never matches."""
        write_transition_key_map(str(tmp_path), "BD-a",
                                 [_payload(12345, "T:n")])
        provider = _transition_key_provider(str(tmp_path))
        assert provider(["12345"]) == {"12345": "T:n"}


class TestWiring:
    def test_resolve_stage_persists_the_map(self):
        """The map must be written the moment payloads exist, or settle in
        two weeks has nothing to read."""
        import inspect
        import scripts.run_daily_pipeline as m
        src = inspect.getsource(m)
        resolve_block = src[src.index("# 0. resolve + fetch basket"):
                            src.index("# corpus for admission")]
        assert "write_transition_key_map" in resolve_block

    def test_provider_no_longer_walks_the_shadow_store(self):
        """The shadow store holds prediction rows, not payloads — walking it
        is the bug this file exists to prevent returning."""
        import inspect
        from scripts.run_daily_pipeline import _transition_key_provider as p
        src = inspect.getsource(p)
        assert "shadow" not in src
        assert "os.walk" not in src
