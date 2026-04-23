"""Episode Client — HTTP client for fetching episodes from the validator.

All requests are signed with the miner's hotkey. The signature covers the
full request (method, path, body hash) to prevent replay attacks.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

from hope.protocol.episode import Episode

logger = logging.getLogger(__name__)


class EpisodeClient:
    """Fetch episodes from the validator's HTTP API."""

    def __init__(self, hotkey: str, wallet=None, timeout: float = 60.0):
        self.hotkey = hotkey
        self.wallet = wallet
        self.timeout = timeout

    def _sign_request(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Build auth headers with request-bound signature."""
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

    async def fetch_episode_list(self, api_endpoint: str, epoch_id: str) -> list[dict]:
        """Get the list of episode metadata for an epoch."""
        path = f"/v1/epochs/{epoch_id}/episodes"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            resp.raise_for_status()
            return resp.json().get("episodes", [])

    async def fetch_episode(self, api_endpoint: str, epoch_id: str, episode_id: str) -> Episode:
        """Fetch a single episode's full payload."""
        path = f"/v1/epochs/{epoch_id}/episodes/{episode_id}"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            resp.raise_for_status()
            payload = resp.json()["payload"]
            return Episode.model_validate(payload)

    async def fetch_all_episodes(self, api_endpoint: str, epoch_id: str) -> list[Episode]:
        """Fetch all episodes in one batch request."""
        path = f"/v1/epochs/{epoch_id}/episodes_batch"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            resp.raise_for_status()
            data = resp.json()
            return [Episode.model_validate(ep["payload"]) for ep in data["episodes"]]
