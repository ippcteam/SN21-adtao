"""Per-cell consensus engine.

The app does not consume one epoch-wide number — it consumes a forecast for a
specific *cell*: a `(transition × account-shape × horizon)` combination, e.g.
"BUDGET:up_large in a mid-spend account, 14-day horizon → cost/conv/cpa
distribution + confidence". This module turns per-episode signal into those
cells.

Two signal sources, same cell/cascade/confidence machinery:

  - ``outcome``     — the empirical distribution of *measured* outcomes for the
                      episodes in a cell. This is the base rate the app can use
                      today, and the baseline miners must beat.
  - ``prediction``  — the consensus of *elite-miner* predictions for the
                      episodes in a cell (optionally skill-weighted). This is the
                      subnet's value-add; it only beats the base rate once miners
                      are good.

Because most cells are thin (see the taxonomy), a single (transition × shape)
cell rarely has enough samples. So every reading resolves through a **power-gated
cascade**: ``(transition × shape) → (transition) → (action_family)``, returning
the *deepest* level that clears the sample bar, with a confidence that reflects
the cell's depth and dispersion. Samples accumulate across epochs (rolling), so
cells fill over time even when no single epoch is enough.

This module is additive: it reads the episodes/outcomes/predictions the validator
already has and changes no scoring, submission, or on-chain behaviour.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

# Horizons every outcome/prediction carries.
HORIZONS = ("7", "14")
# Metrics we publish a distribution for. 'efficiency' = CPA or ROAS depending on
# the account goal (the app maps it by goal type).
METRICS = ("cost", "conversions", "efficiency")

# Map the payload's 4-way spend_bucket onto the 3 coarse strata the taxonomy
# uses. micro+low fold into 'small' (both are sparse); mid stays; high → 'large'.
# Strata are also identity-mapped so the function is idempotent — callers may
# pass either a raw spend_bucket or an already-resolved stratum.
_SHAPE_STRATA = {
    "micro": "small",
    "low": "small",
    "mid": "mid",
    "high": "large",
    "small": "small",
    "large": "large",
}

# Cascade levels, deepest first.
LEVEL_CELL = "transition_x_shape"   # (transition_key, shape)
LEVEL_TRANSITION = "transition"     # (transition_key)
LEVEL_FAMILY = "action_family"      # (BUDGET / BID_SWITCH / TARGET / ...)


def shape_stratum(spend_bucket: Optional[str]) -> str:
    """Coarse account-size stratum from the payload spend_bucket."""
    return _SHAPE_STRATA.get((spend_bucket or "").lower(), "mid")


def action_family(transition_key: str) -> str:
    """The action family prefix of a transition key.

    'BUDGET:up_large' -> 'BUDGET'; 'BID_SWITCH:a->b' -> 'BID_SWITCH';
    'TARGET:strat:up' -> 'TARGET'; 'CAMPAIGN_PAUSE' -> 'CAMPAIGN_PAUSE'.
    """
    return transition_key.split(":", 1)[0] if transition_key else "UNKNOWN"


def cell_label(level: str, key: str, shape: Optional[str] = None) -> str:
    """Human-readable label for a resolved cell."""
    if level == LEVEL_CELL:
        return f"{key} × {shape}"
    return key


def _quantiles_from_samples(samples: list[float]) -> tuple[float, float, float]:
    """Empirical p10/p50/p90 of a sample list."""
    arr = np.asarray(samples, dtype=float)
    return (
        round(float(np.percentile(arr, 10)), 4),
        round(float(np.percentile(arr, 50)), 4),
        round(float(np.percentile(arr, 90)), 4),
    )


@dataclass
class CellConsensus:
    """A resolved consensus reading for one (cell, horizon)."""

    level: str                         # LEVEL_CELL / LEVEL_TRANSITION / LEVEL_FAMILY
    label: str                         # e.g. "BUDGET:up_large × mid"
    transition_key: Optional[str]
    shape: Optional[str]
    horizon: str                       # "7" | "14"
    n: int                             # episodes backing this reading
    source: str                        # "outcome" | "prediction"
    status: str                        # "publish" | "provisional"
    confidence: float                  # 0..1
    quantiles: dict[str, tuple[float, float, float]]  # metric -> (p10,p50,p90)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "label": self.label,
            "transition_key": self.transition_key,
            "shape": self.shape,
            "horizon": self.horizon,
            "n": self.n,
            "source": self.source,
            "status": self.status,
            "confidence": self.confidence,
            "quantiles": {m: list(q) for m, q in self.quantiles.items()},
        }


# (level, key, shape) -> horizon -> metric -> list[float]
_Store = dict


class CellConsensusBuilder:
    """Accumulates per-episode signal into cells and resolves consensus.

    Usage::

        b = CellConsensusBuilder()
        b.ingest_outcomes(episodes, outcomes)              # one or many epochs
        c = b.resolve("BUDGET:up_large", "mid", "14")      # deepest sufficient cell

    Rolling across epochs: keep one builder and call ``ingest_*`` each epoch, or
    persist/reload via ``to_state`` / ``load_state``.
    """

    def __init__(self, publish_n: int = 20, provisional_n: int = 10):
        if provisional_n > publish_n:
            raise ValueError("provisional_n must be <= publish_n")
        self.publish_n = publish_n
        self.provisional_n = provisional_n
        # source -> (level,key,shape) -> horizon -> metric -> samples
        self._store: dict[str, _Store] = {
            "outcome": defaultdict(lambda: defaultdict(lambda: defaultdict(list))),
            "prediction": defaultdict(lambda: defaultdict(lambda: defaultdict(list))),
        }

    # ---- ingestion -------------------------------------------------------

    def _level_keys(self, transition_key: str, shape: str):
        """The three cascade keys an episode contributes to, deepest first."""
        return [
            (LEVEL_CELL, transition_key, shape),
            (LEVEL_TRANSITION, transition_key, None),
            (LEVEL_FAMILY, action_family(transition_key), None),
        ]

    def _add(self, source, lk, horizon, metric, value):
        if value is None:
            return
        self._store[source][lk][horizon][metric].append(float(value))

    def ingest_outcomes(self, episodes: Iterable, outcomes: Iterable) -> int:
        """Accumulate measured-outcome samples. Returns episodes ingested."""
        outcome_map = {o.episode_id: o for o in outcomes}
        added = 0
        for ep in episodes:
            tk = ep.action_bundle.bundle_summary.transition_key
            if not tk:
                continue
            o = outcome_map.get(ep.episode_metadata.episode_id)
            if o is None:
                continue
            shape = shape_stratum(ep.account_state.spend_bucket)
            keys = self._level_keys(tk, shape)
            for horizon in HORIZONS:
                ho = getattr(o, f"t{horizon}", None)
                if ho is None:
                    continue
                for lk in keys:
                    self._add("outcome", lk, horizon, "cost", ho.cost_delta_pct)
                    self._add("outcome", lk, horizon, "conversions", ho.conversions_delta_pct)
                    self._add("outcome", lk, horizon, "efficiency", ho.efficiency_delta_pct)
            added += 1
        return added

    def ingest_predictions(
        self,
        episodes: Iterable,
        predictions_by_miner: dict[str, list],
        miner_weights: Optional[dict[str, float]] = None,
    ) -> int:
        """Accumulate elite-miner prediction samples.

        ``predictions_by_miner`` maps miner_id -> list[Prediction]. Each elite
        miner's predicted central estimate (p50) for an episode contributes a
        sample to that episode's cells; the spread (p10/p90) is pooled too.
        ``miner_weights`` (e.g. skill scores) optionally up-weights a miner by
        contributing its sample proportionally more (rounded replication).
        """
        ep_by_id = {ep.episode_metadata.episode_id: ep for ep in episodes}
        added = 0
        for miner_id, preds in predictions_by_miner.items():
            w = 1 if miner_weights is None else max(1, round(miner_weights.get(miner_id, 0) * 10))
            for pred in preds:
                ep = ep_by_id.get(pred.episode_id)
                if ep is None:
                    continue
                tk = ep.action_bundle.bundle_summary.transition_key
                if not tk:
                    continue
                shape = shape_stratum(ep.account_state.spend_bucket)
                keys = self._level_keys(tk, shape)
                for horizon in HORIZONS:
                    hp = pred.horizons.get(horizon)
                    if hp is None:
                        continue
                    # Pool the predicted central estimate (p50) per metric.
                    samples = {
                        "cost": hp.cost_delta_pct.p50,
                        "conversions": hp.conversions_delta_pct.p50,
                        "efficiency": hp.efficiency_delta_pct.p50,
                    }
                    for lk in keys:
                        for _ in range(w):
                            for metric, val in samples.items():
                                self._add("prediction", lk, horizon, metric, val)
                added += 1
        return added

    # ---- resolution ------------------------------------------------------

    def _build(self, source, lk, horizon) -> Optional[tuple[int, dict]]:
        """Return (n, quantiles) for a stored cell+horizon, or None if empty."""
        per_metric = self._store[source].get(lk, {}).get(horizon)
        if not per_metric:
            return None
        cost_samples = per_metric.get("cost", [])
        n = len(cost_samples)
        if n == 0:
            return None
        quantiles = {}
        for metric in METRICS:
            s = per_metric.get(metric, [])
            if s:
                quantiles[metric] = _quantiles_from_samples(s)
        return n, quantiles

    def _confidence(self, n: int, quantiles: dict) -> float:
        """Confidence rises with sample count and falls with dispersion."""
        n_factor = min(n / self.publish_n, 1.0)
        cost = quantiles.get("cost")
        if cost:
            p10, p50, p90 = cost
            spread = abs(p90 - p10) / max(abs(p50), 1.0)
            spread_factor = 1.0 / (1.0 + spread)
        else:
            spread_factor = 0.5
        return round(n_factor * spread_factor, 3)

    def resolve(
        self,
        transition_key: str,
        shape: str,
        horizon: str,
        source: str = "outcome",
    ) -> Optional[CellConsensus]:
        """Resolve the deepest cell that clears the provisional bar.

        Cascades (transition × shape) → (transition) → (action_family). Returns
        None if even the family level lacks ``provisional_n`` samples.
        """
        if source not in self._store:
            raise ValueError(f"unknown source {source!r}")
        shape = shape_stratum(shape)
        for level, key, sh in self._level_keys(transition_key, shape):
            built = self._build(source, (level, key, sh), horizon)
            if built is None:
                continue
            n, quantiles = built
            if n < self.provisional_n:
                continue
            status = "publish" if n >= self.publish_n else "provisional"
            return CellConsensus(
                level=level,
                label=cell_label(level, key, sh),
                transition_key=transition_key if level != LEVEL_FAMILY else None,
                shape=sh,
                horizon=horizon,
                n=n,
                source=source,
                status=status,
                confidence=self._confidence(n, quantiles),
                quantiles=quantiles,
            )
        return None

    def all_cells(self, source: str = "outcome", min_n: int = 1) -> list[CellConsensus]:
        """Every stored cell+horizon with at least ``min_n`` samples (for reports)."""
        out: list[CellConsensus] = []
        for lk, by_h in self._store[source].items():
            level, key, sh = lk
            for horizon in by_h:
                built = self._build(source, lk, horizon)
                if built is None:
                    continue
                n, quantiles = built
                if n < min_n:
                    continue
                status = (
                    "publish" if n >= self.publish_n
                    else "provisional" if n >= self.provisional_n
                    else "fallback"
                )
                out.append(CellConsensus(
                    level=level,
                    label=cell_label(level, key, sh),
                    transition_key=key if level != LEVEL_FAMILY else None,
                    shape=sh,
                    horizon=horizon,
                    n=n,
                    source=source,
                    status=status,
                    confidence=self._confidence(n, quantiles),
                    quantiles=quantiles,
                ))
        return sorted(out, key=lambda c: (-c.n, c.label, c.horizon))

    # ---- rolling persistence --------------------------------------------

    def to_state(self) -> dict:
        """Serialise accumulated samples (JSON-friendly) for cross-epoch rolling."""
        state = {}
        for source, store in self._store.items():
            state[source] = [
                {"level": lk[0], "key": lk[1], "shape": lk[2],
                 "horizon": h, "metric": m, "samples": vals}
                for lk, by_h in store.items()
                for h, by_m in by_h.items()
                for m, vals in by_m.items()
            ]
        return state

    def load_state(self, state: dict) -> "CellConsensusBuilder":
        """Merge a previously serialised state in (extends current samples)."""
        for source, rows in state.items():
            for r in rows:
                lk = (r["level"], r["key"], r["shape"])
                self._store[source][lk][r["horizon"]][r["metric"]].extend(r["samples"])
        return self
