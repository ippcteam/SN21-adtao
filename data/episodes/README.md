# SN21 (AdTAO) — episode inputs (training features)

This folder holds the **weekly-era episode inputs** for each revealed epoch —
the features miners predict on. It's the companion to `data/outcomes/`, which
holds the measured labels. Together they reconstruct trainable
`(input, outcome)` pairs.

**Current training path:** the rich daily-stream bundle v2
([`SN21_rich_training_v2.jsonl`](https://github.com/ippcteam/SN21-adtao/releases/tag/training-v2-2026-08),
29,129 episodes, all change types). How to use it:
[docs/SN21_TRAINING.md](../../docs/SN21_TRAINING.md). The files in this folder
remain the published weekly-era exports.

Each file (`WR-2026-W<week>-…-E1.json`):

```json
{
  "epoch_id": "WR-2026-W21-PUB-E1",
  "schema_version": "v1.9",
  "episodes": [
    { "episode_id": "550981fc663bb26b", "input": { "account_state": {...},
        "action_bundle": {...}, "campaign_metadata": [...], "date_index": [...],
        "episode_metadata": {...}, "pre_window": {...} } }
  ]
}
```

The `input` block is **identical in shape** to the `input` field of the bundled
`data/training/training_episodes.json`, and is the exact payload the validator
serves miners at `GET /v1/epochs/{id}/episodes/{episode_id}`.

**De-identified by design.** Every identifier is a SHA-256 hash
(`customer_id_hash`, `campaign_id_hash`, `entity_id_hash`); everything else is
categorical (`SEARCH`, `ENABLED`, `spend_bucket`), a numeric aggregate
(`*_micros`, deltas, trends), or an enum. No customer names, emails, URLs, or
raw IDs — the export refuses to write if any appear
(`scripts/export_public_episodes.py` PII guard).

## Reconstruct training pairs

Join inputs to labels by `episode_id`. Not every episode has a published
outcome (only those that reached measurement maturity), so join on the
outcomes:

```python
import json
inputs   = {e["episode_id"]: e["input"]
            for e in json.load(open("data/episodes/WR-2026-W21-PUB-E1.json"))["episodes"]}
outcomes = json.load(open("data/outcomes/WR-2026-W21-PUB-E1.json"))["episodes"]

pairs = [
    {"episode_id": o["episode_id"],
     "input":   inputs[o["episode_id"]],
     "outcome": o["validator_only_outcomes"]["outcomes"]}   # {"t7": {...}, "t14": {...}}
    for o in outcomes
    if o["episode_id"] in inputs
]
# `pairs` now matches the bundled training_episodes.json shape (input + outcome).
```

The outcome deltas (`cost_delta_pct`, `conversions_delta_pct`, …) are exactly
what predictions for this epoch are scored against. The labels remain
independently verifiable against the on-chain anchor — see
`data/outcomes/README.md`.
