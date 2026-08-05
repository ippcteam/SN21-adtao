"""Baseline Model — reference implementation for miners.

This is the predict-zero-plus-heuristics model from miner_quickstart.md
Section 9. It demonstrates how to parse episode features and produce
valid predictions. Miners should aim to beat this model.

Strategy:
1. Start from the action magnitude (if available) as the P50
2. Adjust for portfolio constraints (redistribution likelihood)
3. Widen intervals based on measurement resolution and volatility
4. Set goal_miss_probability from goal deviation + action impact
"""

from __future__ import annotations

from datetime import datetime, timezone

from hope.miner.prediction_engine import PredictionEngine
from hope.protocol.episode import Episode
from hope.protocol.prediction import (
    HorizonPrediction,
    Prediction,
    QuantilePrediction,
)


class BaselineModel(PredictionEngine):
    """Reference baseline model. Demonstrates valid prediction format."""

    def predict(self, episode: Episode) -> Prediction:
        """Generate predictions based on action magnitude and heuristics."""
        em = episode.episode_metadata
        action = episode.action_bundle.actions[0] if episode.action_bundle.actions else None
        agg = episode.pre_window.account_aggregates

        # Extract base estimates from action magnitude
        cost_p50, conv_p50, eff_p50 = self._base_estimates(action)

        # Adjust for portfolio constraints
        portfolio = episode.account_state.portfolio_context
        if portfolio and action:
            cost_p50, conv_p50 = self._adjust_for_redistribution(
                cost_p50, conv_p50, portfolio.redistribution_likelihood, action.type
            )

        # Compute spread based on volatility and resolution
        spread = self._compute_spread(agg.spend_cv, em.measurement_resolution)

        # Build horizon predictions (14-day is dampened version of 7-day)
        h7 = self._build_horizon(cost_p50, conv_p50, eff_p50, spread, episode)
        h14 = self._build_horizon(
            cost_p50 * 0.85, conv_p50 * 0.85, eff_p50 * 0.85,
            spread * 1.1, episode,
        )

        return Prediction(
            episode_id=em.episode_id,
            miner_id="baseline_model",
            submitted_at=datetime.now(timezone.utc),
            horizons={"7": h7, "14": h14},
        )

    def _base_estimates(self, action) -> tuple[float, float, float]:
        """Extract P50 estimates from action magnitude."""
        if not action or not action.magnitude:
            return 0.0, 0.0, 0.0

        mag = action.magnitude

        if action.type == "BUDGET_CHANGE":
            # `spend_change_pct.expected` is expressed in percent on the
            # episode payload (e.g. 20 means +20%). Outcome deltas are
            # fractional, so divide by 100 to convert to the matching
            # fractional scale. Field is often null on live releases —
            # treat that as no point-estimate signal (caller derives one
            # from the pre-window trend; see miner_quickstart.md §6).
            spend_pct = mag.get("spend_change_pct", {})
            if isinstance(spend_pct, dict):
                cost_p50 = (spend_pct.get("expected") or 0.0) / 100.0
            else:
                cost_p50 = float(spend_pct or 0.0) / 100.0
            # Budget increase → proportional conversion increase (simplified)
            conv_p50 = cost_p50 * 0.7  # Diminishing returns
            eff_p50 = conv_p50 - cost_p50  # Efficiency = conv gain - cost gain

        elif action.type == "CAMPAIGN_PAUSE":
            # Deterministic — campaign paused means spend and conversions
            # go to zero, a -1.0 fractional delta from the pre-window.
            cost_p50 = -1.0
            conv_p50 = -1.0
            eff_p50 = 0.0  # CPA undefined when both zero

        elif action.type == "CAMPAIGN_ENABLE":
            # Deprecated action type — kept for legacy episode handling
            # only; not part of LAUNCH_ACTION_TYPES.
            pause_days = mag.get("prior_pause_duration_days", 7)
            recovery = min(0.8, 0.5 + pause_days * 0.02)
            cost_p50 = recovery * 0.80  # Fractional: 80% partial recovery
            conv_p50 = recovery * 0.60
            eff_p50 = conv_p50 - cost_p50

        elif action.type == "BID_STRATEGY_CHANGE":
            # Bid strategy changes are volatile during learning period
            cost_p50 = 0.0  # Direction uncertain
            conv_p50 = 0.0
            eff_p50 = 0.0

        else:
            cost_p50 = 0.0
            conv_p50 = 0.0
            eff_p50 = 0.0

        return cost_p50, conv_p50, eff_p50

    def _adjust_for_redistribution(
        self, cost_p50: float, conv_p50: float,
        redistribution_likelihood: float, action_type: str
    ) -> tuple[float, float]:
        """Adjust for budget redistribution in constrained accounts."""
        if action_type in ("CAMPAIGN_PAUSE",) and redistribution_likelihood > 0.3:
            # In constrained accounts, pausing one campaign redirects budget
            # to siblings — conversions may not drop as much
            conv_dampening = 1.0 - (redistribution_likelihood * 0.5)
            conv_p50 = conv_p50 * conv_dampening
        return cost_p50, conv_p50

    def _compute_spread(self, spend_cv: float, resolution: str) -> float:
        """Compute prediction interval half-width in fractional units.

        Returns a base that's added to / subtracted from p50 to set the
        p10 / p90 bounds. 0.05 is "±5 percentage points around the
        point estimate".
        """
        base = 0.05

        # Wider for volatile accounts
        if spend_cv > 0.3:
            base *= 1.5
        elif spend_cv > 0.15:
            base *= 1.2

        # Wider for lower resolution
        if resolution == "medium":
            base *= 1.3
        elif resolution == "low":
            base *= 1.8

        return base

    def _build_horizon(
        self, cost_p50: float, conv_p50: float, eff_p50: float,
        spread: float, episode: Episode,
    ) -> HorizonPrediction:
        """Build a HorizonPrediction with symmetric spread."""
        # Floor on the half-width so p90 - p10 always exceeds the
        # spec-defined MIN_INTERVAL_WIDTH (see hope/constants.py).
        from hope.constants import MIN_INTERVAL_WIDTH
        spread = max(spread, MIN_INTERVAL_WIDTH)

        # Compute goal miss probability
        goal_miss = self._estimate_goal_miss(episode, cost_p50, conv_p50)

        return HorizonPrediction(
            cost_delta_pct=QuantilePrediction(
                p10=cost_p50 - spread, p50=cost_p50, p90=cost_p50 + spread,
            ),
            conversions_delta_pct=QuantilePrediction(
                p10=conv_p50 - spread, p50=conv_p50, p90=conv_p50 + spread,
            ),
            efficiency_delta_pct=QuantilePrediction(
                p10=eff_p50 - spread, p50=eff_p50, p90=eff_p50 + spread,
            ),
            goal_miss_probability=goal_miss,
            instability_risk=0.15,
        )

    def _estimate_goal_miss(self, episode: Episode, cost_p50: float, conv_p50: float) -> float:
        """Estimate probability of missing the account goal."""
        goal = episode.account_state.goal
        if not goal:
            return 0.3  # No goal info → uncertain

        deviation = goal.deviation
        tolerance = goal.tolerance

        # Already below goal → higher miss probability
        if deviation < -tolerance:
            base_miss = 0.6
        elif deviation < 0:
            base_miss = 0.3
        else:
            base_miss = 0.1

        # Destructive actions increase miss probability
        action = episode.action_bundle.actions[0] if episode.action_bundle.actions else None
        if action and action.type == "CAMPAIGN_PAUSE":
            base_miss = min(1.0, base_miss + 0.3)

        return round(min(1.0, max(0.0, base_miss)), 2)
