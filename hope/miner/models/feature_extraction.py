"""Feature extraction shared between training and inference.

Both `scripts/train_example_model.py` and `TrainedXGBoostModel` (used at
inference time by `hope-miner --model trained`) call `extract_features`.
Keeping the implementation in one place guarantees the inference path
sees exactly the features the training run learned from — drift between
the two is the silent killer of "saved model, then predictions don't
match training behaviour" bugs.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# Encoding for categorical action_type. Order matters — the model learns
# from these integer values, so changing it breaks serialised models.
ACTION_TYPE_MAP = {
    "BUDGET_CHANGE": 0,
    "BID_STRATEGY_CHANGE": 1,
    "TARGET_VALUE_CHANGE": 2,
    "CAMPAIGN_PAUSE": 3,
    "CAMPAIGN_ENABLE": 4,
}

SPEND_BUCKET_MAP = {"micro": 0, "low": 1, "mid": 2, "high": 3}


def extract_features(episode: dict[str, Any]) -> dict[str, float]:
    """Extract predictive features from an episode dict.

    `episode` is the JSON-shaped episode payload (e.g. as returned by
    `Episode.model_dump()` or as provided in `data/training/training_episodes.json`
    under the `input` key). Returns a flat dict of named features the
    model was trained against.
    """
    acct = episode.get("account_state", {}) or {}
    pw = episode.get("pre_window", {}) or {}
    ab = episode.get("action_bundle", {}) or {}
    agg = pw.get("account_aggregates", {}) or {}

    # Campaign time series (first campaign)
    campaigns = pw.get("campaigns", {}) or {}
    camp = next(iter(campaigns.values())) if campaigns else {}
    cost = np.array(camp.get("cost_micros", [0] * 60), dtype=float)
    conv = np.array(camp.get("conversions", [0.0] * 60), dtype=float)
    imp_share = np.array(camp.get("impression_share", [0.0] * 60), dtype=float)

    active_mask = cost > 0
    active_days = int(active_mask.sum())

    # Trend
    cost_trend = 0.0
    if active_days >= 14:
        cost_recent = cost[-14:][cost[-14:] > 0]
        cost_early = cost[:14][cost[:14] > 0]
        if len(cost_early) > 0 and len(cost_recent) > 0:
            cost_trend = (
                cost_recent.mean() - cost_early.mean()
            ) / max(cost_early.mean(), 1)

    # Volatility
    active_cost = cost[active_mask]
    cost_cv = (
        float(np.std(active_cost) / np.mean(active_cost))
        if len(active_cost) > 1 and np.mean(active_cost) > 0
        else 0.0
    )
    active_conv = conv[conv > 0]
    conv_cv = (
        float(np.std(active_conv) / np.mean(active_conv))
        if len(active_conv) > 1 and np.mean(active_conv) > 0
        else 0.0
    )

    # Action
    actions = ab.get("actions", []) or []
    action = actions[0] if actions else {}
    action_type = action.get("type", "UNKNOWN")
    summary = ab.get("bundle_summary", {}) or {}

    magnitude = action.get("magnitude", {}) or {}
    spend_change_expected = 0.0
    if isinstance(magnitude, dict):
        scp = magnitude.get("spend_change_pct", {})
        if isinstance(scp, dict):
            spend_change_expected = float(scp.get("expected", 0.0) or 0.0)
        elif isinstance(scp, (int, float)):
            spend_change_expected = float(scp)

    goal = acct.get("goal", {}) or {}

    return {
        # Time series stats
        "avg_daily_cost": float(np.mean(active_cost)) if len(active_cost) > 0 else 0.0,
        "avg_daily_conv": float(np.mean(active_conv)) if len(active_conv) > 0 else 0.0,
        "cost_trend": cost_trend,
        "cost_cv": cost_cv,
        "conv_cv": conv_cv,
        "active_days": float(active_days),
        "avg_imp_share": float(np.mean(imp_share[imp_share > 0]))
        if np.any(imp_share > 0)
        else 0.0,
        # Account aggregates
        "agg_spend_cv": float(agg.get("spend_cv", 0) or 0),
        "agg_cpc_trend": float(agg.get("cpc_trend", 0) or 0),
        "agg_avg_roas": float(agg.get("avg_roas", 0) or 0),
        # Action
        "action_type": float(ACTION_TYPE_MAP.get(action_type, -1)),
        "spend_change_expected": spend_change_expected,
        "has_destructive": float(summary.get("has_destructive", False)),
        "has_improvement": float(summary.get("has_improvement", False)),
        "max_risk_score": float(summary.get("max_risk_score", 0) or 0),
        "action_count": float(summary.get("action_count", 0) or 0),
        # Account
        "goal_deviation": float(goal.get("deviation", 0) or 0),
        "goal_target": float(goal.get("target", 0) or 0),
        "spend_bucket": float(SPEND_BUCKET_MAP.get(acct.get("spend_bucket", "mid"), 2)),
    }
