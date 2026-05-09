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

    @staticmethod
    def _translate_validator_error(resp: "httpx.Response", path: str) -> str:
        """Turn a non-2xx validator response into an actionable error message.

        Generic httpx tracebacks aren't actionable for new miners hitting
        their first 401/403/404 — translate the common ones into specific
        next-steps the operator's quickstart can point at.
        """
        body_text = resp.text or ""
        try:
            body_text = resp.json().get("detail", body_text)
        except Exception:
            pass
        sc = resp.status_code
        if sc == 401:
            return (
                "validator returned 401 Unauthorized. The miner signature "
                "did not validate. Likely cause: --wallet-name / --wallet-hotkey "
                "don't point at the correct files, or the wallet's hotkey is the "
                "wrong type. Check `btcli wallet list --wallet.name <name>`."
            )
        if sc == 403:
            return (
                f"validator returned 403 Forbidden ({body_text}). Likely cause: "
                "your hotkey is not registered on the subnet. Run "
                "`btcli subnet register --netuid <id> --wallet.name <name> "
                "--wallet.hotkey <hk> --subtensor.network <test|finney>` first, "
                "then retry."
            )
        if sc == 404:
            return (
                f"validator returned 404 Not Found ({body_text}). Likely cause: "
                "the --epoch you passed doesn't match the validator's current "
                "epoch. Check `curl <validator-url>/health` for the live "
                "current_epoch."
            )
        if sc == 422:
            return (
                f"validator returned 422 ({body_text}). A required header is "
                "missing or malformed. Are you using the `hope-miner` CLI "
                "(which signs automatically) or a custom client?"
            )
        return f"validator returned HTTP {sc} for {path}: {body_text}"

    async def fetch_episode_list(self, api_endpoint: str, epoch_id: str) -> list[dict]:
        """Get the list of episode metadata for an epoch."""
        path = f"/v1/epochs/{epoch_id}/episodes"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            if resp.status_code >= 400:
                raise RuntimeError(self._translate_validator_error(resp, path))
            return resp.json().get("episodes", [])

    async def fetch_episode(self, api_endpoint: str, epoch_id: str, episode_id: str) -> Episode:
        """Fetch a single episode's full payload."""
        path = f"/v1/epochs/{epoch_id}/episodes/{episode_id}"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            if resp.status_code >= 400:
                raise RuntimeError(self._translate_validator_error(resp, path))
            payload = resp.json()["payload"]
            return Episode.model_validate(payload)

    async def fetch_all_episodes(self, api_endpoint: str, epoch_id: str) -> list[Episode]:
        """Fetch all episodes in one batch request."""
        path = f"/v1/epochs/{epoch_id}/episodes_batch"
        url = f"{api_endpoint}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._sign_request("GET", path))
            if resp.status_code >= 400:
                raise RuntimeError(self._translate_validator_error(resp, path))
            data = resp.json()
            return [Episode.model_validate(ep["payload"]) for ep in data["episodes"]]
