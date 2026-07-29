"""J3 — per-vertical error series: store idempotency + weekly fold + view."""

from hope.scoring.vertical_error_series import (
    SeriesEntry,
    append_entries,
    condition2_view,
    load_entries,
    weekly_series,
)


def _e(ep, h, vertical, score, day, miner="ref", w=0.4):
    return SeriesEntry(
        episode_id=ep, horizon_days=h, miner=miner, vertical=vertical,
        score=score, pinball_component=score, direction_component=score,
        entry_weight=w, settled_on=day,
    )


def test_append_idempotent_per_entry(tmp_path):
    root = str(tmp_path)
    e = _e("ep1", 7, "ecommerce", 0.8, "2026-08-11")
    assert append_entries(root, [e]) == 1
    assert append_entries(root, [e]) == 0          # replay writes nothing
    assert append_entries(root, [_e("ep1", 14, "ecommerce", 0.7, "2026-08-18")]) == 1
    assert len(load_entries(root)) == 2


def test_weekly_fold_weighted_error():
    entries = [
        _e("a", 7, "ecommerce", 0.9, "2026-08-11", w=0.5).__dict__,
        _e("a", 14, "ecommerce", 0.7, "2026-08-11", w=0.5).__dict__,
        _e("b", 7, "lead_gen", 0.8, "2026-08-12", w=1.0).__dict__,
    ]
    series = weekly_series(entries)
    ec = next(r for r in series if r["vertical"] == "ecommerce")
    # weighted error: (0.1*0.5 + 0.3*0.5) / 1.0 = 0.2
    assert ec["mean_error"] == 0.2
    assert ec["episode_count"] == 1                # one episode, two horizons
    lg = next(r for r in series if r["vertical"] == "lead_gen")
    assert lg["mean_error"] == 0.2


def test_miner_filter_selects_leading_model():
    entries = [
        _e("a", 7, "ecommerce", 0.9, "2026-08-11", miner="champion").__dict__,
        _e("a", 7, "ecommerce", 0.1, "2026-08-11", miner="tail").__dict__,
    ]
    series = weekly_series(entries, miner="champion")
    assert len(series) == 1
    assert abs(series[0]["mean_error"] - 0.1) < 1e-9


def test_condition2_view_excess():
    series = [
        {"week": "2026-W33", "vertical": "ecommerce", "mean_error": 0.35, "episode_count": 40},
        {"week": "2026-W33", "vertical": "lead_gen", "mean_error": 0.20, "episode_count": 200},
    ]
    view = condition2_view(series)
    assert view[0]["ecommerce_excess"] == 0.15     # positive = worse on ecomm
    assert view[0]["ecommerce_episodes"] == 40


def test_condition2_view_missing_vertical_is_none():
    view = condition2_view([
        {"week": "2026-W33", "vertical": "lead_gen", "mean_error": 0.2, "episode_count": 5},
    ])
    assert view[0]["ecommerce_error"] is None
    assert view[0]["ecommerce_excess"] is None
