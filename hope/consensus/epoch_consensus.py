"""Epoch consensus reporter — turn one scored epoch into per-cell consensus.

Runs *after* an epoch is scored, from the same inputs the scorer uses
(episodes, outcomes, per-miner predictions, and each miner's MinerScore). It
feeds the :class:`CellConsensusBuilder`, merges with persisted rolling state so
cells accumulate across epochs, writes a consensus artifact, and updates the
rolling state on disk.

Deliberately **out of band**: it touches no scoring, weight-setting, submission,
or on-chain code. It only reads scored data and writes JSON. Safe to run (or not
run) without affecting consensus on the chain.

Two consensus tracks land in every artifact:
  - ``outcome``     — empirical base rate from measured outcomes (usable today;
                      also the baseline miners must beat).
  - ``prediction``  — consensus of the *elite* miners (skill_score > 0), weighted
                      by skill (the subnet's value-add, once miners beat baseline).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

from .cell_consensus import CellConsensusBuilder

CONSENSUS_SCHEMA_VERSION = "consensus-v1"

DEFAULT_CONSENSUS_DIR_ENV = "SN21_CONSENSUS_DIR"
DEFAULT_CONSENSUS_DIR = Path("~/.sn21/cell_consensus")
ROLLING_STATE_FILENAME = "rolling_state.json"


# --------------------------------------------------------------------------- #
# Pure build                                                                   #
# --------------------------------------------------------------------------- #

def build_epoch_consensus(
    epoch_id: str,
    episodes: list,
    outcomes: list,
    predictions_by_miner: dict[str, list],
    miner_scores: Optional[dict] = None,
    *,
    publish_n: int = 20,
    provisional_n: int = 10,
    prior_state: Optional[dict] = None,
) -> tuple[dict, dict]:
    """Build the consensus artifact + the new rolling state.

    ``miner_scores`` maps miner_id -> an object/dict with a ``skill_score``.
    Only miners with skill_score > 0 (beat the baseline) contribute to the
    elite-miner prediction track, weighted by their skill.

    Returns ``(artifact_dict, new_rolling_state)``. The artifact is the thing the
    app consumes; the rolling state is fed back in as ``prior_state`` next epoch.
    """
    builder = CellConsensusBuilder(publish_n=publish_n, provisional_n=provisional_n)
    if prior_state:
        builder.load_state(prior_state)

    outcomes_added = builder.ingest_outcomes(episodes, outcomes)

    elite_weights = _elite_weights(miner_scores)
    elite_preds = {m: p for m, p in predictions_by_miner.items() if m in elite_weights}
    preds_added = 0
    if elite_preds:
        preds_added = builder.ingest_predictions(episodes, elite_preds, elite_weights)

    artifact = {
        "schema_version": CONSENSUS_SCHEMA_VERSION,
        "epoch_id": epoch_id,
        "publish_n": publish_n,
        "provisional_n": provisional_n,
        "outcomes_ingested": outcomes_added,
        "elite_miner_count": len(elite_weights),
        "elite_predictions_ingested": preds_added,
        # Full, level-tagged inventory for both tracks. The app resolves a query
        # by the cascade: pick the deepest level (transition_x_shape > transition
        # > action_family) whose status != 'fallback' for the requested
        # (transition_key, shape, horizon).
        "cells": {
            "outcome": [c.to_dict() for c in builder.all_cells("outcome", min_n=1)],
            "prediction": [c.to_dict() for c in builder.all_cells("prediction", min_n=1)],
        },
    }
    return artifact, builder.to_state()


def _elite_weights(miner_scores: Optional[dict]) -> dict[str, float]:
    """miner_id -> skill_score for miners that beat the baseline (skill > 0)."""
    if not miner_scores:
        return {}
    weights = {}
    for miner_id, score in miner_scores.items():
        skill = getattr(score, "skill_score", None)
        if skill is None and isinstance(score, dict):
            skill = score.get("skill_score")
        if skill is not None and skill > 0:
            weights[miner_id] = float(skill)
    return weights


# --------------------------------------------------------------------------- #
# Persistence (mirrors hope/reporting/epoch_artifact.py: atomic JSON writes)   #
# --------------------------------------------------------------------------- #

def resolve_consensus_dir(base_dir: Optional[Path] = None) -> Path:
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    env_value = os.environ.get(DEFAULT_CONSENSUS_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_CONSENSUS_DIR.expanduser().resolve()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_rolling_state(base_dir: Optional[Path] = None) -> Optional[dict]:
    path = resolve_consensus_dir(base_dir) / ROLLING_STATE_FILENAME
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_rolling_state(state: dict, base_dir: Optional[Path] = None) -> Path:
    path = resolve_consensus_dir(base_dir) / ROLLING_STATE_FILENAME
    _atomic_write_json(path, state)
    return path


def artifact_path_for(epoch_id: str, base_dir: Optional[Path] = None) -> Path:
    safe = epoch_id.replace("/", "_")
    return resolve_consensus_dir(base_dir) / f"consensus_{safe}.json"


def write_artifact(artifact: dict, base_dir: Optional[Path] = None) -> Path:
    path = artifact_path_for(artifact["epoch_id"], base_dir)
    _atomic_write_json(path, artifact)
    return path


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def compute_and_persist_consensus(
    epoch_id: str,
    episodes: list,
    outcomes: list,
    predictions_by_miner: dict[str, list],
    miner_scores: Optional[dict] = None,
    *,
    base_dir: Optional[Path] = None,
    publish_n: int = 20,
    provisional_n: int = 10,
    publish_url: Optional[str] = None,
) -> tuple[Path, dict]:
    """Build + persist the rolling consensus for one epoch.

    Loads the prior rolling state, ingests this epoch, writes the per-epoch
    artifact and the updated rolling state, and (optionally) POSTs the artifact
    to ``publish_url``. Returns ``(artifact_path, artifact)``.
    """
    prior = read_rolling_state(base_dir)
    artifact, new_state = build_epoch_consensus(
        epoch_id, episodes, outcomes, predictions_by_miner, miner_scores,
        publish_n=publish_n, provisional_n=provisional_n, prior_state=prior,
    )
    path = write_artifact(artifact, base_dir)
    write_rolling_state(new_state, base_dir)

    if publish_url:
        _publish(artifact, publish_url)

    return path, artifact


def _publish(artifact: dict, url: str) -> bool:
    """Best-effort POST of the artifact to a consumer (e.g. OBI). Never raises."""
    try:
        import httpx

        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("SN21_CONSENSUS_PUBLISH_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        resp = httpx.post(url, json=artifact, headers=headers, timeout=30.0)
        return resp.status_code < 400
    except Exception:
        return False
