"""HOPE Data Client — fetches weekly challenge packages from the HOPE data API.

This is the bridge between HOPE's internal data pipeline and the validator.
The validator calls this to pull episodes, outcomes, and integrity metadata
for each epoch.

Phase 1: Our validator calls this directly.
Phase 2+: Could be used by any authorized validator partner.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from hope.constants import HOPE_API_BASE_URL, HOPE_API_VERSION
from hope.protocol.episode import Episode
from hope.protocol.outcomes import HorizonOutcome, Outcome, ScoringMetadata

logger = logging.getLogger(__name__)


@dataclass
class EpochData:
    """Complete data for one epoch, fetched from HOPE."""

    release_key: str
    schema_version: str
    episode_count: int
    episodes: list[Episode]
    outcomes: list[Outcome]
    package_hash: str
    trust_enriched_count: int = 0
    baseline_count: int = 0
    raw_package: Optional[dict] = None


class HopeDataClient:
    """Fetch weekly challenge packages from HOPE's internal data API.

    Usage:
        client = HopeDataClient(api_key="hope-bt-internal-2026")
        data = await client.fetch_epoch_data("WR-2026-W18-PUB-E1")
        print(f"Fetched {len(data.episodes)} episodes")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = HOPE_API_BASE_URL,
        api_version: str = HOPE_API_VERSION,
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}/internal/bittensor/{self.api_version}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    async def list_releases(self) -> list[dict]:
        """List available releases from HOPE."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self._url("/releases"), headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            return data.get("releases", [])

    async def fetch_release_metadata(self, release_key: str) -> dict:
        """Fetch metadata for a specific release."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                self._url(f"/releases/{release_key}"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_epoch_data(self, release_key: str) -> EpochData:
        """Fetch the full challenge package for a release.

        Returns parsed episodes, outcomes, and integrity metadata.
        This is the main method validators call at the start of each epoch.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            logger.info(f"Fetching package for {release_key} from {self.base_url}")
            resp = await client.get(
                self._url(f"/releases/{release_key}/package"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            package = resp.json()

        schema_version = package.get("schema_version", "v0.1")
        integrity = package.get("integrity", {})
        raw_episodes = package.get("episodes", [])

        episodes: list[Episode] = []
        outcomes: list[Outcome] = []

        for ep_data in raw_episodes:
            episode_id = ep_data.get("episode_id", "")
            payload = ep_data.get("payload")

            if not payload:
                continue

            # Parse episode from payload
            episode = self._parse_episode(payload)
            episodes.append(episode)

            # Parse validator-only outcomes
            voo = ep_data.get("validator_only_outcomes")
            outcome = self._parse_outcome(episode_id, voo, payload)
            outcomes.append(outcome)

        epoch_data = EpochData(
            release_key=release_key,
            schema_version=schema_version,
            episode_count=len(episodes),
            episodes=episodes,
            outcomes=outcomes,
            package_hash=integrity.get("package_hash", ""),
            trust_enriched_count=integrity.get("trust_enriched_count", 0),
            baseline_count=integrity.get("baseline_count", 0),
            raw_package=package,
        )

        logger.info(
            f"Parsed {release_key}: {len(episodes)} episodes, "
            f"{sum(1 for o in outcomes if o.t7 is not None)} with t7 outcomes, "
            f"{sum(1 for o in outcomes if o.t14 is not None)} with t14 outcomes"
        )

        return epoch_data

    async def fetch_governance_summary(self) -> dict:
        """Fetch governance summary (public, no auth required)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self._url("/governance/summary"))
            resp.raise_for_status()
            return resp.json()

    def verify_package_hash(self, epoch_data: EpochData) -> bool:
        """Verify that the package hash matches the episode content."""
        if not epoch_data.raw_package or not epoch_data.package_hash:
            return False

        import json
        episodes = epoch_data.raw_package.get("episodes", [])
        hash_input = json.dumps(episodes, sort_keys=True, default=str)
        computed = hashlib.sha256(hash_input.encode()).hexdigest()

        match = computed == epoch_data.package_hash
        if not match:
            logger.warning(
                f"Package hash mismatch: computed={computed[:16]}... "
                f"expected={epoch_data.package_hash[:16]}..."
            )
        return match

    def _parse_episode(self, payload: dict) -> Episode:
        """Parse a raw payload dict into an Episode model."""
        return Episode.model_validate(payload)

    def _parse_outcome(self, episode_id: str, voo: dict | None, payload: dict) -> Outcome:
        """Parse validator-only outcomes into an Outcome model."""
        t7 = None
        t14 = None

        if voo and voo.get("outcomes"):
            raw_outcomes = voo["outcomes"]

            if "t7" in raw_outcomes and raw_outcomes["t7"]:
                t7_data = raw_outcomes["t7"]
                t7 = HorizonOutcome(
                    cost_delta_pct=t7_data.get("cost_delta_pct", 0.0) or 0.0,
                    conversions_delta_pct=t7_data.get("conversions_delta_pct", 0.0) or 0.0,
                    efficiency_delta_pct=t7_data.get("cpa_delta_pct", 0.0) or 0.0,
                    goal_miss=0,
                )

            if "t14" in raw_outcomes and raw_outcomes["t14"]:
                t14_data = raw_outcomes["t14"]
                t14 = HorizonOutcome(
                    cost_delta_pct=t14_data.get("cost_delta_pct", 0.0) or 0.0,
                    conversions_delta_pct=t14_data.get("conversions_delta_pct", 0.0) or 0.0,
                    efficiency_delta_pct=t14_data.get("cpa_delta_pct", 0.0) or 0.0,
                    goal_miss=0,
                )

        # Determine scoring metadata from episode payload
        em = payload.get("episode_metadata", {})
        goal = payload.get("account_state", {}).get("goal", {})

        return Outcome(
            episode_id=episode_id,
            t7=t7,
            t14=t14,
            scoring_metadata=ScoringMetadata(
                goal_metric=goal.get("type", "CPA"),
                measurement_resolution=em.get("measurement_resolution", "high"),
                coverage_status=em.get("coverage_status", "baseline"),
                baseline_type="predict_zero",
            ),
        )
