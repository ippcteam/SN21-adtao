"""Pure builders for the daily-stream training bundle.

Kept side-effect free so the leak-safety that matters can be tested
exhaustively: a training record must carry ONLY the input a miner sees at
prediction time and labels for horizons that have actually settled.
"""
from __future__ import annotations

LABEL_FIELDS = ("cost_delta_pct", "conversions_delta_pct",
                "efficiency_delta_pct", "goal_basis", "finalized_on")
CONTRACT_HORIZONS = ("7", "14", "28")


def build_records(episodes_by_id: dict, outcomes_by_id: dict) -> list:
    """{episode_id: episode_object} + {episode_id: [outcome_row,...]} -> bundle
    records `{episode_id, input, labels}`.

    A record is emitted only when the episode has at least one SETTLED horizon
    (a labelless example is useless for training). `input` is the episode minus
    the validator-only field; `labels` carries one entry per settled horizon.
    """
    records = []
    for eid in sorted(episodes_by_id):
        rows = outcomes_by_id.get(eid) or []
        if not rows:
            continue
        labels = {}
        for r in rows:
            h = r.get("horizon_days")
            if h is None:
                continue
            labels[str(int(h))] = {k: r.get(k) for k in LABEL_FIELDS if k in r}
        if not labels:
            continue
        ep = episodes_by_id[eid]
        inp = {k: v for k, v in ep.items() if k != "validator_only_outcomes"}
        records.append({"episode_id": eid, "input": inp, "labels": labels})
    return records


def validate_leakfree(records: list) -> list:
    """Problems (empty = clean): no record may carry the validator-only field,
    and every label horizon must be a contract horizon."""
    problems = []
    for rec in records:
        if "validator_only_outcomes" in (rec.get("input") or {}):
            problems.append(f"{rec['episode_id']}: validator_only_outcomes leaked into input")
        for h in (rec.get("labels") or {}):
            if h not in CONTRACT_HORIZONS:
                problems.append(f"{rec['episode_id']}: unexpected horizon {h}")
    return problems
