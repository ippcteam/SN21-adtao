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
from typing import Any

from hope.constants import HORIZONS, SCORING_FORMULA_VERSION
from hope.reporting.git_sha import current_commit_sha
from hope.reporting.tier_compute import compute_tier_result_from_score_map
from hope.validator.onchain_runner import EpochScoringOutcome

DEFAULT_ARTIFACT_DIR_ENV = "SN21_EPOCH_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = Path("~/.sn21/epoch_artifacts")

# The daily stream settles at three horizons; the weekly-era HORIZONS is [7, 14].
DAILY_HORIZONS = [7, 14, 28]


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
    epoch_subtype: str | None
    epoch_type_multiplier: float
    horizon_set: list[str]

    # On-chain footprint of this epoch (from the runner's outcome)
    block_range_start: int | None
    block_range_end: int | None

    # Pool size context
    total_registered_uids: int

    # Snapshot timing — when this artifact was finalized + when chain was read
    validator_output_snapshot_timestamp: str
    chain_fetch_timestamp: str

    # The predict-zero participation-gate baseline the on-chain scorer used
    # this epoch. CRITICAL: the leaderboard MUST tier/gate against this same
    # value, not a 0.0 placeholder — otherwise the published tiers disagree
    # with the on-chain funded set (a miner shown "Competitive" earns 0 on
    # chain), which reads as the validator cheating. Surfaced in the payload.
    baseline_score: float = 0.0

    # Private per-miner detail. Stays in this file; never POSTed verbatim.
    per_uid_scores: list[dict[str, Any]] = field(default_factory=list)

    # Tier allocation outcome — serialized TierAllocationResult.
    tier_result: dict[str, Any] = field(default_factory=dict)

    # Reporting-side schema version; bump if the artifact wire shape
    # changes in a way the aggregator must adapt to.
    artifact_schema_version: int = 1


def resolve_artifact_dir(base_dir: Path | None = None) -> Path:
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


def artifact_path_for(epoch_id: str, base_dir: Path | None = None) -> Path:
    """Return the file path this epoch_id maps to."""
    safe_epoch_id = epoch_id.replace("/", "_").replace("..", "_")
    return resolve_artifact_dir(base_dir) / f"epoch_{safe_epoch_id}.json"


def write_artifact(artifact: EpochArtifact, base_dir: Path | None = None) -> Path:
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


