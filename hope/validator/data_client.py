"""the operator Data Client — fetches weekly challenge packages from the the operator data API.

This is the bridge between the operator's internal data pipeline and the validator.
The validator calls this to pull episodes, outcomes, and integrity metadata
for each epoch.

All responses from the operator are cryptographically signed with the operator's ed25519 key.
The validator verifies signatures before accepting any data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

import os

from hope.constants import HOPE_API_VERSION
from hope.hope_public_key import HOPE_PUBLIC_KEY_HEX
from hope.protocol.episode import Episode
from hope.protocol.outcomes import (
    ConditionalPriorBaseline,
    HorizonOutcome,
    Outcome,
    ScoringMetadata,
)

logger = logging.getLogger(__name__)


@dataclass
class EpochData:
    """Complete data for one epoch, fetched from the operator."""

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
    """Fetch weekly challenge packages from the operator's internal data API.

    Usage:
        client = HopeDataClient(api_key=os.environ["HOPE_API_KEY"])
        data = await client.fetch_epoch_data("RELEASE_KEY")
        print(f"Fetched {len(data.episodes)} episodes")
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        api_version: str = HOPE_API_VERSION,
        timeout: float = 120.0,
    ):
        self.api_key = api_key or os.environ.get("HOPE_API_KEY", "")
        self.base_url = (base_url or os.environ.get("HOPE_API_URL", "")).rstrip("/")
        self.api_version = api_version
        self.timeout = timeout

        if not self.api_key:
            raise ValueError("HOPE_API_KEY must be set (pass api_key or set HOPE_API_KEY env var)")
        if not self.base_url:
            raise ValueError("HOPE_API_URL must be set (pass base_url or set HOPE_API_URL env var)")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/internal/bittensor/{self.api_version}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    async def list_releases(self) -> list[dict]:
        """List available releases from the operator."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self._url("/releases"), headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            return data.get("releases", [])

    async def discover_latest_release(self) -> str:
        """Return the release_key of the most recently created release.

        Sorts the operator's `/releases` listing by `created_at` descending
        and returns the first record's `release_key`. Raises RuntimeError
        if no releases are available or the latest record has no
        `release_key` field — both indicate a misconfigured data backend
        rather than a transient error.

        Used by `hope-validator-api --release auto` and any other caller
        that wants to track the current weekly release without manual
        env-var updates. The cron-side equivalent in
        `deploy/validator_scoring/scoring_trigger.sh` implements the same
        algorithm in bash for environments where a Python import isn't
        cheap.
        """
        releases = await self.list_releases()
        if not releases:
            raise RuntimeError(
                "no releases returned from the operator data backend; "
                "cannot resolve auto-release. Verify HOPE_API_URL points "
                "at the live backend and HOPE_API_KEY is authorised."
            )
        releases.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        key = releases[0].get("release_key")
        if not key:
            raise RuntimeError(
                "latest release returned by the operator data backend has "
                "no `release_key` field; cannot resolve auto-release. "
                "Backend response shape may have changed."
            )
        return str(key)

    async def discover_scoreable_release(self) -> str:
        """Return the release_key of the latest CLOSED epoch to SCORE.

        The most-recently-created release is the OPEN submission epoch — the
        one the prediction server is currently serving and accepting bundles
        for. Scoring must target the epoch *before* it: the most-recently
        closed one, which has on-chain submissions to grade. This is the
        scorer's counterpart to `discover_latest_release()` (which returns the
        newest/open epoch and is correct for the prediction server, but wrong
        for scoring).

        Why not the newest: resolving the scorer to the newest release lands on
        the open epoch, whose submission window hasn't closed and which has no
        bundles yet — an empty scoring run. (Exactly the failure when an
        off-cadence rerun was the newest release.)

        Assumption: the newest release == the open submission epoch. Holds for
        the weekly cadence and for off-cadence reruns. If two epochs are
        briefly open at once (a rerun overlapping a fresh weekly build), pin
        `--release <EPOCH_ID>` explicitly. P3's in-process epoch state will make
        this deadline-exact (no created-at heuristic).

        Raises RuntimeError if fewer than two releases exist or the chosen
        record lacks a `release_key`.
        """
        releases = await self.list_releases()
        if not releases:
            raise RuntimeError(
                "no releases returned from the operator data backend; cannot "
                "resolve a scoreable release. Verify HOPE_API_URL / HOPE_API_KEY."
            )
        releases.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        if len(releases) < 2:
            raise RuntimeError(
                "only one release exists; cannot distinguish the open epoch "
                "from a closed one. Pass --release <EPOCH_ID> explicitly."
            )
        key = releases[1].get("release_key")
        if not key:
            raise RuntimeError(
                "the release preceding the newest has no `release_key` field; "
                "backend response shape may have changed."
            )
        return str(key)

    async def fetch_release_metadata(self, release_key: str) -> dict:
        """Fetch metadata for a specific release."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                self._url(f"/releases/{release_key}"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_episodes_only(self, release_key: str) -> EpochData:
        """Fetch episodes WITHOUT outcomes — for distribution phase.

        Calls data API without ?include_outcomes=true, so the server
        does not return outcome data. Double protection: even if
        outcomes leaked, we strip them client-side too.
        """
        # Request WITHOUT include_outcomes — server won't return outcomes
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            logger.info(f"Fetching episodes-only for {release_key} (no outcomes)")
            resp = await client.get(
                self._url(f"/releases/{release_key}/package"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            package = resp.json()

        # Verify the operator's cryptographic signature
        sig_valid = self.verify_hope_signature(package)
        require_sig = os.environ.get("REQUIRE_HOPE_SIGNATURE", "true").lower() != "false"
        if not sig_valid:
            if require_sig:
                raise ValueError(
                    "the operator signature verification failed for episodes. "
                    "Data may be tampered with — refusing to use unverified episodes."
                )
            logger.warning("the operator signature not verified for episodes — REQUIRE_HOPE_SIGNATURE=false allows this")

        episodes = []
        for ep_data in package.get("episodes", []):
            payload = ep_data.get("payload")
            if payload:
                episodes.append(self._parse_episode(payload))

        return EpochData(
            release_key=release_key,
            schema_version=package.get("schema_version", "v0.1"),
            episode_count=len(episodes),
            episodes=episodes,
            outcomes=[],  # Deliberately empty
            package_hash=package.get("integrity", {}).get("package_hash", ""),
            trust_enriched_count=0,
            baseline_count=len(episodes),
            raw_package=None,  # Don't store raw package
        )

    async def fetch_outcomes_only(self, release_key: str) -> list[Outcome]:
        """Fetch outcomes ONLY — for scoring phase (after deadline).

        Calls data API WITH ?include_outcomes=true. This should only
        be called AFTER the submission window closes.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            logger.info(f"Fetching outcomes for {release_key} (include_outcomes=true)")
            resp = await client.get(
                self._url(f"/releases/{release_key}/package?include_outcomes=true"),
                headers=self._headers(),
            )
            resp.raise_for_status()
            package = resp.json()

        # Verify the operator's cryptographic signature
        sig_valid = self.verify_hope_signature(package)
        require_sig = os.environ.get("REQUIRE_HOPE_SIGNATURE", "true").lower() != "false"
        if not sig_valid:
            if require_sig:
                raise ValueError(
                    "the operator signature verification failed for outcomes. "
                    "Data may be tampered with — refusing to use unverified outcomes."
                )
            logger.warning("the operator signature not verified for outcomes — REQUIRE_HOPE_SIGNATURE=false allows this")

        outcomes = []
        for ep_data in package.get("episodes", []):
            episode_id = ep_data.get("episode_id", "")
            payload = ep_data.get("payload", {})
            voo = ep_data.get("validator_only_outcomes")
            outcome = self._parse_outcome(episode_id, voo, payload)
            outcomes.append(outcome)

        with_t7 = sum(1 for o in outcomes if o.t7)
        logger.info(f"Fetched outcomes for {release_key}: {with_t7} with t7")
        return outcomes

    async def fetch_epoch_data(self, release_key: str) -> EpochData:
        """Fetch the full challenge package (episodes + outcomes).

        WARNING: For production use, prefer fetch_episodes_only() during
        distribution and fetch_outcomes_only() after deadline.
        This method is kept for backward compatibility and testing.
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

    def verify_hope_signature(self, package: dict) -> bool:
        """Verify the operator's ed25519 signature on an API response.

        Returns True if signature is valid against the operator's published public key.
        Returns False (with warning) if signature is missing or invalid.
        """
        integrity = package.get("integrity", {})
        signature_hex = integrity.get("signature")
        signing_key_hex = integrity.get("signing_key")

        if not signature_hex:
            logger.warning("the operator response has no signature — cannot verify authenticity")
            return False

        # Verify the signing key matches the operator's published key
        if signing_key_hex and signing_key_hex != HOPE_PUBLIC_KEY_HEX:
            logger.error(
                f"the operator signing key mismatch: got {signing_key_hex[:16]}... "
                f"expected {HOPE_PUBLIC_KEY_HEX[:16]}..."
            )
            return False

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            # Reconstruct the signed content (everything except integrity)
            signable = {k: v for k, v in package.items() if k != "integrity"}
            signable_json = json.dumps(signable, sort_keys=True, default=str)
            content_hash = hashlib.sha256(signable_json.encode()).digest()

            # Verify signature
            pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(HOPE_PUBLIC_KEY_HEX))
            pub_key.verify(bytes.fromhex(signature_hex), content_hash)

            logger.info("the operator signature verified successfully")
            return True

        except ImportError:
            logger.warning("cryptography library not installed — cannot verify the operator signature")
            return False
        except Exception as e:
            logger.error(f"the operator signature verification FAILED: {e}")
            return False

    def _parse_episode(self, payload: dict) -> Episode:
        """Parse a raw payload dict into an Episode model."""
        return Episode.model_validate(payload)

    def _parse_outcome(self, episode_id: str, voo: dict | None, payload: dict) -> Outcome:
        """Parse validator-only outcomes into an Outcome model."""
        t7 = None
        t14 = None

        # Determine goal metric to pick the right efficiency key
        goal = payload.get("account_state", {}).get("goal", {})
        goal_type = goal.get("type", "CPA").upper()
        # Map goal type to the outcome field for efficiency delta
        efficiency_key = "roas_delta_pct" if goal_type == "ROAS" else "cpa_delta_pct"

        if voo and voo.get("outcomes"):
            raw_outcomes = voo["outcomes"]

            if "t7" in raw_outcomes and raw_outcomes["t7"]:
                t7_data = raw_outcomes["t7"]
                t7 = HorizonOutcome(
                    cost_delta_pct=t7_data.get("cost_delta_pct", 0.0) or 0.0,
                    conversions_delta_pct=t7_data.get("conversions_delta_pct", 0.0) or 0.0,
                    efficiency_delta_pct=t7_data.get(efficiency_key, 0.0) or 0.0,
                    goal_miss=int(t7_data.get("goal_miss", 0) or 0),
                )

            if "t14" in raw_outcomes and raw_outcomes["t14"]:
                t14_data = raw_outcomes["t14"]
                t14 = HorizonOutcome(
                    cost_delta_pct=t14_data.get("cost_delta_pct", 0.0) or 0.0,
                    conversions_delta_pct=t14_data.get("conversions_delta_pct", 0.0) or 0.0,
                    efficiency_delta_pct=t14_data.get(efficiency_key, 0.0) or 0.0,
                    goal_miss=int(t14_data.get("goal_miss", 0) or 0),
                )

        em = payload.get("episode_metadata", {})

        # Conditional-prior baseline: optional. The release artifact may publish
        # per-episode (campaign_type, action_type) historical means under
        # "scoring_metadata.conditional_prior". When absent we fall back to
        # predict_zero so a missing prior does not zero the participation gate.
        sm_payload = payload.get("scoring_metadata", {})
        cp_payload = sm_payload.get("conditional_prior")
        baseline_type = sm_payload.get("baseline_type")
        conditional_prior: ConditionalPriorBaseline | None = None
        if cp_payload:
            conditional_prior = ConditionalPriorBaseline.model_validate(cp_payload)
            baseline_type = baseline_type or "conditional_prior"
        else:
            baseline_type = baseline_type or "predict_zero"

        return Outcome(
            episode_id=episode_id,
            t7=t7,
            t14=t14,
            scoring_metadata=ScoringMetadata(
                goal_metric=goal.get("type", "CPA"),
                measurement_resolution=em.get("measurement_resolution", "high"),
                coverage_status=em.get("coverage_status", "baseline"),
                baseline_type=baseline_type,
                conditional_prior=conditional_prior,
            ),
        )
