"""Episode Client — HTTP client for fetching episodes from the validator.

Supports signed requests when a wallet is available.
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

    def _headers(self) -> dict[str, str]:
        """Build auth headers with optional signature."""
        headers = {"X-Miner-Hotkey": self.hotkey}

        if self.wallet:
            try:
                nonce = str(time.time())
                message = hashlib.sha256(f"{self.hotkey}:{nonce}".encode()).hexdigest()
                signature = self.wallet.hotkey.sign(message.encode()).hex()
                headers["X-Miner-Nonce"] = nonce
                headers["X-Miner-Signature"] = signature
            except Exception as e:
                logger.warning(f"Failed to sign request: {e}")

        return headers

    async def fetch_episode_list(self, api_endpoint: str, epoch_id: str) -> list[dict]:
        """Get the list of episode metadata for an epoch."""
        url = f"{api_endpoint}/epochs/{epoch_id}/episodes"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json().get("episodes", [])

    async def fetch_episode(self, api_endpoint: str, epoch_id: str, episode_id: str) -> Episode:
        """Fetch a single episode's full payload."""
        url = f"{api_endpoint}/epochs/{epoch_id}/episodes/{episode_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            payload = resp.json()["payload"]
            return Episode.model_validate(payload)

    async def fetch_all_episodes(self, api_endpoint: str, epoch_id: str) -> list[Episode]:
        """Fetch all episodes in one batch request."""
        url = f"{api_endpoint}/epochs/{epoch_id}/episodes_batch"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            return [Episode.model_validate(ep["payload"]) for ep in data["episodes"]]
