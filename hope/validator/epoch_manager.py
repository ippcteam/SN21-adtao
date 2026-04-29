"""Epoch Manager — state machine for the epoch lifecycle.

States:
  IDLE → PREPARING → COMMITTED → DISTRIBUTING → COLLECTING → SCORING → REVEALING → COMPLETE

Each state transition performs specific actions:
- PREPARING: fetch data from HOPE, compute commitments
- COMMITTED: publish commitment hash
- DISTRIBUTING: serve episodes to miners
- COLLECTING: accept predictions until deadline
- SCORING: score all predictions against outcomes
- REVEALING: publish outcomes + salt for verification
- COMPLETE: set weights, log summary
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from hope.constants import PREDICTION_DEADLINE_HOURS
from hope.protocol.episode import Episode
from hope.protocol.outcomes import Outcome
from hope.protocol.prediction import Prediction
from hope.scoring.scorer import EpochScorer, MinerScore
from hope.scoring.weights import ScoringWeights
from hope.validator.data_client import EpochData

logger = logging.getLogger(__name__)


class EpochState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    COMMITTED = "committed"
    DISTRIBUTING = "distributing"
    COLLECTING = "collecting"
    SCORING = "scoring"
    REVEALING = "revealing"
    COMPLETE = "complete"


@dataclass
class EpochContext:
    """All data for a single epoch."""

    epoch_id: str
    state: EpochState = EpochState.IDLE
    episodes: list[Episode] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    predictions: dict[str, list[Prediction]] = field(default_factory=dict)
    scores: dict[str, MinerScore] = field(default_factory=dict)

    # Commitment
    salt: str = ""
    commitment_hash: str = ""
    merkle_root: str = ""
    committed_at: Optional[str] = None

    # Timing
    started_at: Optional[str] = None
    deadline: Optional[str] = None
    scored_at: Optional[str] = None
    revealed_at: Optional[str] = None

    # Scoring config
    weights: ScoringWeights = field(default_factory=ScoringWeights)


class EpochManager:
    """Manages the lifecycle of epochs."""

    def __init__(self):
        self.current: Optional[EpochContext] = None
        self.history: list[EpochContext] = []
        self.scorer = EpochScorer()

    def get_validator_state(self) -> "LiveState":
        """Get a live state proxy for the FastAPI app.

        Returns an object that always reads from self.current,
        so scoring/reveal updates are visible immediately.
        """
        return LiveState(self)

    # -- State transitions --

    def prepare(self, epoch_data: EpochData) -> EpochContext:
        """IDLE → PREPARING: Load epoch data and compute commitments."""
        now = datetime.now(timezone.utc)

        ctx = EpochContext(
            epoch_id=epoch_data.release_key,
            state=EpochState.PREPARING,
            episodes=epoch_data.episodes,
            outcomes=epoch_data.outcomes,
            started_at=now.isoformat(),
            deadline=(now + timedelta(hours=PREDICTION_DEADLINE_HOURS)).isoformat(),
        )

        # Generate commitment
        ctx.salt = secrets.token_hex(32)
        ctx.commitment_hash = self._compute_commitment(ctx)
        ctx.merkle_root = self._compute_merkle_root(ctx)
        ctx.committed_at = now.isoformat()

        ctx.state = EpochState.COMMITTED
        self.current = ctx

        logger.info(
            f"Epoch {ctx.epoch_id} prepared: {len(ctx.episodes)} episodes, "
            f"commitment={ctx.commitment_hash[:16]}..., "
            f"deadline={ctx.deadline}"
        )

        return ctx

    def start_distribution(self) -> None:
        """COMMITTED → DISTRIBUTING: Begin serving episodes to miners."""
        if not self.current or self.current.state != EpochState.COMMITTED:
            raise ValueError("Cannot start distribution — not in COMMITTED state")

        self.current.state = EpochState.DISTRIBUTING
        logger.info(f"Epoch {self.current.epoch_id} distributing")

        # Immediately move to collecting (miners can start fetching)
        self.current.state = EpochState.COLLECTING
        logger.info(f"Epoch {self.current.epoch_id} collecting predictions until {self.current.deadline}")

    def score(self) -> dict[str, MinerScore]:
        """COLLECTING → SCORING: Score all miner predictions."""
        if not self.current:
            raise ValueError("No active epoch")

        self.current.state = EpochState.SCORING
        logger.info(
            f"Scoring epoch {self.current.epoch_id}: "
            f"{len(self.current.predictions)} miners submitted"
        )

        # Run the scoring pipeline
        self.current.scores = self.scorer.score_epoch(
            all_predictions=self.current.predictions,
            episodes=self.current.episodes,
            outcomes=self.current.outcomes,
        )

        self.current.scored_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"Scoring complete: {len(self.current.scores)} miners scored. "
            f"Top score: {max((s.final_score for s in self.current.scores.values()), default=0):.4f}"
        )

        return self.current.scores

    def reveal(self) -> dict:
        """SCORING → REVEALING: Publish outcomes for verification."""
        if not self.current or self.current.state != EpochState.SCORING:
            raise ValueError("Cannot reveal — not in SCORING state")

        self.current.state = EpochState.REVEALING
        self.current.revealed_at = datetime.now(timezone.utc).isoformat()

        reveal_data = {
            "epoch_id": self.current.epoch_id,
            "commitment_hash": self.current.commitment_hash,
            "salt": self.current.salt,
            "scoring_weights": self.current.weights.to_commitment_json(),
            "outcome_count": len(self.current.outcomes),
        }

        logger.info(f"Epoch {self.current.epoch_id} revealed")
        return reveal_data

    def complete(self) -> None:
        """REVEALING → COMPLETE: Finalize epoch."""
        if not self.current:
            raise ValueError("No active epoch")

        self.current.state = EpochState.COMPLETE
        self.history.append(self.current)
        logger.info(f"Epoch {self.current.epoch_id} complete")

    # -- Commitment helpers --

    def _compute_commitment(self, ctx: EpochContext) -> str:
        """Compute SHA256 commitment hash over outcomes + salt + weights."""
        outcomes_json = json.dumps(
            [o.model_dump(mode="json") for o in ctx.outcomes],
            sort_keys=True, default=str,
        )
        weights_json = ctx.weights.to_commitment_json()
        payload = f"{outcomes_json}{ctx.salt}{weights_json}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def _compute_merkle_root(self, ctx: EpochContext) -> str:
        """Compute a simple Merkle root over individual outcome hashes."""
        if not ctx.outcomes:
            return hashlib.sha256(b"empty").hexdigest()

        leaves = []
        for outcome in ctx.outcomes:
            leaf = hashlib.sha256(
                json.dumps(outcome.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest()
            leaves.append(leaf)

        # Simple iterative Merkle tree
        while len(leaves) > 1:
            if len(leaves) % 2 == 1:
                leaves.append(leaves[-1])  # Duplicate last if odd
            next_level = []
            for i in range(0, len(leaves), 2):
                combined = leaves[i] + leaves[i + 1]
                next_level.append(hashlib.sha256(combined.encode()).hexdigest())
            leaves = next_level

        return leaves[0]

    def verify_commitment(self, revealed_outcomes_json: str, salt: str, weights_json: str) -> bool:
        """Verify that revealed data matches the commitment hash."""
        if not self.current:
            return False
        payload = f"{revealed_outcomes_json}{salt}{weights_json}"
        computed = hashlib.sha256(payload.encode()).hexdigest()
        return computed == self.current.commitment_hash


class LiveState(dict):
    """Dict-like proxy that reads from EpochManager.current on every access.

    This ensures the FastAPI app always sees the latest epoch state,
    including post-scoring results and revealed outcomes.
    """

    def __init__(self, manager: EpochManager):
        super().__init__()
        self._mgr = manager

    def get(self, key, default=None):
        ctx = self._mgr.current
        if not ctx:
            return default

        if key == "current_epoch_id":
            return ctx.epoch_id
        if key == "episodes":
            return ctx.episodes
        if key == "predictions":
            return ctx.predictions
        if key == "outcomes":
            return ctx.outcomes
        if key == "deadline":
            return ctx.deadline
        if key == "registered_miners":
            return set()
        if key == "commitment":
            if not ctx.commitment_hash:
                return default
            return {
                "hash": ctx.commitment_hash,
                "merkle_root": ctx.merkle_root,
                "committed_at": ctx.committed_at,
                "episode_count": len(ctx.episodes),
            }
        if key == "reveal":
            if not ctx.revealed_at:
                return default
            return {
                "commitment_hash": ctx.commitment_hash,
                "salt": ctx.salt,
                "scoring_weights": ctx.weights.to_commitment_json(),
                "outcomes": [o.model_dump(mode="json") for o in ctx.outcomes],
                "scores": {
                    mid: {
                        "raw_score": s.raw_score,
                        "skill_score": s.skill_score,
                        "final_score": s.final_score,
                    }
                    for mid, s in ctx.scores.items()
                },
            }
        if key == "miner_scores":
            return ctx.scores if ctx.scores else default
        return default

    def __getitem__(self, key):
        result = self.get(key)
        if result is None and key not in self:
            raise KeyError(key)
        return result

    def __contains__(self, key):
        return key in ("current_epoch_id", "episodes", "predictions", "deadline",
                       "commitment", "reveal", "miner_scores", "registered_miners")

    def __setitem__(self, key, value):
        ctx = self._mgr.current
        if ctx and key == "predictions":
            ctx.predictions = value
