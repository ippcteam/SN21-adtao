# SN21 — Training a model (one path, end to end)

| | |
| :---- | :---- |
| **Audience** | Miners |
| **Status** | Authoritative training path for the daily stream |
| **Last updated** | 2026-08-20 |
| **Prerequisites** | Registered hotkey ([quickstart §2](./miner_quickstart.md)) |

One route from published data to a container the subnet will run. Six steps,
each with the exact command. If a step needs something that does not exist
yet, it says so rather than inventing a CLI.

> **What you are training on, plainly.** The current published corpus is
> **rich daily-stream v2** (`training-v2-2026-08`): reconstructed real account
> change windows whose outcome periods have already elapsed, with measured
> 7- and 14-day labels and 28-day labels where they have matured. The live
> contract is the **daily stream** (`BD-*` baskets, 7/14/**28**-day). The first
> *live* 28-day daily outcomes still settle on **8 September 2026**; v2 already
> carries 28-day labels on the historical windows that have elapsed. The
> expanded change types are already in this bundle **and** in live daily
> baskets from **20 August 2026**. A model trained only on budget and
> campaign-pause will miss that population.
>
> Two things are deliberately excluded from both the bundle and the live
> baskets: IP-blocklist hygiene churn, and budget moves under $5/day.
> Label coverage in v2: every episode carries 7-day outcomes, ~4 in 5 carry
> 14-day, and about half carry 28-day — a share that grows with each refresh
> as more windows mature.

---

## 1. Get the data

**Rich training bundle v2 (current, train on this):** 29,129 reconstructed
daily-stream episodes, one JSON object per line. First line is a `_manifest`.
Windows span **2026-06-15 – 2026-08-04**. Labels are measured results, not
simulations.

```bash
curl -L -o SN21_rich_training_v2.jsonl \
  https://github.com/ippcteam/SN21-adtao/releases/download/training-v2-2026-08/SN21_rich_training_v2.jsonl
```

Also on the [releases page](https://github.com/ippcteam/SN21-adtao/releases/tag/training-v2-2026-08).

Each record is `{"episode_id", "input": {"payload", "transition_key"}, "labels"}`.
`input.payload` is what you train on:

| Field | What it is |
| :---- | :---- |
| `pre_window` | 60-day baseline (`avg_daily_cost_micros`, conversions, conversion value, `baseline_days` / `active_days`) plus an 8-week `weekly_series` (`cost_micros`, `conversions`; a week may be `null`) |
| `account_state.archetypes` | Detected archetypes in the 30 days before the change — present on **84%** of records |
| `action_bundle.actions` | Each change: `type`, `ts`, `op`, `entity_type`, `client_type`, plus `from_value` / `to_value` or `from_strategy` / `to_strategy` when they apply |
| `action_bundle.bundle_summary` | `action_count`, `action_types`, `transition_key`, `source_mix` (`user` / `system` from the Google Ads client type) |
| `labels` | Settled deltas vs the 60-day baseline: `cost_delta_pct`, `conversions_delta_pct`, `cpa_delta_pct`, `conversion_value_delta_pct` at horizons **7** (28,592), **14** (23,384), **28** (14,849) |

> **Which efficiency label is graded — the basis map.** Every record carries
> both `cpa_delta_pct` and `conversion_value_delta_pct`, but each episode is
> graded on only **one**, the account's own optimization basis. The map from
> `episode_id` to that basis is published for this exact corpus at
> `https://hope-bittensor-api.onrender.com/v1/daily/training-basis-map`
> (`{episode_id, efficiency_basis, source, guarded}`; `efficiency_basis` is
> `cpa` or `conversion_value`). It covers the training-v2 episodes; the daily
> map at `/v1/daily/episode-basis-map` covers settled daily-receipt episodes
> and does not overlap this corpus. From the next refresh, the basis also ships
> inline on each record as `account_state.goal_basis`. See
> [MINER_MODEL_SPEC](./MINER_MODEL_SPEC.md#account_stategoal_basis--which-efficiency-metric-you-are-scored-on).

**Change types.** Not only budget and pause. About 16.9k records are
**composite** windows — several changes land together; predict the net effect.

| Type | Episodes containing it | Signal |
| :---- | ---: | :---- |
| `BUDGET_CHANGE` | 15,422 | `from_value` / `to_value` (daily budget) |
| `NEGATIVE_KEYWORD_ADD` | 6,720 | criterion create |
| `CAMPAIGN_PAUSE` | 3,465 | campaign status → paused |
| `TARGET_VALUE_CHANGE` | 2,801 | new tCPA / tROAS |
| `CRITERION_CHANGE` | 2,434 | campaign / ad-group criterion update |
| `BID_STRATEGY_CHANGE` | 1,721 | `from_strategy` / `to_strategy` |
| `ASSET_CHANGE` | 804 | campaign asset |
| `NEGATIVE_KEYWORD_REMOVE` | 602 | criterion remove |
| Also present | — | `GEO_CHANGE`, `SCHEDULE_CHANGE`, `AUDIENCE_CHANGE`, `KEYWORD_ADD` / `KEYWORD_REMOVE`, `AD_CREATE` / `AD_PAUSE` / `AD_ENABLE` / `AD_REMOVE`, `ADGROUP_*`, `DEMOGRAPHIC_CHANGE`, `DEVICE_TARGETING_CHANGE`, `PLACEMENT_CHANGE`, `CRITERION_*` |

Campaign identifiers are one-way hashes. This release is
**campaign-resolvable** changes; keyword-level (ad-group-scoped) episodes
follow in a later release. Outcome deltas naturally skew negative: many real
changes intentionally cut volume. `source_mix` treats web/mobile as `user`
and scripts/API/rules as `system`.

A held-out evaluation set is **never** published, and any training episode
that overlapped it has been removed. That set is the only clean benchmark
that exists; publishing it would destroy it permanently.

**Weekly-era bundle (superseded for training):** the 10,791-record
[`SN21_training_bundle.jsonl`](https://github.com/ippcteam/SN21-adtao/releases/tag/training-bundle-2026-08)
is still available. It is almost entirely `BUDGET_CHANGE` / `CAMPAIGN_PAUSE`
and predates the 72-hour episode cap now live on the daily stream. Use v2.

**Weekly-era exports (in this repo):** still useful as a second, narrower
corpus. Pair by epoch id:
`data/episodes/WR-2026-W21-PUB-E1.json` ↔
`data/outcomes/WR-2026-W21-PUB-E1.json`.
Shapes: [episodes](../data/episodes/README.md) ·
[outcomes](../data/outcomes/README.md).

```bash
ls data/episodes/    # weekly-era inputs
ls data/outcomes/    # weekly-era labels
```

---

## 2. Join inputs to labels

The rich bundle is already joined. Skip the first line (`_manifest`), then
`json.loads` per line. Features live under `input.payload`; labels under
`labels` keyed `"7"` / `"14"` / `"28"`.

```python
import json

records = []
with open("SN21_rich_training_v2.jsonl") as f:
    for i, line in enumerate(f):
        obj = json.loads(line)
        if i == 0 and "_manifest" in obj:
            continue
        records.append((obj["input"]["payload"], obj["labels"]))

print(f"{len(records)} (payload, labels) pairs")
```

Weekly-era files still join on `episode_id` (both carry an `episodes` array):

```python
import json, glob, os

pairs = []
for ep_path in sorted(glob.glob("data/episodes/*.json")):
    out_path = os.path.join("data/outcomes", os.path.basename(ep_path))
    if not os.path.exists(out_path):
        continue                                   # not every epoch is revealed
    eps = {e["episode_id"]: e["input"]
           for e in json.load(open(ep_path))["episodes"]}
    for o in json.load(open(out_path))["episodes"]:
        if o["episode_id"] in eps:
            pairs.append((eps[o["episode_id"]], o))

print(f"{len(pairs)} (input, outcome) pairs")
```

> **One schema, several wrappings.** Weekly-era exports are `v1.9` with
> fields under `input`. The rich v2 bundle wraps the same ideas as
> `input.payload` (`schema_version: v2.0`). The live daily payload is v2.0
> with those fields at top level. Read `input.payload` when present, else
> `input`, else the top level, and the same code handles all three. See
> [MINER_MODEL_SPEC](./MINER_MODEL_SPEC.md). The container still emits
> `cost_delta_pct` / `conversions_delta_pct` / `efficiency_delta_pct`; map
> the v2 `cpa_delta_pct` (or conversion-value) label onto the efficiency
> head from the account's own goal.

---

## 3. Train and export weights

Any framework. What matters is the output shape: per episode, per horizon
(7/14/28), monotone **p10 ≤ p50 ≤ p90** for `cost_delta_pct`,
`conversions_delta_pct` and `efficiency_delta_pct`.

A worked starting point ships in the repo:

```bash
python scripts/train_example_model.py \
    --data-file data/training/training_episodes.json
```

> This is a **pipeline check on 10 episodes**, not a competitive model. It
> proves your loop runs; it will not clear the admission gate.

Scoring rewards calibrated *ranges*, not point guesses — quantile accuracy is
50% of the score and interval coverage another 10%. A confident, narrow, wrong
band scores far worse than an honest wide one. See
[SN21_SCORING](./SN21_SCORING.md).

---

## 4. Bake the weights into a container

The sandbox has **no network**, so everything must be inside the image.

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY model_weights.pkl predict.py ./     # weights INSIDE the image
ENTRYPOINT ["python", "-u", "predict.py"]
```

Contract: one episode JSON per line on **stdin** → one prediction JSON per line
on **stdout**. Budget: 1 GB RAM, 15 CPU-minutes per basket.

---

## 5. Smoke-test locally, exactly as the subnet runs it

```bash
docker build -t sn21-miner:v1 .

# One real episode in, one prediction out
python - <<'EOF' > /tmp/one_episode.ndjson
import json
d = json.load(open("data/episodes/WR-2026-W21-PUB-E1.json"))
print(json.dumps(d["episodes"][0]["input"]))
EOF

docker run --rm -i --network none --memory 1g \
    sn21-miner:v1 < /tmp/one_episode.ndjson
```

`--network none` matters: it is how the subnet runs you, and it is the fastest
way to find a model that secretly phones home.

What to check: exactly one output line per input line; valid JSON; all three
horizons; p10 ≤ p50 ≤ p90 on every metric. **A container that exits 0 while
printing nothing usable counts as zero predictions** — it is recorded as a
missed day, not an error.

---

## 6. Score yourself before you ship

```bash
python scripts/score_predictions.py \
    --training-data data/training/training_episodes.json --run-baseline
```

`--run-baseline` is the part that matters: admission requires **beating the
naive baseline** and covering **≥90%** of what the reference model covers. If
you do not beat the baseline locally, you will not pass the gate.

> **Accuracy caveat.** `score_predictions.py` implements the weekly-era
> scorer. It is directionally right — same four components, same quantile
> emphasis — but the live daily formula weights quantile 0.50 / coverage 0.10 /
> direction 0.15 / goal 0.15, renormalised over 0.90, and scores direction and
> goal on the **account's own goal metric**. Use it to catch regressions, not
> to predict your exact live score.

---

## Then what

Publish and commit the digest: [quickstart §5](./miner_quickstart.md#5-publish-your-image-and-commit-the-digest).
Confirm it landed: [quickstart §5b](./miner_quickstart.md). How you get paid:
[SN21_REWARDS](./SN21_REWARDS.md).
