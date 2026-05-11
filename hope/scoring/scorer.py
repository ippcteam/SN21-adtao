"""Epoch Scorer — top-level orchestrator for scoring a complete epoch.

Flow:
1. Convert prediction dicts to lists (deduplicated at submission time)
2. For each (episode, prediction, outcome) triple:
   a. Score episode via EpisodeScorer
   b. Compute skill score vs system estimate baseline
3. Apply coverage penalty (miners MUST cover most episodes)
4. Apply null penalty across all predictions
5. Aggregate episode scores with episode weighting
6. Return final miner scores
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from hope.protocol.episode import Episode
from hope.protocol.prediction import Prediction
from hope.protocol.outcomes import Outcome
from hope.scoring.episode_scorer import EpisodeScore, EpisodeScorer
from hope.scoring.null_penalty import NullPenalty
from hope.scoring.skill_score import SkillScoreCalculator
from hope.scoring.weights import ScoringWeights

logger = logging.getLogger(__name__)

# Coverage penalty: miners must cover at least this fraction of episodes
MIN_COVERAGE_FRACTION = 0.50  # Must predict at least 50% of episodes
COVERAGE_PENALTY_POWER = 2.0  # Quadratic penalty below full coverage


@dataclass
class MinerScore:
    """Final score for a single miner in an epoch."""

    miner_id: str
    raw_score: float
    skill_score: float
    null_penalty: float
    coverage_penalty: float
    final_score: float
    episodes_scored: int
    episodes_total: int
    coverage_fraction: float
    episode_scores: list[EpisodeScore] = field(default_factory=list)


class EpochScorer:
    """Top-level scoring orchestrator for a complete epoch."""

    def __init__(self, weights: ScoringWeights | None = None):
        self.weights = weights or ScoringWeights()
        self.episode_scorer = EpisodeScorer(self.weights)
        self.null_penalty_calc = NullPenalty()
        self.skill_calc = SkillScoreCalculator()

        # Validate scoring weights at initialization
        if not self.weights.validate_ranges():
            raise ValueError(
                "Scoring weights outside published ranges. "
                f"Current: qa={self.weights.quantile_accuracy}, cal={self.weights.calibration}, "
                f"dir={self.weights.directional}, goal={self.weights.goal_accuracy}"
            )

    def _compute_episode_weight(self, episode: Episode) -> float:
        """Compute weighting for an episode based on resolution and coverage."""
        resolution = episode.episode_metadata.measurement_resolution
        coverage = episode.episode_metadata.coverage_status

        base = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(resolution, 0.5)

        if coverage == "trust_enriched":
            base *= 1.2

        return base

    def _compute_coverage_penalty(
        self,
        episodes_scored: int,
        total_episodes: int,
    ) -> float:
        """Compute coverage penalty for miners who skip episodes.

        Coverage fraction = episodes_predicted / total_episodes
        If coverage < MIN_COVERAGE_FRACTION: score = 0 (hard cutoff)
        Otherwise: penalty = (1 - coverage_fraction) ^ COVERAGE_PENALTY_POWER

        This means:
        - 100% coverage = 0% penalty
        - 80% coverage = 4% penalty (0.2^2)
        - 60% coverage = 16% penalty (0.4^2)
        - 50% coverage = 25% penalty (0.5^2)
        - <50% coverage = score zeroed out
        """
        if total_episodes == 0:
            return 0.0

        coverage = episodes_scored / total_episodes

        if coverage < MIN_COVERAGE_FRACTION:
            return 1.0  # Full penalty — score zeroed

        gap = 1.0 - coverage
        return gap ** COVERAGE_PENALTY_POWER

    def _normalize_predictions(
        self,
        predictions: list[Prediction] | dict[str, Prediction],
    ) -> list[Prediction]:
        """Convert predictions to list, handling both dict (new) and list (legacy) formats."""
        if isinstance(predictions, dict):
            return list(predictions.values())
        return predictions

    def score_miner(
        self,
        miner_id: str,
        predictions: list[Prediction] | dict[str, Prediction],
        episodes: list[Episode],
        outcomes: list[Outcome],
    ) -> MinerScore:
        """Score a single miner across all episodes in the epoch."""
        pred_list = self._normalize_predictions(predictions)
        episode_map = {ep.episode_metadata.episode_id: ep for ep in episodes}
        outcome_map = {o.episode_id: o for o in outcomes}
        # Count total scorable episodes (those with outcomes)
        total_scorable = sum(1 for eid in episode_map if eid in outcome_map)

        scored_episodes: list[EpisodeScore] = []
        pred_by_eid = {p.episode_id: p for p in pred_list}

        # Score over ALL scorable episodes — skipped episodes contribute zero
        # to numerator but still count in denominator. This makes cherry-picking
        # strictly worse than full coverage at equal per-episode quality.
        weighted_sum = 0.0
        weight_sum = 0.0

        for eid, episode in episode_map.items():
            outcome = outcome_map.get(eid)
            if not outcome:
                continue

            ep_weight = self._compute_episode_weight(episode)
            weight_sum += ep_weight

            pred = pred_by_eid.get(eid)
            if not pred:
                # Skipped episode: zero score but weight still counts
                continue

            ep_score = self.episode_scorer.score_episode(pred, outcome, episode)
            scored_episodes.append(ep_score)
            weighted_sum += ep_score.weighted_total * ep_weight

        raw_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0

        # Compute skill score against baseline
        baseline_score = self._compute_baseline_score(pred_list, episodes, outcomes)
        skill = self.skill_calc.compute_skill_score(raw_score, baseline_score)

        # C5: Null penalty computed over ALL episodes, not just submitted ones.
        # Skipped episodes count as near-zero. No-action (control) episodes
        # are exempted — their true outcome is ~0 by definition, so a
        # miner predicting near-zero on them is being honest, not lazy.
        no_action_ids = {
            ep.episode_metadata.episode_id
            for ep in episodes
            if not ep.action_bundle.actions
        }
        nz_fraction = self.null_penalty_calc.compute_near_zero_fraction(
            pred_list,
            total_episodes=total_scorable,
            no_action_episode_ids=no_action_ids,
        )
        penalty = self.null_penalty_calc.compute_penalty(nz_fraction)

        # C2: Coverage penalty — miners must predict most episodes
        episodes_scored = len(scored_episodes)
        coverage_penalty = self._compute_coverage_penalty(episodes_scored, total_scorable)
        coverage_fraction = episodes_scored / total_scorable if total_scorable > 0 else 0.0

        final = raw_score * (1.0 - penalty) * (1.0 - coverage_penalty)

        return MinerScore(
            miner_id=miner_id,
            raw_score=round(raw_score, 6),
            skill_score=round(skill, 6),
            null_penalty=round(penalty, 6),
            coverage_penalty=round(coverage_penalty, 6),
            final_score=round(final, 6),
            episodes_scored=episodes_scored,
            episodes_total=total_scorable,
            coverage_fraction=round(coverage_fraction, 4),
            episode_scores=scored_episodes,
        )

    def _compute_baseline_score(
        self,
        predictions: list[Prediction],
        episodes: list[Episode],
        outcomes: list[Outcome],
    ) -> float:
        """Score the predict-zero baseline across all episodes."""
        episode_map = {ep.episode_metadata.episode_id: ep for ep in episodes}
        outcome_map = {o.episode_id: o for o in outcomes}

        weighted_sum = 0.0
        weight_sum = 0.0

        for pred in predictions:
            episode = episode_map.get(pred.episode_id)
            outcome = outcome_map.get(pred.episode_id)
            if not episode or not outcome:
                continue

            baseline_pred = self.skill_calc.compute_baseline_prediction(
                pred.episode_id,
                baseline_type=outcome.scoring_metadata.baseline_type,
                conditional_prior=outcome.scoring_metadata.conditional_prior,
            )
            ep_score = self.episode_scorer.score_episode(baseline_pred, outcome, episode)
            ep_weight = self._compute_episode_weight(episode)

            weighted_sum += ep_score.weighted_total * ep_weight
            weight_sum += ep_weight

        return weighted_sum / weight_sum if weight_sum > 0 else 0.0

    def score_epoch(
        self,
        all_predictions: dict[str, list[Prediction] | dict[str, Prediction]],
        episodes: list[Episode],
        outcomes: list[Outcome],
    ) -> dict[str, MinerScore]:
        """Score all miners in an epoch. Returns miner_id -> MinerScore."""
        results = {}
        for miner_id, preds in all_predictions.items():
            results[miner_id] = self.score_miner(miner_id, preds, episodes, outcomes)
        return results
