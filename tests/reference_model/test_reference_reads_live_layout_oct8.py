"""The reference model's budget lean fires on the live daily basket layout.

Live episodes carry the budget change in payload.action_bundle.actions[].magnitude
(previous_amount_micros / new_amount_micros) under a BUDGET:* transition key; the
model read only flat weekly-era fields, so on every live basket it fell back to
its constants (found 7 Oct 2026). The flat layout must keep working.
"""
import importlib.util
import os

_p = os.path.join(os.path.dirname(__file__), "..", "..", "reference_model", "model.py")
_spec = importlib.util.spec_from_file_location("reference_model", _p)
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


def _live(prev, new, key="BUDGET:down"):
    return {"episode_id": "e1", "transition_key": key, "system_estimate": None,
            "payload": {"action_bundle": {
                "bundle_summary": {"transition_key": key},
                "actions": [{"type": "BUDGET_CHANGE", "magnitude": {
                    "previous_amount_micros": prev, "new_amount_micros": new}}]}}}


def test_live_budget_cut_leans_cost_down():
    out = M.predict(_live(984_640_000, 892_400_000))
    assert out["7"]["cost_delta_pct"]["p50"] == round((892.4 - 984.64) / 984.64 * 0.5, 6)


def test_training_record_wrapping_is_read_the_same_way():
    ep = _live(1_000_000_000, 1_200_000_000, "BUDGET:up")
    wrapped = {"episode_id": "e1", "input": ep, "labels": {}}
    assert M.predict(wrapped)["14"]["cost_delta_pct"]["p50"] == 0.1


def test_non_budget_episode_keeps_the_constants():
    ep = _live(1, 2, key="NEGATIVE_KEYWORD_ADD:single")
    assert M.predict(ep)["7"]["cost_delta_pct"]["p50"] == M.LEAN["cost_delta_pct"]


def test_flat_weekly_layout_still_works():
    ep = {"action_type": "BUDGET_CHANGE", "from_value": 100, "to_value": 50}
    assert M.predict(ep)["28"]["cost_delta_pct"]["p50"] == -0.25


def test_every_episode_still_gets_an_answer_on_a_real_basket():
    import json
    sample = os.environ.get("SN21_LIVE_SAMPLE", "")
    if not os.path.exists(sample):
        import pytest
        pytest.skip("no live basket sample")
    eps = json.load(open(sample))
    for ep in eps:
        ep = {k: v for k, v in ep.items() if k != "validator_only_outcomes"}
        assert set(M.predict(ep)) == {"7", "14", "28"}
