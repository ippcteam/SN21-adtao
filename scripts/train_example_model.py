"""Example: Train a prediction model on the operator episodes.

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

# Use the shared feature extractor — same code path runs at training time
# (here) and at inference time (hope-miner --model trained). Drift between
# the two is what causes "trained model performs differently in production"
# bugs, so we keep one implementation.
from hope.miner.models.feature_extraction import extract_features  # noqa: E402


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
    parser.add_argument("--save-model", type=str, default=None,
                        help="Save the trained model to this path "
                             "(loadable via `hope-miner --model trained --model-file <path>`)")
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

    # =====================================================
    # STEP 5: Save model (so the miner can use it)
    # =====================================================
    if args.save_model:
        try:
            import joblib
        except ImportError:
            print("\nERROR: --save-model requires joblib. Install with: pip install joblib")
            sys.exit(1)
        from pathlib import Path
        out = Path(args.save_model).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "version": 1,
            "feature_names": feature_names,
            "model_cost": model_cost,
            "model_conv": model_conv,
        }, out)
        print(f"\n  ✓ Saved model to {out}")
        print("    Use it with:")
        print(f"      hope-miner --model trained --model-file {out} ...")


if __name__ == "__main__":
    main()
