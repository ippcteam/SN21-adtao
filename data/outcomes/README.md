# SN21 (AdTAO) — revealed epoch outcomes

This folder holds the **measured outcomes** for each scored epoch, published after
the submission deadline so miners can train on them and anyone can audit them.

Each file (`WR-2026-W<week>-…-E1.json`) contains, per episode:

```json
{
  "epoch_id": "WR-2026-W21-PUB-E1",
  "episodes": [
    {
      "episode_id": "550981fc663bb26b",
      "validator_only_outcomes": {
        "outcomes": {
          "t7":  { "cost_delta_pct": -0.34, "conversions_delta_pct": -0.51,
                   "cpa_delta_pct": 0.36, "conversion_value_delta_pct": -0.03,
                   "measured_at": "2026-05-15T11:06:19Z" },
          "t14": { ... }
        }
      }
    }
  ]
}
```

`t7` / `t14` are the 7- and 14-day measurement horizons. The deltas are the
measured percentage changes the validator scores predictions against.

> **Era note.** This corpus is the **weekly era** (`WR-*` epochs, 7- and
> 14-day horizons), which concluded on 3 August 2026. It remains the largest
> published training set and the input schema is unchanged. On the daily
> stream, each day's settled outcomes are published inside the day's signed
> receipt instead — see [SN21_VERIFYING.md](../../docs/SN21_VERIFYING.md).

**Only `episode_id` + the measured outcomes are published** — no customer
identifiers, account context, or campaign payloads.

## Verify it's the real, unaltered truth (no API key needed)

The operator commits the canonical hash of each epoch's outcome set on chain
(`sn21-outcomes-v1`, under the outcome-signer hotkey). The file here reproduces
that exact hash, so you can confirm it's byte-for-byte what was anchored — and
identical for every validator:

```bash
python scripts/verify_public_outcomes.py \
  --file data/outcomes/WR-2026-W21-PUB-E1.json \
  --outcome-signer <OUTCOME_SIGNER_SS58> --network finney --netuid 21
# -> OK ✓ outcomes match the on-chain anchor
```

A pass means the operator cannot have altered these outcomes after the fact or
served a different set to anyone.

## Use them to train

Join these outcomes to the episodes you predicted on (or fetched) by
`episode_id` — the outcomes are the labels, your episode features are the inputs.
Predictions for each epoch are scored against exactly these measured deltas.
