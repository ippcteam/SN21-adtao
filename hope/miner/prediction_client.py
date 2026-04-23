"""Prediction Client — HTTP client for submitting predictions to the validator."""

from __future__ import annotations

import logging

import httpx

from hope.protocol.prediction import Prediction

logger = logging.getLogger(__name__)


class PredictionClient:
    """Submit predictions to the validator's HTTP API."""

    def __init__(self, hotkey: str, timeout: float = 60.0):
        self.hotkey = hotkey
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-Miner-Hotkey": self.hotkey}

    async def submit_predictions(
        self, api_endpoint: str, epoch_id: str, predictions: list[Prediction]
    ) -> dict:
        """Submit a batch of predictions for an epoch."""
        url = f"{api_endpoint}/epochs/{epoch_id}/predictions"

        payload = {
            "predictions": [
                {
                    "episode_id": p.episode_id,
                    "horizons": {
                        h_key: {
                            "cost_delta_pct": h.cost_delta_pct.model_dump(),
                            "conversions_delta_pct": h.conversions_delta_pct.model_dump(),
                            "efficiency_delta_pct": h.efficiency_delta_pct.model_dump(),
                            "goal_miss_probability": h.goal_miss_probability,
                            "instability_risk": h.instability_risk,
                        }
                        for h_key, h in p.horizons.items()
                    },
                }
                for p in predictions
            ]
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            result = resp.json()

        logger.info(
            f"Submitted {len(predictions)} predictions: "
            f"{result.get('accepted', 0)} accepted, {result.get('rejected', 0)} rejected"
        )
        return result
