"""Epoch Scorer — top-level orchestrator for scoring a complete epoch.

Flow:
1. For each (episode, prediction, outcome) triple:
   a. Score episode via EpisodeScorer
   b. Compute skill score vs system estimate baseline
2. Apply null penalty across all predictions
3. Aggregate episode scores with episode weighting
4. Return final miner scores
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hope.protocol.episode import Episode
from hope.protocol.prediction import Prediction
from hope.protocol.outcomes import Outcome
from hope.scoring.episode_scorer import EpisodeScore, EpisodeScorer
from hope.scoring.null_penalty import NullPenalty
from hope.scoring.skill_score import SkillScoreCalculator
from hope.scoring.weights import ScoringWeights


@dataclass
class MinerScore:
    """Final score for a single miner in an epoch."""

    miner_id: str
    raw_score: float
    skill_score: float
    null_penalty: float
    final_score: float
    episodes_scored: int
    episode_scores: list[EpisodeScore] = field(default_factory=list)


class EpochScorer:
    """Top-level scoring orchestrator for a complete epoch."""

    def __init__(self, weights: ScoringWeights | None = None):
        self.weights = weights or ScoringWeights()
        self.episode_scorer = EpisodeScorer(self.weights)
        self.null_penalty_calc = NullPenalty()
        self.skill_calc = SkillScoreCalculator()

    def _compute_episode_weight(self, episode: Episode) -> float:
        """Compute weighting for an episode based on resolution and coverage."""
        resolution = episode.episode_metadata.measurement_resolution
        coverage = episode.episode_metadata.coverage_status

        base = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(resolution, 0.5)

        if coverage == "trust_enriched":
            base *= 1.2  # TRUST episodes carry higher weight

        return base

    def score_miner(
        self,
        miner_id: str,
        predictions: list[Prediction],
        episodes: list[Episode],
        outcomes: list[Outcome],
    ) -> MinerScore:
        """Score a single miner across all episodes in the epoch."""
        episode_map = {ep.episode_metadata.episode_id: ep for ep in episodes}
        outcome_map = {o.episode_id: o for o in outcomes}

        scored_episodes: list[EpisodeScore] = []
        weighted_sum = 0.0
        weight_sum = 0.0

        for pred in predictions:
            episode = episode_map.get(pred.episode_id)
            outcome = outcome_map.get(pred.episode_id)

            if not episode or not outcome:
                continue

            ep_score = self.episode_scorer.score_episode(pred, outcome, episode)
            ep_weight = self._compute_episode_weight(episode)

            scored_episodes.append(ep_score)
            weighted_sum += ep_score.weighted_total * ep_weight
            weight_sum += ep_weight

        raw_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0

        # Compute skill score against baseline
        baseline_score = self._compute_baseline_score(predictions, episodes, outcomes)
        skill = self.skill_calc.compute_skill_score(raw_score, baseline_score)

        # Apply null penalty
        nz_fraction = self.null_penalty_calc.compute_near_zero_fraction(predictions)
        penalty = self.null_penalty_calc.compute_penalty(nz_fraction)
        final = raw_score * (1.0 - penalty)

        return MinerScore(
            miner_id=miner_id,
            raw_score=round(raw_score, 6),
            skill_score=round(skill, 6),
            null_penalty=round(penalty, 6),
            final_score=round(final, 6),
            episodes_scored=len(scored_episodes),
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

            baseline_pred = self.skill_calc.compute_baseline_prediction(pred.episode_id)
            ep_score = self.episode_scorer.score_episode(baseline_pred, outcome, episode)
            ep_weight = self._compute_episode_weight(episode)

            weighted_sum += ep_score.weighted_total * ep_weight
            weight_sum += ep_weight

        return weighted_sum / weight_sum if weight_sum > 0 else 0.0

    def score_epoch(
        self,
        all_predictions: dict[str, list[Prediction]],
        episodes: list[Episode],
        outcomes: list[Outcome],
    ) -> dict[str, MinerScore]:
        """Score all miners in an epoch. Returns miner_id -> MinerScore."""
        results = {}
        for miner_id, preds in all_predictions.items():
            results[miner_id] = self.score_miner(miner_id, preds, episodes, outcomes)
        return results
