"""Per-epoch artifact — the private bridge between scoring and the public report.

After a successful scoring run, the validator writes one JSON file
per epoch containing everything the leaderboard reporter needs to
produce the public aggregate payload. The artifact is:

  * **Operator-private.** It carries per-UID score data and miner
    hotkeys. The aggregator strips this down to the public-payload
    shape; only that aggregate is ever POSTed to the website.
  * **Human-inspectable.** JSON, indented. Easy to diff weekly,
    easy to debug.
  * **Re-readable.** A file per epoch keyed by release key. Re-running
    the reporter (or re-POSTing on 5xx retry) reads back what was
    written without re-doing the chain work.

Storage convention: `${SN21_EPOCH_ARTIFACT_DIR}/epoch_<release_key>.json`,
default base dir `~/.sn21/epoch_artifacts`. Atomic write via a
per-process tempfile + `os.rename` so concurrent writers (the active
scoring run and a retry of the same epoch) cannot leave a partial
JSON on disk.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hope.constants import HORIZONS, SCORING_FORMULA_VERSION
from hope.reporting.git_sha import current_commit_sha
from hope.reporting.tier_compute import compute_tier_result_from_score_map
from hope.validator.onchain_runner import EpochScoringOutcome


DEFAULT_ARTIFACT_DIR_ENV = "SN21_EPOCH_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = Path("~/.sn21/epoch_artifacts")


@dataclass
class EpochArtifact:
    """Operator-private per-epoch record.

    Carries everything the leaderboard reporter aggregates into the
    public payload. Per-UID fields stay private; the aggregator
    surfaces only counts + distribution shape.
    """

    # Identity
    epoch_id: str

    # Provenance — the policy version + the executable that produced the numbers
    scoring_formula_version: str
    scoring_formula_commit: str

    # Epoch classification (for the public payload's epoch_type fields)
    epoch_type: str
    epoch_subtype: Optional[str]
    epoch_type_multiplier: float
    horizon_set: list[str]

    # On-chain footprint of this epoch (from the runner's outcome)
    block_range_start: Optional[int]
    block_range_end: Optional[int]

    # Pool size context
    total_registered_uids: int

    # Snapshot timing — when this artifact was finalized + when chain was read
    validator_output_snapshot_timestamp: str
    chain_fetch_timestamp: str

    # Private per-miner detail. Stays in this file; never POSTed verbatim.
    per_uid_scores: list[dict[str, Any]] = field(default_factory=list)

    # Tier allocation outcome — serialized TierAllocationResult.
    tier_result: dict[str, Any] = field(default_factory=dict)

    # Reporting-side schema version; bump if the artifact wire shape
    # changes in a way the aggregator must adapt to.
    artifact_schema_version: int = 1


def resolve_artifact_dir(base_dir: Optional[Path] = None) -> Path:
    """Resolve the artifact directory.

    Precedence: explicit arg > `SN21_EPOCH_ARTIFACT_DIR` env > default.
    Expands `~` and returns an absolute Path. Caller is responsible for
    creating the directory; `write_artifact` does it implicitly.
    """
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    env_value = os.environ.get(DEFAULT_ARTIFACT_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_ARTIFACT_DIR.expanduser().resolve()


def artifact_path_for(epoch_id: str, base_dir: Optional[Path] = None) -> Path:
    """Return the file path this epoch_id maps to."""
    safe_epoch_id = epoch_id.replace("/", "_").replace("..", "_")
    return resolve_artifact_dir(base_dir) / f"epoch_{safe_epoch_id}.json"


def write_artifact(artifact: EpochArtifact, base_dir: Optional[Path] = None) -> Path:
    """Write the artifact to disk atomically.

    Returns the final path. Creates `base_dir` if it does not exist.
    Replaces any existing file at the target path (write_artifact is
    intended to be idempotent: the latest scoring run for an epoch
    wins).
    """
    base = resolve_artifact_dir(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    final_path = artifact_path_for(artifact.epoch_id, base_dir=base)
    tmp_path = final_path.with_suffix(f".json.tmp.{os.getpid()}")

    payload = dataclasses.asdict(artifact)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)
    return final_path


def read_artifact(epoch_id: str, base_dir: Optional[Path] = None) -> EpochArtifact:
    """Round-trip read of a previously written artifact.

    The reporter's POST path reads back what was written so it can
    retry without recomputing.
    """
    final_path = artifact_path_for(epoch_id, base_dir=base_dir)
    with open(final_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return EpochArtifact(**payload)


def _build_per_uid_scores(outcome: EpochScoringOutcome) -> list[dict[str, Any]]:
    """Cross-reference outcome.score_map (hotkey→score_micro) with
    outcome.miner_reads (hotkey→uid) into the private per-miner list.
    """
    hotkey_to_uid: dict[bytes, int] = {
        read.miner_hotkey: read.miner_uid for read in outcome.miner_reads
    }
    rows: list[dict[str, Any]] = []
    for hotkey, score_micro in outcome.score_map.items():
        rows.append({
            "uid": hotkey_to_uid.get(hotkey, -1),
            "hotkey": hotkey.hex(),
            "score_micro": int(score_micro),
            "raw_score": score_micro / 1_000_000.0,
        })
    # Stable ordering for diff-friendliness — sort by uid then hotkey.
    rows.sort(key=lambda r: (r["uid"], r["hotkey"]))
    return rows


def build_artifact(
    *,
    outcome: EpochScoringOutcome,
    epoch_id: str,
    total_registered_uids: int,
    chain_fetch_timestamp: str,
    epoch_type: str = "Search",
    epoch_subtype: Optional[str] = "campaign-level",
    epoch_type_multiplier: float = 1.0,
) -> EpochArtifact:
    """Assemble an EpochArtifact from a completed scoring run.

    Args:
        outcome: the `EpochScoringOutcome` returned by `run_epoch_scoring`.
        epoch_id: the release key being scored.
        total_registered_uids: `metagraph.n` at chain-fetch time.
        chain_fetch_timestamp: ISO8601 UTC of when the metagraph was read.
        epoch_type / epoch_subtype / epoch_type_multiplier: classification
            of this epoch per `SN21_REWARD_MECHANISM.md` §"Component 3".
            Phase 1 ships only the Search/campaign-level row; richer
            classification (lookup against the release's `scope_filter`
            using `EPOCH_TYPE_TABLE`) lands when other epoch types come
            into scope.

    Returns:
        An `EpochArtifact` with all fields populated. Tier result is
        computed via `compute_tier_result_from_score_map(...)` on the
        outcome's score_map. The artifact is NOT written to disk by
        this function — call `write_artifact(...)` separately, or use
        `build_and_write_artifact(...)` for the one-shot path.
    """
    tier_result_obj = compute_tier_result_from_score_map(outcome.score_map)
    tier_result_dict = dataclasses.asdict(tier_result_obj)

    return EpochArtifact(
        epoch_id=epoch_id,
        scoring_formula_version=SCORING_FORMULA_VERSION,
        scoring_formula_commit=current_commit_sha(fallback_env="SN21_BUILD_SHA"),
        epoch_type=epoch_type,
        epoch_subtype=epoch_subtype,
        epoch_type_multiplier=epoch_type_multiplier,
        horizon_set=[f"{h}d" for h in HORIZONS],
        block_range_start=outcome.block_range_start,
        block_range_end=outcome.block_range_end,
        total_registered_uids=total_registered_uids,
        validator_output_snapshot_timestamp=datetime.now(timezone.utc).isoformat(),
        chain_fetch_timestamp=chain_fetch_timestamp,
        per_uid_scores=_build_per_uid_scores(outcome),
        tier_result=tier_result_dict,
    )


def build_and_write_artifact(
    *,
    outcome: EpochScoringOutcome,
    epoch_id: str,
    total_registered_uids: int,
    chain_fetch_timestamp: str,
    base_dir: Optional[Path] = None,
    epoch_type: str = "Search",
    epoch_subtype: Optional[str] = "campaign-level",
    epoch_type_multiplier: float = 1.0,
) -> Path:
    """One-shot: build + write. Returns the path the artifact was written to."""
    artifact = build_artifact(
        outcome=outcome,
        epoch_id=epoch_id,
        total_registered_uids=total_registered_uids,
        chain_fetch_timestamp=chain_fetch_timestamp,
        epoch_type=epoch_type,
        epoch_subtype=epoch_subtype,
        epoch_type_multiplier=epoch_type_multiplier,
    )
    return write_artifact(artifact, base_dir=base_dir)
