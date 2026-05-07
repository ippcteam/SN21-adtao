"""Prediction Client — HTTP client for submitting signed predictions to the validator.

Miners must sign every request. The signature covers the full request:
  sign(SHA256(hotkey:nonce:METHOD:path:body_hash))
This prevents replay attacks and ensures the validator cannot tamper with submissions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx

from hope.protocol.prediction import Prediction

logger = logging.getLogger(__name__)


class PredictionClient:
    """Submit signed predictions to the validator's HTTP API."""

    def __init__(self, hotkey: str, wallet=None, timeout: float = 60.0):
        self.hotkey = hotkey
        self.wallet = wallet  # Bittensor wallet for signing
        self.timeout = timeout

    def _sign_request(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> dict[str, str]:
        """Build auth headers with request-bound signature.

        Signature covers: SHA256(hotkey:nonce:METHOD:path:body_hash)
        """
        headers = {"X-Miner-Hotkey": self.hotkey}

        if self.wallet:
            try:
                nonce = str(time.time())
                body_hash = hashlib.sha256(body).hexdigest()
                message = hashlib.sha256(
                    f"{self.hotkey}:{nonce}:{method}:{path}:{body_hash}".encode()
                ).hexdigest()
                signature = self.wallet.hotkey.sign(message.encode()).hex()
                headers["X-Miner-Nonce"] = nonce
                headers["X-Miner-Signature"] = signature
            except Exception as e:
                logger.warning(f"Failed to sign request: {e}")

        return headers

    async def submit_predictions(
        self, api_endpoint: str, epoch_id: str, predictions: list[Prediction]
    ) -> dict:
        """Submit a batch of signed predictions for an epoch."""
        path = f"/v1/epochs/{epoch_id}/predictions"
        url = f"{api_endpoint}{path}"

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

        body = json.dumps(payload, sort_keys=True).encode()
        headers = self._sign_request("POST", path, body)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, content=body, headers={
                **headers,
                "Content-Type": "application/json",
            })
            resp.raise_for_status()
            result = resp.json()

        logger.info(
            f"Submitted {len(predictions)} predictions: "
            f"{result.get('accepted', 0)} accepted, {result.get('rejected', 0)} rejected"
        )
        return result
