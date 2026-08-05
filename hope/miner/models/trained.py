"""TrainedXGBoostModel — load a model file produced by train_example_model.py.

A miner runs `python scripts/train_example_model.py --save-model my_model.pkl`,
then runs `hope-miner --model trained --model-file my_model.pkl ...` to use
the trained model in production. The same `extract_features` call is used at
both training and inference time (via `hope.miner.models.feature_extraction`)
so what the model learned at training is exactly what it sees at inference.

The serialized format is a joblib-pickled dict:

    {
      "version": 1,
      "feature_names": ["avg_daily_cost", ...],
      "model_cost": <fitted XGBRegressor>,
      "model_conv": <fitted XGBRegressor>,
    }
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from hope.miner.models.feature_extraction import extract_features
from hope.miner.prediction_engine import PredictionEngine
from hope.protocol.episode import Episode
from hope.protocol.prediction import (
    HorizonPrediction,
    Prediction,
    QuantilePrediction,
)

logger = logging.getLogger(__name__)


_MODEL_FORMAT_VERSION = 1


class TrainedXGBoostModel(PredictionEngine):
    """Inference wrapper around a trained-and-saved XGBoost model bundle."""

    name = "trained"

    def __init__(self, model_file: str | Path):
        try:
            import joblib
        except ImportError as e:
            raise RuntimeError(
                "TrainedXGBoostModel requires joblib + xgboost + scikit-learn. "
                "Install with: pip install xgboost scikit-learn joblib"
            ) from e

        import joblib

        path = Path(model_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"model file not found: {path}")
        bundle = joblib.load(path)

        version = bundle.get("version") if isinstance(bundle, dict) else None
        if version != _MODEL_FORMAT_VERSION:
            raise ValueError(
                f"unsupported model bundle version: {version!r} "
                f"(expected {_MODEL_FORMAT_VERSION}). Re-train with the "
                f"current version of scripts/train_example_model.py."
            )
        self.feature_names: list[str] = bundle["feature_names"]
        self.model_cost = bundle["model_cost"]
        self.model_conv = bundle["model_conv"]
        logger.info(
            "loaded TrainedXGBoostModel from %s (%d features)",
            path, len(self.feature_names),
        )

    def predict(self, episode: Episode) -> Prediction:
        ep_dict = episode.model_dump()
        feats = extract_features(ep_dict)
        x = np.array(
            [[feats.get(f, 0.0) for f in self.feature_names]], dtype=float
        )
        cost_p50 = float(self.model_cost.predict(x)[0])
        conv_p50 = float(self.model_conv.predict(x)[0])

        # Spread heuristic in fractional units — scales with prediction
        # magnitude, floored by the spec-defined MIN_INTERVAL_WIDTH so
        # narrow-around-zero predictions don't trip the null detector.
        # Miners should tune this — calibration is a major scoring component.
        from hope.constants import MIN_INTERVAL_WIDTH
        spread = max(MIN_INTERVAL_WIDTH, abs(cost_p50) * 0.3)
        eff_p50 = 0.0
        eff_spread = max(MIN_INTERVAL_WIDTH, spread / 2)

        h7 = HorizonPrediction(
            cost_delta_pct=QuantilePrediction(
                p10=cost_p50 - spread, p50=cost_p50, p90=cost_p50 + spread,
            ),
            conversions_delta_pct=QuantilePrediction(
                p10=conv_p50 - spread, p50=conv_p50, p90=conv_p50 + spread,
            ),
            efficiency_delta_pct=QuantilePrediction(
                p10=eff_p50 - eff_spread, p50=eff_p50, p90=eff_p50 + eff_spread,
            ),
            goal_miss_probability=0.30,
            instability_risk=0.15,
        )
        # 14-day: dampen point estimate, narrow spread (learning stabilises)
        h14 = HorizonPrediction(
            cost_delta_pct=QuantilePrediction(
                p10=cost_p50 * 0.85 - spread * 0.9,
                p50=cost_p50 * 0.85,
                p90=cost_p50 * 0.85 + spread * 0.9,
            ),
            conversions_delta_pct=QuantilePrediction(
                p10=conv_p50 * 0.85 - spread * 0.9,
                p50=conv_p50 * 0.85,
                p90=conv_p50 * 0.85 + spread * 0.9,
            ),
            efficiency_delta_pct=QuantilePrediction(
                p10=eff_p50 - eff_spread * 0.9,
                p50=eff_p50,
                p90=eff_p50 + eff_spread * 0.9,
            ),
            goal_miss_probability=0.25,
            instability_risk=0.10,
        )

        from datetime import datetime, timezone

        return Prediction(
            episode_id=episode.episode_metadata.episode_id,
            miner_id="trained_xgboost",
            submitted_at=datetime.now(timezone.utc),
            horizons={"7": h7, "14": h14},
        )
