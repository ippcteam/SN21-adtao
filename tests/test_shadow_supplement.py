"""A supplement run adds to a shadow day without erasing it.

WHY (8 Sept 2026). The operator supplemented BD-2026-09-07 with the episodes
it missed. Every reader of the shadow ledger takes the LAST line per model
(day_run_status, day_coverage, finalize_day) and load_prediction_index unions
predictions per episode across lines — so the line a supplement appends must
carry the WHOLE day: original predictions verbatim, the added ones joined,
coverage over the union. Anything less would either erase the morning run
(last-line readers) or double-count nothing (index readers) — silently.
"""

from hope.backtest.container_runner import RunResult
from hope.backtest.shadow import (
    ShadowModel,
    day_coverage,
    day_run_status,
    last_record,
    merged_supplement,
    record_day,
    record_supplement,
)

M = ShadowModel(hotkey="hk1", image_digest="repo@sha256:abc", admitted_at="2026-09-01")


def _pred(*ids):
    return {i: {"h7": 0.6, "h14": 0.6, "h28": 0.6} for i in ids}


class TestMergedLine:
    def test_original_predictions_survive_verbatim_and_added_join(self):
        prev = {"day": "2026-09-07", "hotkey": "hk1", "image_digest": "repo@sha256:abc",
                "ok": True, "error": None, "episodes_in": 435, "predictions_out": 435,
                "predictions": _pred("a", "b")}
        res = RunResult(ok=True, predictions=_pred("c"), episodes_in=1, predictions_out=1,
                        duration_s=3.0)
        line = merged_supplement(prev, res, ["c"], "BD-2026-09-07")
        assert set(line["predictions"]) == {"a", "b", "c"}
        assert line["predictions"]["a"] == prev["predictions"]["a"]
        assert line["episodes_in"] == 436 and line["predictions_out"] == 3
        assert line["ok"] is True
        assert line["supplement"] == {"basket": "BD-2026-09-07", "added": 1,
                                      "added_predicted": 1, "previous_ok": True,
                                      "previous_episodes_in": 435,
                                      "previous_duration_s": None}

    def test_a_failed_supplement_run_reads_as_not_ok_but_keeps_the_morning(self):
        prev = {"ok": True, "episodes_in": 10, "predictions_out": 10,
                "predictions": _pred("a")}
        res = RunResult(ok=False, error="timeout", episodes_in=2, predictions_out=0)
        line = merged_supplement(prev, res, ["x", "y"], "BD-x")
        assert line["ok"] is False and line["error"] == "timeout"
        assert line["predictions"] == _pred("a")
        assert line["episodes_in"] == 12 and line["predictions_out"] == 1

    def test_a_model_that_failed_in_the_morning_stays_failed(self):
        prev = {"ok": False, "error": "oom", "episodes_in": 10, "predictions_out": 0,
                "predictions": {}}
        res = RunResult(ok=True, predictions=_pred("x"), episodes_in=1, predictions_out=1)
        line = merged_supplement(prev, res, ["x"], "BD-x")
        assert line["ok"] is False
        assert line["error"] == "oom"

    def test_no_previous_line_is_just_the_supplement(self):
        res = RunResult(ok=True, predictions=_pred("x"), episodes_in=1, predictions_out=1)
        line = merged_supplement(None, res, ["x"], "BD-x")
        assert line["episodes_in"] == 1 and line["predictions_out"] == 1 and line["ok"]


class TestLedgerReaders:
    def test_readers_see_the_whole_day_after_a_supplement(self, tmp_path):
        root = str(tmp_path)
        record_day(root, "2026-09-07", M,
                   RunResult(ok=True, predictions=_pred("a", "b"), episodes_in=2,
                             predictions_out=2))
        record_supplement(root, "2026-09-07", M,
                          RunResult(ok=True, predictions=_pred("c"), episodes_in=1,
                                    predictions_out=1),
                          ["c"], "BD-2026-09-07")
        assert day_coverage(root, "2026-09-07")["hk1"] == (3, 3)
        assert day_run_status(root, "2026-09-07")["hk1"] == (True, None)
        last = last_record(root, "2026-09-07", "hk1")
        assert set(last["predictions"]) == {"a", "b", "c"}
        assert last["day"] == "2026-09-07" and last["hotkey"] == "hk1"
        assert last["image_digest"] == "repo@sha256:abc"

    def test_settle_index_unions_the_lines(self, tmp_path):
        from hope.scoring.settle_day_flow import load_prediction_index

        root = str(tmp_path)
        record_day(root, "2026-09-07", M,
                   RunResult(ok=True, predictions=_pred("a"), episodes_in=1, predictions_out=1))
        record_supplement(root, "2026-09-07", M,
                          RunResult(ok=True, predictions=_pred("c"), episodes_in=1,
                                    predictions_out=1),
                          ["c"], "BD-2026-09-07")
        idx = load_prediction_index(root)
        assert set(idx) == {"a", "c"}
        assert idx["a"]["hk1"]["h7"] == 0.6


class TestAddedSetDerivation:
    def test_added_is_what_the_tkey_map_does_not_know(self, tmp_path):
        import json
        import os

        from scripts.supplement_shadow_day import added_payloads

        root = str(tmp_path)
        os.makedirs(os.path.join(root, "tkeys"))
        with open(os.path.join(root, "tkeys", "BD-2026-09-07.json"), "w") as fh:
            json.dump({"1": "BUDGET_CHANGE", "2": "AD_EDIT"}, fh)
        payloads = [{"episode_id": "1"}, {"episode_id": "2"}, {"episode_id": "3"}]
        assert [p["episode_id"] for p in added_payloads(root, "BD-2026-09-07", payloads)] == ["3"]

    def test_no_map_means_everything_is_new(self, tmp_path):
        from scripts.supplement_shadow_day import added_payloads

        payloads = [{"episode_id": "1"}]
        assert added_payloads(str(tmp_path), "BD-x", payloads) == payloads
