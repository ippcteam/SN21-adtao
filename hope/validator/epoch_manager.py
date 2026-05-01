"""Epoch Manager — state machine for the epoch lifecycle.

States:
  IDLE → PREPARING → DISTRIBUTING → COLLECTING → CLOSED → SCORING → REVEALING → COMPLETE

Key security property:
  Outcomes are NOT fetched until AFTER the submission deadline closes.
  This prevents validators from seeing answers before miners submit.

Phase separation:
  1. PREPARING: fetch episodes ONLY (no outcomes) from HOPE API
  2. DISTRIBUTING → COLLECTING: miners submit predictions
  3. CLOSED: submission deadline passed, no more predictions accepted
  4. SCORING: NOW fetch outcomes from HOPE API, score predictions
  5. REVEALING: publish outcomes for verification
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

from hope.constants import (
    PREDICTION_DEADLINE_HOURS,
    MINING_CLOSE_HOUR_UTC,
)
from hope.protocol.episode import Episode
from hope.protocol.outcomes import Outcome
from hope.protocol.prediction import Prediction
from hope.scoring.scorer import EpochScorer, MinerScore
from hope.scoring.weights import ScoringWeights
from hope.validator.integrity import (
    EpisodeCommitment,
    PredictionMerkleTree,
    compute_episode_commitment,
    build_prediction_merkle_tree,
    compute_scoring_hash,
)

logger = logging.getLogger(__name__)


class EpochState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    DISTRIBUTING = "distributing"
    COLLECTING = "collecting"
    CLOSED = "closed"          # Submissions closed, outcomes not yet fetched
    SCORING = "scoring"
    REVEALING = "revealing"
    COMPLETE = "complete"


@dataclass
class EpochContext:
    """All data for a single epoch."""

    epoch_id: str
    state: EpochState = EpochState.IDLE
    episodes: list[Episode] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)  # Empty until SCORING phase
    predictions: dict[str, dict[str, Prediction]] = field(default_factory=dict)
    prediction_receipts: dict[str, dict] = field(default_factory=dict)  # hotkey -> {episode_id -> receipt}
    scores: dict[str, MinerScore] = field(default_factory=dict)

    # Integrity proofs
    episode_commitment: Optional[EpisodeCommitment] = None  # Published before distribution
    prediction_merkle: Optional[PredictionMerkleTree] = None  # Published before scoring
    scoring_hash: str = ""  # Deterministic hash of scoring output

    # Legacy commitment (for verification endpoint)
    salt: str = ""
    commitment_hash: str = ""
    merkle_root: str = ""
    committed_at: Optional[str] = None

    # Timing
    started_at: Optional[str] = None
    deadline: Optional[str] = None
    closed_at: Optional[str] = None       # When submissions were closed
    outcomes_fetched_at: Optional[str] = None  # When outcomes were fetched (AFTER deadline)
    scored_at: Optional[str] = None
    revealed_at: Optional[str] = None

    # Scoring config
    weights: ScoringWeights = field(default_factory=ScoringWeights)


class EpochManager:
    """Manages the lifecycle of epochs with phase separation."""

    def __init__(self):
        self.current: Optional[EpochContext] = None
        self.history: list[EpochContext] = []
        self.scorer = EpochScorer()
        self.registered_miners: set[str] = set()  # Populated from metagraph

    def get_validator_state(self) -> "LiveState":
        """Get a live state proxy for the FastAPI app."""
        return LiveState(self)

    def is_submission_open(self) -> bool:
        """Check if the submission window is currently open."""
        if not self.current:
            return False
        if self.current.state not in (EpochState.COLLECTING, EpochState.DISTRIBUTING):
            return False
        if self.current.deadline:
            deadline = datetime.fromisoformat(self.current.deadline)
            if datetime.now(timezone.utc) > deadline:
                return False
        return True

    # -- State transitions --

    def _compute_deadline(self, now: datetime) -> datetime:
        """Compute the next mining close time based on the weekly cadence.

        Mining closes: Sunday midnight EST = Monday 05:00 UTC
        If we're past this week's close, target next week's.
        """
        # Find next Monday 05:00 UTC
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour >= MINING_CLOSE_HOUR_UTC:
            days_until_monday = 7  # Already past this Monday's close
        next_close = now.replace(
            hour=MINING_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_monday)
        return next_close

    def prepare_episodes_only(self, episodes: list[Episode], release_key: str,
                               deadline_hours: float = PREDICTION_DEADLINE_HOURS) -> EpochContext:
        """IDLE → PREPARING: Load episodes WITHOUT outcomes.

        Outcomes are intentionally NOT loaded here. They will be fetched
        only after the submission deadline closes. This prevents the
        validator from seeing answers before miners submit.

        Cadence:
          Mining:    Monday noon EST (17:00 UTC) → Sunday midnight EST (Mon 05:00 UTC)
          Scoring:   Monday 05:00 UTC → Monday 17:00 UTC
        """
        now = datetime.now(timezone.utc)

        # Use the cadence-based deadline, or fallback to hours-based for testing
        if deadline_hours == PREDICTION_DEADLINE_HOURS:
            deadline = self._compute_deadline(now)
        else:
            deadline = now + timedelta(hours=deadline_hours)

        ctx = EpochContext(
            epoch_id=release_key,
            state=EpochState.PREPARING,
            episodes=episodes,
            outcomes=[],  # Deliberately empty — fetched after deadline
            started_at=now.isoformat(),
            deadline=deadline.isoformat(),
        )

        # A6: Compute episode commitment BEFORE distributing to miners
        ctx.episode_commitment = compute_episode_commitment(
            episodes=ctx.episodes,
            epoch_id=ctx.epoch_id,
            timestamp=now.isoformat(),
        )

        ctx.state = EpochState.DISTRIBUTING
        self.current = ctx

        logger.info(
            f"Epoch {ctx.epoch_id} prepared: {len(ctx.episodes)} episodes, "
            f"outcomes=DEFERRED (fetched after deadline), "
            f"deadline={ctx.deadline}, "
            f"episode_hash={ctx.episode_commitment.episode_hash[:16]}..."
        )

        return ctx

    def start_collecting(self) -> None:
        """DISTRIBUTING → COLLECTING: Accept predictions from miners."""
        if not self.current:
            raise ValueError("No active epoch")
        self.current.state = EpochState.COLLECTING
        logger.info(f"Epoch {self.current.epoch_id} collecting predictions until {self.current.deadline}")

    def close_submissions(self) -> None:
        """COLLECTING → CLOSED: No more predictions accepted."""
        if not self.current:
            raise ValueError("No active epoch")
        self.current.state = EpochState.CLOSED
        self.current.closed_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Epoch {self.current.epoch_id} submissions CLOSED. "
            f"{len(self.current.predictions)} miners submitted."
        )

    def load_outcomes_and_score(self, outcomes: list[Outcome]) -> dict[str, MinerScore]:
        """CLOSED → SCORING: Load outcomes (NOW) and score predictions.

        This is the critical phase separation: outcomes are only loaded
        AFTER submissions are closed. The validator provably did not have
        the answers while miners were predicting.
        """
        if not self.current:
            raise ValueError("No active epoch")
        if self.current.state != EpochState.CLOSED:
            raise ValueError(f"Cannot score — submissions not closed (state={self.current.state})")

        # A4: Build prediction Merkle tree BEFORE fetching outcomes
        # This proves predictions were locked before validator saw answers
        self.current.prediction_merkle = build_prediction_merkle_tree(
            self.current.predictions,
        )
        logger.info(
            f"Prediction Merkle tree built: {self.current.prediction_merkle.leaf_count} leaves, "
            f"root={self.current.prediction_merkle.root[:16]}..."
        )

        # Load outcomes NOW (after predictions are locked)
        self.current.outcomes = outcomes
        self.current.outcomes_fetched_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Outcomes loaded AFTER deadline: {len(outcomes)} outcomes, "
            f"fetched at {self.current.outcomes_fetched_at}"
        )

        # Compute commitment (over outcomes + predictions Merkle root)
        self.current.salt = secrets.token_hex(32)
        self.current.commitment_hash = self._compute_commitment(self.current)
        self.current.merkle_root = self._compute_merkle_root(self.current)
        self.current.committed_at = datetime.now(timezone.utc).isoformat()

        # Score
        self.current.state = EpochState.SCORING
        logger.info(
            f"Scoring epoch {self.current.epoch_id}: "
            f"{len(self.current.predictions)} miners submitted"
        )

        self.current.scores = self.scorer.score_epoch(
            all_predictions=self.current.predictions,
            episodes=self.current.episodes,
            outcomes=self.current.outcomes,
        )

        self.current.scored_at = datetime.now(timezone.utc).isoformat()
        self.current.scoring_hash = compute_scoring_hash(self.current.scores)

        logger.info(
            f"Scoring complete: {len(self.current.scores)} miners scored. "
            f"Top score: {max((s.final_score for s in self.current.scores.values()), default=0):.4f}, "
            f"scoring_hash={self.current.scoring_hash[:16]}..."
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
            "outcomes_fetched_at": self.current.outcomes_fetched_at,
            "submissions_closed_at": self.current.closed_at,
            # Integrity proofs
            "episode_hash": self.current.episode_commitment.episode_hash if self.current.episode_commitment else None,
            "prediction_merkle_root": self.current.prediction_merkle.root if self.current.prediction_merkle else None,
            "prediction_count": self.current.prediction_merkle.leaf_count if self.current.prediction_merkle else 0,
            "scoring_hash": self.current.scoring_hash,
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
        """Compute SHA256 commitment hash over all integrity data.

        Includes: episode_hash + prediction_merkle_root + outcomes + salt + weights
        This binds all components together — any tampering invalidates the commitment.
        """
        episode_hash = ctx.episode_commitment.episode_hash if ctx.episode_commitment else ""
        pred_root = ctx.prediction_merkle.root if ctx.prediction_merkle else ""
        outcomes_json = json.dumps(
            [o.model_dump(mode="json") for o in ctx.outcomes],
            sort_keys=True, default=str,
        )
        weights_json = ctx.weights.to_commitment_json()
        payload = f"{episode_hash}{pred_root}{outcomes_json}{ctx.salt}{weights_json}"
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

        while len(leaves) > 1:
            if len(leaves) % 2 == 1:
                leaves.append(leaves[-1])
            next_level = []
            for i in range(0, len(leaves), 2):
                combined = leaves[i] + leaves[i + 1]
                next_level.append(hashlib.sha256(combined.encode()).hexdigest())
            leaves = next_level

        return leaves[0]

    def verify_commitment(
        self,
        episode_hash: str,
        prediction_merkle_root: str,
        revealed_outcomes_json: str,
        salt: str,
        weights_json: str,
    ) -> bool:
        """Verify that revealed data matches the commitment hash.

        Must match _compute_commitment(): SHA256(episode_hash + pred_root + outcomes + salt + weights)
        """
        if not self.current:
            return False
        payload = f"{episode_hash}{prediction_merkle_root}{revealed_outcomes_json}{salt}{weights_json}"
        computed = hashlib.sha256(payload.encode()).hexdigest()
        return computed == self.current.commitment_hash


class LiveState(dict):
    """Dict-like proxy that reads from EpochManager.current on every access."""

    def __init__(self, manager: EpochManager):
        super().__init__()
        self._mgr = manager

    def __getitem__(self, key):
        result = self.get(key)
        if result is None and key not in self:
            raise KeyError(key)
        return result

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
        if key == "submission_open":
            return self._mgr.is_submission_open()
        if key == "registered_miners":
            return self._mgr.registered_miners
        if key == "episode_commitment":
            if not ctx.episode_commitment:
                return default
            return {
                "episode_hash": ctx.episode_commitment.episode_hash,
                "episode_count": ctx.episode_commitment.episode_count,
                "committed_at": ctx.episode_commitment.committed_at,
            }
        if key == "commitment":
            if not ctx.commitment_hash:
                return default
            return {
                "hash": ctx.commitment_hash,
                "merkle_root": ctx.merkle_root,
                "committed_at": ctx.committed_at,
                "episode_count": len(ctx.episodes),
                "episode_hash": ctx.episode_commitment.episode_hash if ctx.episode_commitment else None,
                "prediction_merkle_root": ctx.prediction_merkle.root if ctx.prediction_merkle else None,
                "prediction_count": ctx.prediction_merkle.leaf_count if ctx.prediction_merkle else 0,
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
                "outcomes_fetched_at": ctx.outcomes_fetched_at,
                "submissions_closed_at": ctx.closed_at,
            }
        if key == "miner_scores":
            return ctx.scores if ctx.scores else default
        if key == "prediction_receipts":
            return ctx.prediction_receipts
        return default

    def __contains__(self, key):
        return key in ("current_epoch_id", "episodes", "predictions", "outcomes",
                       "deadline", "submission_open", "commitment", "reveal",
                       "miner_scores", "registered_miners", "episode_commitment",
                       "uid_map", "prediction_receipts")

    def __setitem__(self, key, value):
        ctx = self._mgr.current
        if ctx and key == "predictions":
            ctx.predictions = value
