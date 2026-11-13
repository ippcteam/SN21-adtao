"""scripts/preview_count_score.py re-scores only small-campaign results, as settle would (13 Nov 2026)."""
import json
from datetime import date

from hope.scoring import count_score
from scripts import preview_count_score as P


def test_only_small_campaign_entries_are_rescored_with_the_settle_blend(tmp_path):
    (tmp_path / "receipts").mkdir()
    pred = {"cost_delta_pct": {"p50": 0.1}, "conversions_delta_pct": {"p50": 0.5}}
    entries = [{"episode_id": "small", "horizon_days": 7, "miner": "m1", "score": 0.6, "prediction": pred,
                "finalized_on": "2026-11-12"},
               {"episode_id": "busy", "horizon_days": 7, "miner": "m1", "score": 0.7, "prediction": pred,
                "finalized_on": "2026-11-12"}]
    (tmp_path / "receipts" / "2026-11-12.json").write_text(json.dumps({"document": {"metrics": {"entries": entries}}}))
    adj = P.adjusted_scores(str(tmp_path), date(2026, 11, 13), 28, {("small", 7): (2.0, 3.0)})
    want = count_score.blend(0.6, count_score.count_component(pred, 2.0, 3.0))
    assert adj == {("small", 7, "m1"): want} and want != 0.6
