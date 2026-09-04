# SN21 — Miner model specification (container contract)

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners |
| **Status** | Authoritative for the daily stream |
| **Last updated** | 2026-08-20 |
| **Related** | [miner_quickstart.md](./miner_quickstart.md) · [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_TRAINING.md](./SN21_TRAINING.md) · [SN21_MODEL_PRIVACY.md](./SN21_MODEL_PRIVACY.md) |

This is the contract between your model and the subnet: what you ship, how it
is executed, and what it must emit. Scoring is in
[SN21_SCORING.md](./SN21_SCORING.md); emissions are in
[SN21_REWARDS.md](./SN21_REWARDS.md). When image bytes are closed vs
released is in [SN21_MODEL_PRIVACY.md](./SN21_MODEL_PRIVACY.md).

---

## 1. What you submit

A container image (OCI / Docker). You commit its **digest** on chain:

```
sn21-model:v1:<repo>@sha256:<64hex>
```

The digest pins the exact bits the subnet will run. Updating your model means
pushing a new image, committing the new digest, and re-entering the admission
gate.

## 2. Execution contract

Your entrypoint reads **one episode payload per line on stdin** and writes
**one prediction per line on stdout**:

```json
{"episode_id": "...", "horizons": {"7": {...}, "14": {...}, "28": {...}}}
```

A line counts as a prediction for an episode only when `horizons` carries a
non-empty block for **every** horizon listed in that episode's
`episode_metadata.outcome_horizons_days`. A missing, empty or null block makes
the line an abstention for that episode: it produces no scored entry, and the
episode is uncovered under the absence rule. Extra horizons are ignored.

Each horizon carries monotone `p10` / `p50` / `p90` for `cost_delta_pct`,
`conversions_delta_pct` and `efficiency_delta_pct`, plus `goal_miss_probability`
and `instability_risk` in `[0, 1]`.

> `goal_miss_probability` and `instability_risk` are **accepted but not
> scored** — no ground truth exists for them. Spend your model's capacity on
> the delta metrics.

**Runtime limits:**

| Limit | Value |
| :---- | :---- |
| Network | **None** — the sandbox runs `--network=none`; everything you need ships in the image |
| Memory | **1 GB** |
| CPU time | **15 minutes** per daily basket (~250 episodes) |
| Filesystem | Read-only root |

Exceeding a limit aborts that day's run: no scores for the day.

> **Amended 2026-08-24 — the absence penalty.** This document previously
> said a missed day "costs you evidence rather than incurring a separate
> penalty." That is no longer true, and here is why, openly: under that
> rule a miner absent on hard days kept a spotless average built on easy
> days and held first place while every participant's score moved. From
> the published effective date, every episode of a subnet-run day you do
> not return a scoreable prediction for enters your **standing** as a
> zero (floor **0.00**) carried at the **full episode weight** — the same
> standing mass a covered episode contributes — so covering an episode at
> any honest score always leaves a standing at least as high as dropping
> it. (Corrected same-day from an initially published 0.30 floor, before
> any charge was ever applied; see SN21_REWARDS for the open correction.)
> There is no coverage threshold to duck under and no exit: you are
> charged for every uncovered day as long as you hold a board position,
> so going quiet bleeds your standing until you return or your entries
> age out of the 35-day window. Days the **subnet** fails to run charge
> nobody. Every applied penalty is published beside the receipts
> (`/v1/daily/absence-penalties`), so your standing stays reproducible
> from public documents, penalties included. Full details in
> [SN21_REWARDS.md](./SN21_REWARDS.md).

### Determinism is required, and it is checked

Your container must give the **same answer to the same question every time**.
This is not a recommendation: at admission the same sample of episodes is put
to your image twice, and **any disagreement rejects the submission** with the
reason `nondeterministic`.

The rule exists because the subnet publishes that anyone can replay a scored
day and reproduce the result. A model that answers differently on a replay
makes that promise false for every day it ran, and makes two validators
disagree about what you predicted.

**This is the rejection most likely to surprise you**, because a
non-deterministic model usually passes local testing. The common causes:

- Python hash randomisation changing set or dict iteration order
- iterating a `set` where order reaches the output
- an unseeded random number generator
- timestamps, elapsed time, or anything derived from the clock
- floating-point reduction order that varies with thread count — this one
  passes every single-threaded test and fails on a different machine
- GPU non-determinism from atomics or auto-tuned kernel selection

Check before you submit. Run your image three times over the same basket and
compare the output byte for byte:

```bash
for i in 1 2 3; do
  docker run --rm -i --network=none --memory=1g --read-only \
    <your-image> < basket.jsonl > run$i.jsonl
done
cmp run1.jsonl run2.jsonl && cmp run2.jsonl run3.jsonl && echo "deterministic"
```

Abstaining is always allowed — declining an episode is not the same as
contradicting yourself, and a re-run that fails to start is treated as a
liveness matter, not as non-determinism.

### Schema versions

Several surfaces, one schema:

| Surface | Shape |
| :---- | :---- |
| Historical exports under `data/episodes/` | Weekly-era export — episode fields at the top level |
| Weekly-era training bundle | `{"episode_id", "input": {...}, "labels": {...}}` |
| **Rich training bundle v2** | `{"episode_id", "input": {"payload": {...}, "transition_key"}, "labels": {...}}` — first line is a `_manifest` |
| **Live daily contract** | **Episode payload v2.0** — id, metadata, account state, pre-window series, action bundle with magnitudes |

