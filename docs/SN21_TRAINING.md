# SN21 — Training a model (one path, end to end)

| | |
| :---- | :---- |
| **Audience** | Miners |
| **Status** | Authoritative training path for the daily stream |
| **Last updated** | 2026-08-04 |
| **Prerequisites** | Registered hotkey ([quickstart §2](./miner_quickstart.md)) |

One route from published data to a container the subnet will run. Six steps,
each with the exact command. If a step needs something that does not exist
yet, it says so rather than inventing a CLI.

> **What you are training on, plainly.** The published corpus is
> **weekly-era** (`WR-*` epochs, 7- and 14-day outcomes). The live contract is
> the **daily stream** (`BD-*` baskets, 7/14/**28**-day). Same input schema, so
> a model trained here runs unchanged live — but the 28-day horizon has no
> historical labels until the first daily 28-day outcomes settle on
> **8 September 2026**. Train the 7- and 14-day heads on real data; the 28-day
> head starts as an extrapolation. Everyone is in that position, including us.

---

## 1. Get the data

**Published corpus (in this repo):**

```bash
ls data/episodes/    # inputs  — features you predict on
ls data/outcomes/    # labels  — measured results, published after each deadline
```

Both folders have READMEs describing the shapes:
[episodes](../data/episodes/README.md) · [outcomes](../data/outcomes/README.md).
Files pair by epoch id: `data/episodes/WR-2026-W21-PUB-E1.json` ↔
`data/outcomes/WR-2026-W21-PUB-E1.json`.

**Training bundle (larger and pre-joined):** 3,069 settled episodes with all
three horizons already attached, one JSON object per line.

```bash
curl -L -o SN21_training_bundle.jsonl \
  https://github.com/ippcteam/SN21-adtao/releases/download/training-bundle-2026-08/SN21_training_bundle.jsonl
```

Also on the [releases page](https://github.com/ippcteam/SN21-adtao/releases/tag/training-bundle-2026-08).

A held-out set of 247 episodes is **never** published — it is how a submitted
model gets evaluated on data it cannot have trained on. That set is the only
clean benchmark that exists, and publishing it would destroy it permanently.

---

## 2. Join inputs to labels

Both files carry an `episodes` array; `episode_id` is the join key.

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

The bundle is already joined — one JSON object per line, `{"episode_id",
"input", "labels"}` — so with it, step 2 is just `json.loads` per line.

> **One schema, three wrappings.** Published exports are `v1.9` with fields
> under `input`; the bundle uses the same `input` wrapper; the live daily
> payload is `v2.0` with those fields at top level. Read from `input` when
> present, else top level, and the same code handles all three. See
> [MINER_MODEL_SPEC](./MINER_MODEL_SPEC.md).

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
