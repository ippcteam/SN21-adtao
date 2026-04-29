"""Example: Train a prediction model on HOPE episodes.

This script shows a real miner workflow:
1. Fetch training data (episodes with known outcomes)
2. Extract features from episodes
3. Train an XGBoost model
4. Evaluate predictions using the scoring library
5. Submit to the validator

Usage:
    # Train on data from the validator
    python scripts/train_example_model.py --validator-url http://localhost:8080

    # Train on downloaded data
    python scripts/train_example_model.py --data-file data/training/training_episodes.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


def extract_features(episode: dict) -> dict:
    """Extract predictive features from an episode.

    This is where the real miner value-add is. Better features = better model.
    """
    acct = episode.get("account_state", {})
    pw = episode.get("pre_window", {})
    ab = episode.get("action_bundle", {})
    agg = pw.get("account_aggregates", {})

    # Campaign time series (first campaign)
    campaigns = pw.get("campaigns", {})
    camp = list(campaigns.values())[0] if campaigns else {}
    cost = np.array(camp.get("cost_micros", [0] * 60), dtype=float)
    conv = np.array(camp.get("conversions", [0.0] * 60), dtype=float)
    imp_share = np.array(camp.get("impression_share", [0.0] * 60), dtype=float)

    # Active days (non-zero cost)
    active_mask = cost > 0
    active_days = active_mask.sum()

    # --- TREND FEATURES ---
    if active_days >= 14:
        cost_recent = cost[-14:][cost[-14:] > 0]
        cost_early = cost[:14][cost[:14] > 0]
        cost_trend = (cost_recent.mean() - cost_early.mean()) / max(cost_early.mean(), 1) if len(cost_early) > 0 and len(cost_recent) > 0 else 0
    else:
        cost_trend = 0

    # --- VOLATILITY FEATURES ---
    active_cost = cost[active_mask]
    cost_cv = float(np.std(active_cost) / np.mean(active_cost)) if len(active_cost) > 1 and np.mean(active_cost) > 0 else 0
    active_conv = conv[conv > 0]
    conv_cv = float(np.std(active_conv) / np.mean(active_conv)) if len(active_conv) > 1 and np.mean(active_conv) > 0 else 0

    # --- ACTION FEATURES ---
    actions = ab.get("actions", [])
    action = actions[0] if actions else {}
    action_type = action.get("type", "UNKNOWN")
    summary = ab.get("bundle_summary", {})

    # Magnitude
    magnitude = action.get("magnitude", {})
    spend_change_expected = 0.0
    if isinstance(magnitude, dict):
        scp = magnitude.get("spend_change_pct", {})
        if isinstance(scp, dict):
            spend_change_expected = scp.get("expected", 0.0)
        elif isinstance(scp, (int, float)):
            spend_change_expected = float(scp)

    # --- ACCOUNT FEATURES ---
    goal = acct.get("goal", {})
    goal_deviation = goal.get("deviation", 0) if goal else 0
    goal_target = goal.get("target", 0) if goal else 0

    # Encode action type as numeric
    action_type_map = {
        "BUDGET_CHANGE": 0,
        "BID_STRATEGY_CHANGE": 1,
        "TARGET_VALUE_CHANGE": 2,
        "CAMPAIGN_PAUSE": 3,
        "CAMPAIGN_ENABLE": 4,
    }

    features = {
        # Time series stats
        "avg_daily_cost": float(np.mean(active_cost)) if len(active_cost) > 0 else 0,
        "avg_daily_conv": float(np.mean(active_conv)) if len(active_conv) > 0 else 0,
        "cost_trend": cost_trend,
        "cost_cv": cost_cv,
        "conv_cv": conv_cv,
        "active_days": int(active_days),
        "avg_imp_share": float(np.mean(imp_share[imp_share > 0])) if np.any(imp_share > 0) else 0,

        # Account aggregates
        "agg_spend_cv": agg.get("spend_cv", 0),
        "agg_cpc_trend": agg.get("cpc_trend", 0),
        "agg_avg_roas": agg.get("avg_roas", 0),

        # Action
        "action_type": action_type_map.get(action_type, -1),
        "spend_change_expected": spend_change_expected,
        "has_destructive": int(summary.get("has_destructive", False)),
        "has_improvement": int(summary.get("has_improvement", False)),
        "max_risk_score": summary.get("max_risk_score", 0),
        "action_count": summary.get("action_count", 0),

        # Account
        "goal_deviation": goal_deviation,
        "goal_target": goal_target,
        "spend_bucket": {"micro": 0, "low": 1, "mid": 2, "high": 3}.get(
            acct.get("spend_bucket", "mid"), 2
        ),
    }

    return features


def extract_targets(outcome: dict) -> dict:
    """Extract prediction targets from an outcome."""
    targets = {}

    for horizon in ["t7", "t14"]:
        h = outcome.get(horizon)
        if h:
            targets[f"{horizon}_cost_delta"] = h["cost_delta_pct"]
            targets[f"{horizon}_conv_delta"] = h["conversions_delta_pct"]
            targets[f"{horizon}_eff_delta"] = h["efficiency_delta_pct"]
            targets[f"{horizon}_goal_miss"] = h["goal_miss"]
        else:
            targets[f"{horizon}_cost_delta"] = None
            targets[f"{horizon}_conv_delta"] = None
            targets[f"{horizon}_eff_delta"] = None
            targets[f"{horizon}_goal_miss"] = None

    return targets


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Train example prediction model")
    parser.add_argument("--data-file", type=str, default=None,
                        help="Path to training_episodes.json")
    parser.add_argument("--validator-url", type=str, default=None,
                        help="Validator URL to fetch training data")
    args = parser.parse_args()

    # Load training data
    if args.data_file:
        print(f"Loading from {args.data_file}...")
        with open(args.data_file) as f:
            training_data = json.load(f)
    elif args.validator_url:
        import httpx
        print(f"Fetching from {args.validator_url}/training/episodes...")
        resp = httpx.get(f"{args.validator_url}/training/episodes", timeout=60)
        training_data = resp.json()["training_episodes"]
    else:
        print("Provide --data-file or --validator-url")
        sys.exit(1)

    print(f"Training examples: {len(training_data)}")

    if len(training_data) < 3:
        print("Not enough training data (need at least 3 examples with outcomes)")
        print("Run: python scripts/generate_training_data.py  first")
        sys.exit(1)

    # =====================================================
    # STEP 1: Extract features and targets
    # =====================================================
    print("\n=== Step 1: Feature Extraction ===")

    feature_rows = []
    target_rows = []

    for example in training_data:
        features = extract_features(example["input"])
        targets = extract_targets(example["outcome"])

        # Only use examples with t7 outcomes
        if targets["t7_cost_delta"] is not None:
            feature_rows.append(features)
            target_rows.append(targets)

    print(f"  Usable examples (with t7 outcomes): {len(feature_rows)}")

    if len(feature_rows) < 3:
        print("  Not enough examples with outcomes to train")
        sys.exit(1)

    feature_names = list(feature_rows[0].keys())
    X = np.array([[row[f] for f in feature_names] for row in feature_rows])
    y_cost = np.array([row["t7_cost_delta"] for row in target_rows])
    y_conv = np.array([row["t7_conv_delta"] for row in target_rows])

    print(f"  Features: {len(feature_names)}")
    print(f"  Feature names: {feature_names}")
    print(f"  X shape: {X.shape}")
    print(f"  y_cost range: [{y_cost.min():.2f}, {y_cost.max():.2f}]")
    print(f"  y_conv range: [{y_conv.min():.2f}, {y_conv.max():.2f}]")

    # =====================================================
    # STEP 2: Train model
    # =====================================================
    print("\n=== Step 2: Train XGBoost Model ===")

    try:
        from xgboost import XGBRegressor
        from sklearn.model_selection import cross_val_score
    except ImportError:
        print("  Install XGBoost: pip install xgboost scikit-learn")
        print("  Skipping model training — showing feature extraction only")
        return

    # Train cost delta model
    model_cost = XGBRegressor(n_estimators=50, max_depth=3, learning_rate=0.1)
    model_cost.fit(X, y_cost)

    # Train conversion delta model
    model_conv = XGBRegressor(n_estimators=50, max_depth=3, learning_rate=0.1)
    model_conv.fit(X, y_conv)

    print("  Cost model trained")
    print("  Conv model trained")

    # Cross-validation (if enough data)
    if len(X) >= 5:
        cv_cost = cross_val_score(model_cost, X, y_cost, cv=min(3, len(X)), scoring="r2")
        cv_conv = cross_val_score(model_conv, X, y_conv, cv=min(3, len(X)), scoring="r2")
        print(f"  Cost R²: {cv_cost.mean():.3f} (±{cv_cost.std():.3f})")
        print(f"  Conv R²: {cv_conv.mean():.3f} (±{cv_conv.std():.3f})")

    # Feature importance
    print("\n  Top features (cost model):")
    importances = model_cost.feature_importances_
    sorted_idx = np.argsort(importances)[::-1]
    for i in sorted_idx[:5]:
        print(f"    {feature_names[i]}: {importances[i]:.3f}")

    # =====================================================
    # STEP 3: Generate predictions
    # =====================================================
    print("\n=== Step 3: Generate Predictions ===")

    predictions = []
    for row in feature_rows:
        x = np.array([[row[f] for f in feature_names]])
        cost_pred = float(model_cost.predict(x)[0])
        conv_pred = float(model_conv.predict(x)[0])

        # Estimate spread from training residuals
        spread = max(5.0, abs(cost_pred) * 0.3)

        predictions.append({
            "cost_p50": cost_pred,
            "conv_p50": conv_pred,
            "spread": spread,
        })

    print(f"  Generated {len(predictions)} predictions")

    # =====================================================
    # STEP 4: Score predictions
    # =====================================================
    print("\n=== Step 4: Score Against Ground Truth ===")

    from hope.scoring import EpochScorer
    from hope.protocol.episode import Episode
    from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction
    from hope.protocol.outcomes import Outcome, HorizonOutcome
    from datetime import datetime, timezone

    scorer = EpochScorer()

    scored_episodes = []
    scored_outcomes = []
    scored_predictions = []

    for i, (example, pred) in enumerate(zip(training_data, predictions)):
        if target_rows[i]["t7_cost_delta"] is None:
            continue

        ep = Episode.model_validate(example["input"])
        scored_episodes.append(ep)

        t7 = example["outcome"].get("t7")
        t14 = example["outcome"].get("t14")
        outcome = Outcome(
            episode_id=ep.episode_metadata.episode_id,
            t7=HorizonOutcome(**t7) if t7 else None,
            t14=HorizonOutcome(**t14) if t14 else None,
        )
        scored_outcomes.append(outcome)

        spread = pred["spread"]
        p = Prediction(
            episode_id=ep.episode_metadata.episode_id,
            miner_id="trained_model",
            submitted_at=datetime.now(timezone.utc),
            horizons={
                "7": HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(
                        p10=pred["cost_p50"] - spread,
                        p50=pred["cost_p50"],
                        p90=pred["cost_p50"] + spread,
                    ),
                    conversions_delta_pct=QuantilePrediction(
                        p10=pred["conv_p50"] - spread,
                        p50=pred["conv_p50"],
                        p90=pred["conv_p50"] + spread,
                    ),
                    efficiency_delta_pct=QuantilePrediction(p10=-spread, p50=0, p90=spread),
                    goal_miss_probability=0.3,
                    instability_risk=0.15,
                ),
                "14": HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(
                        p10=pred["cost_p50"] * 0.85 - spread * 1.1,
                        p50=pred["cost_p50"] * 0.85,
                        p90=pred["cost_p50"] * 0.85 + spread * 1.1,
                    ),
                    conversions_delta_pct=QuantilePrediction(
                        p10=pred["conv_p50"] * 0.85 - spread * 1.1,
                        p50=pred["conv_p50"] * 0.85,
                        p90=pred["conv_p50"] * 0.85 + spread * 1.1,
                    ),
                    efficiency_delta_pct=QuantilePrediction(
                        p10=-spread * 1.1, p50=0, p90=spread * 1.1
                    ),
                    goal_miss_probability=0.3,
                    instability_risk=0.1,
                ),
            },
        )
        scored_predictions.append(p)

    # Score trained model
    trained_scores = scorer.score_epoch(
        {"trained_model": scored_predictions}, scored_episodes, scored_outcomes
    )

    # Also score baseline for comparison
    from hope.miner.models.baseline import BaselineModel

    baseline = BaselineModel()
    baseline_preds = [baseline.predict(ep) for ep in scored_episodes]
    baseline_scores = scorer.score_epoch(
        {"baseline": baseline_preds}, scored_episodes, scored_outcomes
    )

    trained = trained_scores["trained_model"]
    base = baseline_scores["baseline"]

    print(f"\n  {'Model':<20} {'Raw':>8} {'Penalty':>8} {'Final':>8} {'Episodes':>8}")
    print(f"  {'-'*55}")
    print(f"  {'Trained XGBoost':<20} {trained.raw_score:>8.4f} {trained.null_penalty:>8.4f} {trained.final_score:>8.4f} {trained.episodes_scored:>8}")
    print(f"  {'Baseline':<20} {base.raw_score:>8.4f} {base.null_penalty:>8.4f} {base.final_score:>8.4f} {base.episodes_scored:>8}")
    print(f"\n  Trained model is {trained.final_score / max(base.final_score, 0.0001):.1f}x the baseline score")


if __name__ == "__main__":
    main()