Field names and meanings are identical where they overlap. Training code
should read `input.payload` when present, else `input`, else the top level.
The reference model's flattened field list is a convenience view of the same
schema, not a third format. The v2 bundle and its change types are in
[SN21_TRAINING.md](./SN21_TRAINING.md).

New per-action fields in v2 payloads, worth modelling: `from_value` /
`to_value` on budget and target changes (the change's actual size),
`client_type` (Google's record of who made the change), and a
`source_mix` summary on the bundle (user = web/mobile interface,
system = scripts, API, rules). `transition_key` is low-cardinality by
design — families like `BUDGET:up_large`, `NEGATIVE_KEYWORD_ADD`, and
`COMPOSITE:<dominant>+<n>` for mixed 72-hour windows, where the
prediction is the net effect of the listed actions together.

### `account_state.goal_basis` — which efficiency metric you are scored on

Each episode is scored on the account's own optimization goal, resolved and
**frozen at reveal** so it cannot move after you predict. `account_state`
carries it:

```json
"goal_basis": {
  "basis": "cpa | conversion_value",
  "source": "configured | taxonomy | default",
  "guarded": false,
  "frozen_at": "2026-08-22T14:05:13.896676+00:00"
}
```

- **basis** — which measured column supplies the efficiency delta the scorer
  compares your prediction against: `cpa` (cost-per-acquisition) or
  `conversion_value` (a ROAS / value goal).
- **source** — which rung resolved the basis: `configured` (the account has
  an explicit goal metric, e.g. Target CPA or Target ROAS — this wins
  outright), `taxonomy` (no explicit goal, but the account's vertical is a
  value vertical → `conversion_value`), or `default` (neither → `cpa`).
- **guarded** — `true` means the account *intended* `conversion_value` but had
  no conversion-value baseline in the pre-window, so the basis was vetoed down
  to `cpa`; `source` still records the intended rung.

The basis is **per episode, not a fixed account attribute**: if an advertiser
changes a campaign's optimization goal, later episodes flip basis. Model on
the resolved `goal_basis` in each payload, not on a per-campaign assumption.

Published daily receipts also record the resolved basis per outcome under the
field name `efficiency_basis`. Two basis maps are published, both keyed by
`episode_id` (= `candidate_key[:16]`, the same id the bundle and receipts use)
and both carrying `{episode_id, efficiency_basis, source, guarded}`:

| Map | Route | Covers |
| :---- | :---- | :---- |
| Daily | `/v1/daily/episode-basis-map` | Episodes that have appeared in a settled **daily receipt**. |
| Training | `/v1/daily/training-basis-map` | Every episode in the published **rich training-v2** corpus. |

The two populations do not overlap: the daily map is recent settled receipts,
the training map is the reconstructed historical windows in the bundle. If you
train on the bundle, use the **training** map. Fetch either from the same host
as the receipts, e.g.
`https://hope-bittensor-api.onrender.com/v1/daily/training-basis-map`. From the
**next** training-bundle refresh, the resolved basis also ships inline on every
record as `account_state.goal_basis`, so no join is needed.

## 3. Admission — the backtest gate

A newly committed digest runs against a **held-out historical corpus** of
episodes with settled outcomes. Admission requires **beating the published
naive baseline** (persistence: zero-change medians with corpus-calibrated
spreads) on the gate metric — quantile pinball blended with direction
accuracy, 70/30 — and reaching at least 90% of the reference model's
coverage. Every model update re-runs the gate, and gate results are published.

## 4. The daily cycle once admitted

**Wall clock:** a **midnight EST** cut-off each day is the day boundary for
the basket and prediction-lock cycle.

| Day | What happens |
| :---- | :---- |
| Day 0 | Account changes occur; they become that day's basket |
| Day 1 | The subnet executes your container against the basket in the sandbox; its output is locked as your predictions |
| Day 15 / 22 / 36 | Each (episode, horizon) is scored exactly once at 7 / 14 / 28 days plus a 7-day settling window |

Scores fold into your episode-age-weighted standing, weights follow the
published curve, and the champion seat changes only under the promotion rule
in [SN21_REWARDS.md](./SN21_REWARDS.md).

Every scored day is published as a signed receipt you can recompute — see
[SN21_VERIFYING.md](./SN21_VERIFYING.md).

## 5. Liveness and chronic failure

A crash, timeout or budget breach means no scores that day. Sustained failure
is bounded by a published policy:

| Rule | Value |
| :---- | :---- |
| Strike | A day your container was run and delivered nothing usable, where the fault is yours |
| Eviction | **5 strikes within a rolling 14 days** |
| Return | After **7 days**, on a clean run |

Three things are explicitly **not** strikes:

- A weak, wrong or missing **prediction**. Accuracy is priced in your
  standing; liveness is a separate axis and stays one.
- A day the **subnet did not run**, or ran an empty basket.
- A failure on the **operator's** side. Those days count neither against you
  nor for you.

The policy is published ahead of enforcement, and any change to how it
affects weights is announced through the miner channels before it takes
effect.