def read_artifact(epoch_id: str, base_dir: Path | None = None) -> EpochArtifact:
    """Round-trip read of a previously written artifact.

    The reporter's POST path reads back what was written so it can
    retry without recomputing.
    """
    final_path = artifact_path_for(epoch_id, base_dir=base_dir)
    with open(final_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return EpochArtifact(**payload)


def _build_per_uid_scores(outcome: EpochScoringOutcome,
                          *, baseline_score: float = 0.0) -> list[dict[str, Any]]:
    """Cross-reference outcome.score_map (hotkey→score_micro) with
    outcome.miner_reads (hotkey→uid) into the private per-miner list.

    Two kinds of row:
      * SCORED — one per entry in `score_map` (no `status`; the aggregator
        defaults these to "scored").
      * NOT SCORED — one per `miner_reads` entry that did NOT score
        (`ok is False`), carrying `status = excluded_reason` so the
        leaderboard shows WHY (not_registered, invalid_commit, late,
        plaintext_unavailable, …). The CMS Disqualifier panel renders
        these — without them, miners who submitted but didn't score would
        silently vanish from the published table.
    """
    hotkey_to_uid: dict[bytes, int] = {
        read.miner_hotkey: read.miner_uid for read in outcome.miner_reads
    }
    rows: list[dict[str, Any]] = []
    scored_hotkeys: set[bytes] = set()
    for hotkey, score_micro in outcome.score_map.items():
        scored_hotkeys.add(hotkey)
        raw = score_micro / 1_000_000.0
        rows.append({
            "uid": hotkey_to_uid.get(hotkey, -1),
            "hotkey": hotkey.hex(),
            "score_micro": int(score_micro),
            "raw_score": raw,
            # Did this miner clear the gate? (raw_score > baseline). Drives the
            # leaderboard's "met baseline ✓/✗" + the gap-to-baseline display,
            # and reconciles the published tiers with the on-chain funded set.
            "met_baseline": raw > baseline_score,
        })
    # Every miner we READ but could NOT score gets a disqualification row so
    # the published table covers the full submitter set, not just winners.
    for read in outcome.miner_reads:
        if read.ok or read.miner_hotkey in scored_hotkeys:
            continue
        rows.append({
            "uid": read.miner_uid,
            "hotkey": read.miner_hotkey.hex(),
            "score_micro": 0,
            "raw_score": 0.0,
            "met_baseline": False,
            "status": read.excluded_reason or "unknown",
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
    epoch_subtype: str | None = "campaign-level",
    epoch_type_multiplier: float = 1.0,
    baseline_score: float = 0.0,
    horizons: list[int] | None = None,
) -> EpochArtifact:
    """Assemble an EpochArtifact from a completed scoring run.

    `horizons` overrides the published horizon set (default `HORIZONS`, the
    weekly-era [7, 14]). The daily stream passes [7, 14, 28].

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
    # Gate + tier against the REAL predict-zero baseline the chain used — NOT
    # the 0.0 placeholder. This is what makes the published tiers/funded set
    # equal the on-chain funded set (the fix for "Competitive but earns 0").
    tier_result_obj = compute_tier_result_from_score_map(
        outcome.score_map, baseline_score=baseline_score)
    tier_result_dict = dataclasses.asdict(tier_result_obj)
    horizons = horizons if horizons is not None else HORIZONS

    return EpochArtifact(
        epoch_id=epoch_id,
        scoring_formula_version=SCORING_FORMULA_VERSION,
        scoring_formula_commit=current_commit_sha(fallback_env="SN21_BUILD_SHA"),
        epoch_type=epoch_type,
        epoch_subtype=epoch_subtype,
        epoch_type_multiplier=epoch_type_multiplier,
        horizon_set=[f"{h}d" for h in horizons],
        block_range_start=outcome.block_range_start,
        block_range_end=outcome.block_range_end,
        total_registered_uids=total_registered_uids,
        validator_output_snapshot_timestamp=datetime.now(timezone.utc).isoformat(),
        chain_fetch_timestamp=chain_fetch_timestamp,
        baseline_score=baseline_score,
        per_uid_scores=_build_per_uid_scores(outcome, baseline_score=baseline_score),
        tier_result=tier_result_dict,
    )


def build_and_write_artifact(
    *,
    outcome: EpochScoringOutcome,
    epoch_id: str,
    total_registered_uids: int,
    chain_fetch_timestamp: str,
    base_dir: Path | None = None,
    epoch_type: str = "Search",
    epoch_subtype: str | None = "campaign-level",
    epoch_type_multiplier: float = 1.0,
    baseline_score: float = 0.0,
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
        baseline_score=baseline_score,
    )
    return write_artifact(artifact, base_dir=base_dir)


def _ss58_to_bytes(hotkey_ss58: str) -> bytes:
    """Decode an SS58 hotkey to its 32-byte public key (score_map key type)."""
    from substrateinterface.utils.ss58 import ss58_decode

    return bytes.fromhex(ss58_decode(hotkey_ss58))


@dataclass(frozen=True)
class _DailyRead:
    """The subset of MinerReadResult that `_build_per_uid_scores` reads.

    The daily stream has no on-chain per-miner commit triple (it scores
    off-chain), so we cannot build a real MinerReadResult. The artifact only
    ever touches these four fields, so a duck-typed stand-in is both correct
    and honest about what a daily read carries.
    """

    miner_hotkey: bytes
    miner_uid: int
    ok: bool
    excluded_reason: str | None


def build_daily_artifact(
    *,
    standings: dict[str, float],
    uid_by_hotkey: dict[str, int],
    total_registered_uids: int,
    day: str,
    block_range_start: int | None = None,
    block_range_end: int | None = None,
    baseline_score: float = 0.0,
    registered_hotkeys: list[str] | None = None,
    # epoch_type MUST be one of the CMS's accepted classifications (Search,
    # PMax, Shopping, Video/Display, Consolidation, Championship) — "Daily" is
    # NOT one, and posting it would be rejected. The daily baskets are
    # predominantly search-campaign changes, so "Search" is both accepted and
    # accurate; the stream's daily-ness is carried by the BD- epoch_id, which
    # is what the site keys its daily framing on.
    epoch_type: str = "Search",
    epoch_subtype: str | None = "campaign-level",
    epoch_type_multiplier: float = 1.0,
    chain_fetch_timestamp: str | None = None,
) -> EpochArtifact:
    """Assemble an EpochArtifact for a DAILY basket from the executor's standings.

    The daily stream scores off-chain — the validator's on-chain path filters
    BD- epochs out — so there is no `EpochScoringOutcome` to feed `build_artifact`.
    This reconstructs the minimal outcome the artifact needs (a byte-keyed
    `score_map` and `miner_reads`) from the executor's per-hotkey daily
    standings, then reuses `build_artifact` with the daily [7, 14, 28] horizon
    set. The result flows through the SAME `aggregate()` → `post_epoch_report`
    pipe the weekly path uses, so the site's leaderboard needs no special case.

    Args:
        standings: `hotkey_ss58 → daily score in [0, 1]` (the D13 age-weighted
            average the executor's ledger produces).
        uid_by_hotkey: `hotkey_ss58 → metagraph uid` at chain-fetch time.
        day: the basket day `YYYY-MM-DD`; the epoch id is `BD-<day>`.
        registered_hotkeys: optional full registered set (ss58). Any hotkey in
            this set but NOT in `standings` becomes a disqualification row
            (`not_scored_this_day`) so the published table covers the whole
            field, not only the scored miners.
    """
    score_map: dict[bytes, int] = {}
    reads: list[_DailyRead] = []
    scored: set[str] = set()
    for hotkey_ss58, score in standings.items():
        try:
            hk_bytes = _ss58_to_bytes(hotkey_ss58)
        except Exception:
            # A malformed hotkey should not sink the whole artifact — skip it.
            continue
        scored.add(hotkey_ss58)
        score_map[hk_bytes] = int(round(float(score) * 1_000_000))
        reads.append(_DailyRead(
            miner_hotkey=hk_bytes,
            miner_uid=int(uid_by_hotkey.get(hotkey_ss58, -1)),
            ok=True,
            excluded_reason=None,
        ))
    for hotkey_ss58 in (registered_hotkeys or []):
        if hotkey_ss58 in scored:
            continue
        try:
            hk_bytes = _ss58_to_bytes(hotkey_ss58)
        except Exception:
            continue
        reads.append(_DailyRead(
            miner_hotkey=hk_bytes,
            miner_uid=int(uid_by_hotkey.get(hotkey_ss58, -1)),
            ok=False,
            excluded_reason="not_scored_this_day",
        ))

    outcome = EpochScoringOutcome(
        miner_reads=reads,
        score_map=score_map,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        report_only=True,
    )
    return build_artifact(
        outcome=outcome,
        epoch_id=f"BD-{day}",
        total_registered_uids=total_registered_uids,
        chain_fetch_timestamp=(
            chain_fetch_timestamp or datetime.now(timezone.utc).isoformat()),
        epoch_type=epoch_type,
        epoch_subtype=epoch_subtype,
        epoch_type_multiplier=epoch_type_multiplier,
        baseline_score=baseline_score,
        horizons=DAILY_HORIZONS,
    )
