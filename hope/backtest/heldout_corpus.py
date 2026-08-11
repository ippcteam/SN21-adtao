"""The admission corpus — episodes the gate judges a model on.

WHY THIS EXISTS

    MINER_MODEL_SPEC §3 promises admission runs against "a held-out
    historical corpus of episodes with settled outcomes". Nothing assembled
    one. `corpus.load_outcomes` reads the weekly release exports, and those
    carry no outcome rows at all, so the gate had truth for nothing.

HELD-OUT MEANS HELD OUT

    A corpus a miner trained on measures memorisation, not skill, and the
    published training bundle is exactly what every miner was told to train
    on. So membership is decided by the ACTION WINDOW, not by episode id:
    ids are not comparable across exports (the bundle numbers episodes
    sequentially, the labelled export uses content hashes), and comparing
    them produced a confident, meaningless "zero overlap".

    A date is comparable across both. Any episode whose action window starts
    after the last window in the published bundle cannot be in it, whatever
    it is called.

EVERY MODEL FACES THE SAME CORPUS

    Selection is deterministic — sorted by episode id, then strided — so two
    models admitted a week apart were judged on identical episodes, and a
    verdict can be recomputed by anyone holding the same export. A random
    sample would make the gate unreproducible and the published verdicts
    unfalsifiable.

SIZE IS A BUDGET, NOT A PREFERENCE

    The published runtime limit is 15 minutes per basket of ~250 episodes,
    and admission runs the container twice (the determinism re-run). The
    default corpus is sized to that budget rather than to the pool, which is
    ~11.5k episodes and would blow it many times over.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from hope.backtest.gate import OutcomeRow

# Matches the published per-basket budget: the gate must cost a miner's
# image no more than a normal day would.
DEFAULT_CORPUS_EPISODES = 250

# The metric the gate calls `efficiency` is CPA in the outcome tables.
EFFICIENCY_KEY = "cpa_delta_pct"


def read_labelled_export(path: str) -> Iterator[dict]:
    """Yield records from a labelled JSONL export, skipping manifest lines."""
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "_manifest" in record:
                continue
            yield record


def episode_id_of(record: dict) -> str | None:
    metadata = (record.get("input") or {}).get("episode_metadata") or {}
    episode_id = metadata.get("episode_id")
    return str(episode_id) if episode_id else None


def action_window_start(record: dict) -> str:
    metadata = (record.get("input") or {}).get("episode_metadata") or {}
    return (metadata.get("action_window_start") or "")[:10]


def published_cutoff(bundle_path: str) -> str:
    """The last action-window date in the published training bundle.

    Everything at or before this may be in miners' hands; everything after it
    demonstrably is not.
    """
    latest = ""
    for record in read_labelled_export(bundle_path):
        # The bundle nests the payload under `input` exactly as the labelled
        # export does, so one accessor serves both.
        start = action_window_start(record)
        if start > latest:
            latest = start
    return latest


def outcome_rows(episode_id: str, settled: dict) -> list[OutcomeRow]:
    """Settled outcomes for one episode -> gate truth rows.

    A horizon with no cost delta is skipped rather than zero-filled: the gate
    scores against truth, and an invented zero is not truth.
    """
    rows = []
    for horizon, values in (settled or {}).items():
        if not isinstance(values, dict):
            continue
        if values.get("cost_delta_pct") is None:
            continue
        try:
            horizon_days = int(horizon)
        except (TypeError, ValueError):
            continue
        rows.append(OutcomeRow(
            episode_id=episode_id,
            horizon_days=horizon_days,
            cost_delta_pct=float(values["cost_delta_pct"]),
            conversions_delta_pct=float(values.get("conversions_delta_pct") or 0),
            efficiency_delta_pct=float(values.get(EFFICIENCY_KEY) or 0),
        ))
    return rows


def select(records: Iterable[dict], cutoff: str,
           limit: int = DEFAULT_CORPUS_EPISODES) -> tuple[list[dict], list[OutcomeRow]]:
    """(episode payloads, outcome rows) for the held-out corpus.

    Payloads are emitted in the LIVE contract shape — episode_id at the top
    level beside the v2.0 blocks — because that is what a miner's container
    reads on a normal day. Gating against a differently-shaped payload would
    admit models that cannot read the real thing.
    """
    eligible = []
    for record in records:
        episode_id = episode_id_of(record)
        if not episode_id:
            continue
        if action_window_start(record) <= cutoff:
            continue          # possibly published; not held out
        rows = outcome_rows(episode_id, record.get("settled_outcomes") or {})
        if not rows:
            continue          # no truth -> nothing to score against
        eligible.append((episode_id, record, rows))

    # Deterministic and spread across the pool: sort, then stride. Taking the
    # first N would bias the corpus toward whichever accounts sort first.
    eligible.sort(key=lambda item: item[0])
    if limit and len(eligible) > limit:
        stride = len(eligible) / float(limit)
        eligible = [eligible[int(i * stride)] for i in range(limit)]

    episodes, outcomes = [], []
    for episode_id, record, rows in eligible:
        payload = {"episode_id": episode_id}
        payload.update(record.get("input") or {})
        episodes.append(payload)
        outcomes.extend(rows)
    return episodes, outcomes


def build(labelled_path: str, bundle_path: str | None = None,
          cutoff: str | None = None,
          limit: int = DEFAULT_CORPUS_EPISODES) -> dict:
    """Assemble the corpus. Either supply a cutoff or the bundle to derive it.

    Refuses to build without a cutoff rather than defaulting to "no
    exclusion": a corpus that silently included the published bundle would
    still produce verdicts, and they would all be wrong in the same
    flattering direction.
    """
    if cutoff is None:
        if not bundle_path:
            raise ValueError(
                "a cutoff or a published-bundle path is required — refusing "
                "to build a corpus that may include episodes miners trained on")
        cutoff = published_cutoff(bundle_path)
    if not cutoff:
        raise ValueError("could not determine a published cutoff date")

    episodes, outcomes = select(read_labelled_export(labelled_path),
                                cutoff, limit)
    return {
        "cutoff": cutoff,
        "episodes": episodes,
        "outcomes": outcomes,
        "episode_count": len(episodes),
        "outcome_row_count": len(outcomes),
    }
