# Impact Prediction Subnet (SN21) — Miner Quickstart

**For:** Miners joining the Impact Prediction Subnet on Bittensor
**Subnet:** SN21 (testnet netuid 466 / mainnet netuid 21)
**Validator URL:** https://validator.adtao.io
**Schema:** v1.9 (Phase 1 Epoch 1 — Search campaigns, campaign-level actions)
**Horizons:** 7-day and 14-day predictions

The example commands in this guide target **testnet 466**. For
mainnet, swap `test` → `finney` and `466` → `21`.

---

## 1. What You're Doing

You receive an **episode** — a structured dataset describing a Google Ads account's state before a set of interventions were applied. Your job: predict what happens to performance metrics over the next 7 and 14 days as a result of those interventions.

You output **probabilistic distributions** (P10/P50/P90), not point estimates. You're rewarded for calibrated uncertainty — accurate predictions with honest confidence intervals.

**You don't need to be a Google Ads expert.** Each episode is a structured prediction problem: given features (time series, categorical context, action descriptions), output calibrated distributions. Domain knowledge helps, but the core task is probabilistic prediction.

---

## 2. Getting Started

### Step 1: Install

```bash
git clone https://github.com/ippcteam/SN21-adtao.git
cd SN21-adtao
pip install -e ".[miner]"
```

### Step 2: Register on the subnet

You **must** register on-chain to participate and earn emissions.
Registration burns a small amount of TAO.

```bash
# Create a coldkey (the keys-of-keys for your wallet)
btcli wallet new_coldkey --wallet.name my_miner
```

`btcli` will prompt you for three things — what to enter:

| Prompt | What to enter |
|---|---|
| `Enter the path to the wallets directory:` | Press **Enter** to accept the default `~/.bittensor/wallets/` |
| `Choose the number of words [12/15/18/21/24]:` | Type `12` and Enter (12 is the default; gives 128 bits of entropy) |
| `Specify password for key encryption:` | Pick a strong password and **save it in your password manager**. You'll need it for every chain transaction. |
| `Retype your password:` | Same password again |

**Scripting / CI tip:** to avoid the wallet-path prompt entirely, pass
`--wallet-path ~/.bittensor/wallets` explicitly on every `btcli wallet`
and `btcli subnet` command. The default works for interactive users; the
explicit flag is the only reliable form under `cron`, `docker run -i`,
or any non-TTY environment where stdin is closed.

**The mnemonic prints to your terminal.** Read it from the screen and
write it directly into your password manager. **Never copy/paste it
anywhere else** — terminal output goes to scrollback, screenshot tools,
clipboard managers. After saving, run `clear` to wipe scrollback.

```bash
# Create a hotkey under that coldkey (the per-purpose signing key)
btcli wallet new_hotkey --wallet.name my_miner --wallet.hotkey default
```

Same flow — choose `12` words, save the mnemonic in your password
manager, run `clear`. The hotkey is stored unencrypted (no password
prompt), which is intentional — it's for automated signing.

```bash
# Register on SN21 (testnet 466)
btcli subnet register --netuid 466 \
    --wallet.name my_miner --wallet.hotkey default \
    --subtensor.network test
```

This shows the registration cost (~0.0005 τ on testnet), asks
`Do you want to continue? [y/n]:` (type `y`), then prompts
`Enter your password:` — that's the **coldkey password** from the
first command above.

```bash
# Verify registration
btcli subnet metagraph --netuid 466 --subtensor.network test
```

The Rich-formatted table truncates the SS58 column to `5…` on most
terminals. To confirm your full hotkey is registered, drop into Python:
```python
import bittensor as bt
mg = bt.Subtensor(network='test').metagraph(netuid=466)
print('your hotkey present:', '<your-ss58>' in mg.hotkeys)
print('your UID:', mg.hotkeys.index('<your-ss58>') if '<your-ss58>' in mg.hotkeys else 'NOT REGISTERED')
```

> **Validator-side delay.** The validator's authentication cache refreshes
> the metagraph every ~2 minutes. Brand-new registrations may see a
> `403 Hotkey not registered on subnet` from the validator for up to
> that long after the on-chain registration lands. If you're hitting
> `hope-miner` immediately after registering, wait 2-3 minutes and
> retry. (The cache used to be set-once-at-startup, which permanently
> rejected new miners until validator restart — this is fixed.)

**Need testnet TAO?** Get free testnet TAO from the Bittensor Discord
faucet bot:
- Join: https://discord.gg/bittensor
- Channel: `#testnet-faucet`
- Command: `!faucet <your-coldkey-ss58>` (paste your coldkey ss58 from
  `btcli wallet list --wallet.name my_miner`)

**SN21-specific community channel:**
[adtao SN21 #general](https://discord.com/channels/799672011265015819/1489651673944297472)
— for protocol questions, announcements, and operator support.

Registration on testnet currently costs ~0.0005 TAO; mainnet pricing
varies — see `btcli subnet register` output for the live cost.

### Step 3: Generate an ed25519 signing key (required, one-time)

The validator's chain reads your AES-encrypted predictions and
verifies an ed25519 inner signature against the hotkey↔key binding
you publish on chain. You need a separate ed25519 PEM file:

```bash
python scripts/sn21_keys.py generate \
    --role miner \
    --output ~/sn21-miner.pem
# Prints the ed25519 public key (safe to share). The private PEM
# stays on disk at ~/sn21-miner.pem (mode 0600). Save the file or
# its contents in a password manager — losing it means you can't
# sign new submissions.
```

### Step 4: Register the ed25519 binding on chain (one-time)

Tells the chain: "ed25519 public key X belongs to my hotkey." Without
this, validators reject your prediction signatures.

```bash
python scripts/sn21_keys.py register \
    --role miner --network test --netuid 466 \
    --wallet-name my_miner --wallet-hotkey default \
    --key ~/sn21-miner.pem
# Prompts:
#   "Submit registration commit? [y/N]" → type 'y'   (or pass --yes)
#   coldkey password (the one you set in Step 2)
# On success, prints `success: True`, `block: <N>`, and `extrinsic_hash:
# 0x...`. Save the block number — useful if you ever need to re-audit
# the registration with `verify_epoch.py --block-hash`.
```

**Scripting / cron note:** add `--yes` to skip the interactive prompt.
The script also auto-confirms when stdin isn't a TTY, so it works under
`docker run -i` and similar non-interactive invocations without
hanging.

### Step 5: Train on historical data (recommended)

Before predicting on live epochs, train a model on past episodes
with known outcomes:

```bash
# Train the example XGBoost model on the bundled sample dataset
python scripts/train_example_model.py \
    --data-file data/training/training_episodes.json
```

The training set is bundled at `data/training/training_episodes.json`
(an operator-harvested snapshot of episodes with measured t7/t14 outcomes).
Each example has:
- `input` — the full episode payload (what you receive during a live epoch)
- `outcome` — the actual t7/t14 deltas (what really happened)

> **Sample data caveat — important.** The bundled dataset is harvested
> from real episodes and is the same shape as a live epoch, but the
> sample is small and the action-type mix is skewed toward whichever
> categories matured first. Counts as of the most recent refresh:
>
> | Action type | Bundled sample | Live distribution |
> |---|---:|---|
> | `BID_STRATEGY_CHANGE` | ~79% | varies by epoch |
> | `CAMPAIGN_PAUSE` | ~11% | varies by epoch |
> | `BUDGET_CHANGE` | ~11% | varies by epoch |
> | `TARGET_VALUE_CHANGE` | 0% | not yet matured |
>
> The bundled set is enough to validate your pipeline end-to-end (the
> trainer + scorer round-trips on every action type it contains). To
> train a model that's competitive across the full action enum, harvest
> additional examples from live epochs: snapshot `/episodes_batch`
> during the prediction window and capture `/verification` after the
> reveal. Coverage of `TARGET_VALUE_CHANGE` improves as that action
> type matures in production.
>
> The launch action enum is the four types in
> `hope/constants.py:LAUNCH_ACTION_TYPES`
> (`BUDGET_CHANGE`, `BID_STRATEGY_CHANGE`, `TARGET_VALUE_CHANGE`,
> `CAMPAIGN_PAUSE`). `CAMPAIGN_ENABLE` is deprecated and will not
> appear in live epochs.
>
> Outcome `efficiency_delta_pct` is `null` on many horizons when the
> measured CPA / ROAS is undefined (zero conversions or zero cost).
> The scorer skips that metric for those horizons and averages over
> the remaining (cost, conversions) deltas — see
> `docs/SN21_REWARD_MECHANISM.md` for the full rule.

### Step 6: Run the miner

The complete command for testnet 466:

```bash
hope-miner --validator-url https://validator.adtao.io \
    --wallet-name my_miner --wallet-hotkey default \
    --epoch WR-2026-W18-PUB-E1 \
    --bt-network test --netuid 466 \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --archive-tier-3 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/sn21-miner.pem
```

**What each flag does:**

| Flag | Purpose |
|---|---|
| `--validator-url` | Where the miner fetches episodes from. Points at the operator's validator HTTP API. |
| `--epoch` | Current epoch identifier. Look it up at https://validator.adtao.io/health → `current_epoch` field. |
| `--archive-tier-2` | Operator-redundancy archive — your AES_ct lands here too. For testnet bootstrap, use the operator's archive at `adtao-deploy.onrender.com`. |
| `--archive-tier-3` | Your "self-archive" URL — this is the URL announced on chain inside your bundled commit. For testnet bootstrap, sharing the operator's archive is fine. For production, run your own with `hope-archive-server` (see §10). |
| `--ed25519-key-file` | The PEM you generated in Step 3. |

**Expected output (success):**

```
Loaded wallet my_miner/default: 5G7Aweu9tqG3QxV...
Results:
  ok: True
  failure_reason: None
  bundle_block: <chain block where commit landed>
  reveal_round: 28457883
  uploads_ok: [(2, True), (3, True)]
```

The miner submits exactly **one TimelockEncrypted commit per epoch** —
a CBOR bundle carrying { AES key K, sha256(ciphertext), self-archive
URL }. The chain auto-decrypts the bundle at the drand reveal round
(after the prediction deadline passes), then validators read all three
values atomically.

If the run fails, see §11 (Troubleshooting).

### Step 7: Check your score

The validator scores miners post-deadline at the next subnet
tempo step (~72 min after the bundle's reveal round on testnet 466).

```bash
# Live API: scores once scoring has run for this epoch
curl https://validator.adtao.io/v1/epochs/WR-2026-W18-PUB-E1/scores

# Independently re-derive scoring from chain state alone (testnet)
python scripts/verify_epoch.py \
    --epoch-id WR-2026-W18-PUB-E1 \
    --network test --netuid 466 \
    --validator-hotkey 5ChwLaQa5TRhboq47eHmw4AHfg6AbGUL4jB26mUxM5n1Zsm1 \
    --tier-2-base https://adtao-deploy.onrender.com

# Score yourself offline against the bundled sample data
python scripts/score_predictions.py \
    --training-data data/training/training_episodes.json \
    --run-baseline
```

This runs the exact same scoring code the validator uses, against the
10-example sample dataset bundled in the repo.

> Note: you might see references to `HOPE_API_KEY` and a `--release`
> mode in the script's `--help`. Those are operator-only — `HOPE_API_KEY`
> is the operator's credential to the data backend that produces
> releases, and there's no way (or reason) for a miner to obtain one.
> Your real epoch score lives at `/v1/epochs/{id}/scores` on the validator
> once scoring runs; offline self-scoring uses `--training-data`.

If `/scores` returns `409 Scoring not yet complete`, the validator's
on-chain scoring run hasn't fired yet — wait for the next subnet tempo
step (or check `https://validator.adtao.io/health`).

---

## 3. What You Receive (Episode Structure)

Each episode has 6 sections. For Phase 1, all episodes are Search campaigns with campaign-level actions.

### 3.1 Episode Metadata

```json
{
  "episode_id": "37b646dcdf02bd6e",
  "schema_version": "v1.9",
  "phase": 1,
  "epoch": 1,
  "coverage_status": "baseline",
  "measurement_resolution": "high",
  "action_window_start": "2026-03-31T22:10:41Z",
  "action_window_end": "2026-03-31T22:10:41Z",
  "outcome_horizons_days": [7, 14],
  "environmental_context": {
    "seasonality_index": 1.0,
    "auction_pressure_trend": 0.0,
    "spend_volatility_cv": 0.22,
    "week_over_week_delta": 0.05
  }
}
```

### 3.2 Account State

```json
{
  "customer_id_hash": "2de25609e1b19b7b...",
  "currency_code": "USD",
  "account_type": "lead",
  "spend_bucket": "mid",
  "tracking_reliability": "high",
  "goal": {
    "type": "CPA",
    "target": 25.00,
    "deviation": -0.08,
    "tolerance": 0.10
  }
}
```

`coverage_status = "trust_enriched"` episodes also include `archetypes`, `guardrails`, `health`, and `portfolio_context` — use these when available, they give stronger signal.

### 3.3 Pre-Window (60 days of daily data)

```json
{
  "campaigns": {
    "<campaign_id_hash>": {
      "campaign_type": "SEARCH",
      "bid_strategy_type": "TARGET_CPA",
      "impressions": [15000, 14800, ...],
      "clicks": [450, 430, ...],
      "cost_micros": [22500000, 21500000, ...],
      "conversions": [12.5, 11.8, ...],
      "conversion_value_micros": [625000000, ...],
      "impression_share": [0.72, 0.71, ...]
    }
  },
  "account_aggregates": {
    "avg_daily_spend_micros": 75000000,
    "avg_daily_conversions": 38.5,
    "avg_cpa_micros": 19480000,
    "avg_roas": 3.12,
    "spend_cv": 0.18,
    "conversion_cv": 0.25,
    "impression_share_trend": -0.02,
    "cpc_trend": 0.03
  }
}
```

All arrays are length 60 (60-day pre-window), indexed by `date_index`.

### 3.4 Action Bundle

```json
{
  "window_start": "2026-03-31T22:10:41Z",
  "window_end": "2026-03-31T22:10:41Z",
  "actions": [
    {
      "type": "BUDGET_CHANGE",
      "scope": "campaign",
      "impact_class": "capacity",
      "risk_tier": "medium",
      "reversibility": "reversible",
      "blast_radius": {
        "tier": "parent_equivalent",
        "impact_ratio": 1.0,
        "spend_contribution": 1.0
      },
      "magnitude": {
        "spend_change_pct": {"min": 15.0, "max": 25.0, "expected": 20.0},
        "certainty": 0.85
      }
    }
  ],
  "bundle_summary": {
    "action_count": 1,
    "has_destructive": false,
    "has_improvement": true,
    "max_risk_score": 50,
    "net_capacity_delta": 0.20
  }
}
```

### 3.5 Action Types (Phase 1)

| Type | What Happened | Key Signal |
|------|--------------|------------|
| `BUDGET_CHANGE` | Daily budget increased/decreased | `magnitude.spend_change_pct` — the % budget change |
| `BID_STRATEGY_CHANGE` | Bidding strategy switched (e.g., Manual CPC → Target CPA) | `magnitude.from/to` — expect 7-14 day learning period volatility |
| `TARGET_VALUE_CHANGE` | tCPA/tROAS target adjusted (same strategy, different target) | `magnitude.metric`, `magnitude.new_target`, `magnitude.target_vs_current_pct` |
| `CAMPAIGN_PAUSE` | Campaign paused | `magnitude.spend_change_pct = -100` — deterministic |

---

## 4. What You Output

### Prediction Schema

For each episode, predict across both horizons (7 and 14 days):

```json
{
  "episode_id": "37b646dcdf02bd6e",
  "horizons": {
    "7": {
      "cost_delta_pct": {"p10": -0.05, "p50": -0.025, "p90": 0.01},
      "conversions_delta_pct": {"p10": -0.06, "p50": -0.035, "p90": 0.005},
      "efficiency_delta_pct": {"p10": -0.03, "p50": 0.01, "p90": 0.04},
      "goal_miss_probability": 0.30,
      "instability_risk": 0.15
    },
    "14": {
      "cost_delta_pct": {"p10": -0.06, "p50": -0.03, "p90": 0.0},
      "conversions_delta_pct": {"p10": -0.07, "p50": -0.04, "p90": -0.005},
      "efficiency_delta_pct": {"p10": -0.04, "p50": 0.005, "p90": 0.035},
      "goal_miss_probability": 0.35,
      "instability_risk": 0.10
    }
  }
}
```

### What "Delta" Means

All deltas are fractional ratios of relative change from the
pre-window baseline:

```
delta = (post_window_avg - pre_window_avg) / pre_window_avg
```

A prediction of `cost_delta_pct.p50 = -0.05` means "I expect daily
cost to drop 5% compared to the 60-day average." The field name
`*_delta_pct` is a historical artifact — values are fractional ratios
(e.g. `-0.05`), **not** percent integers.

Outcome values on the chain and from the validator use the same
fractional convention, so predictions and outcomes are on identical
scales for scoring purposes.

### Validation Rules

| Rule | Constraint | If Violated |
|------|-----------|-------------|
| Quantile ordering | `p10 <= p50 <= p90` | Rejected |
| Both horizons required | `"7"` and `"14"` must be present | Rejected |
| All three metrics required | cost, conversions, efficiency | Rejected |
| Probabilities in [0, 1] | goal_miss and instability_risk | Rejected |
| Minimum interval width | `p90 - p10 > MIN_INTERVAL_WIDTH` (see `hope/constants.py`; expressed in fractional units) | Counted as null per the penalty rule below |
| No NaN/Inf | All values finite | Rejected |

### Efficiency Delta

`efficiency_delta_pct` depends on the account's goal type:
- If `goal.type` contains "CPA" or "cost" → this is ΔCPA (negative = improvement)
- If `goal.type` contains "ROAS" → this is ΔROAS (positive = improvement)

---

## 5. How You're Scored

Your score is a weighted combination of four components:

### 5.1 Quantile Accuracy (50% weight)

How close are your P10/P50/P90 to the actual outcome? Uses pinball loss + CRPS.

```
L_tau(actual, predicted) = tau * max(actual - predicted, 0)
                          + (1-tau) * max(predicted - actual, 0)
```

Averaged across all three metrics (cost, conversions, efficiency).

### 5.2 Calibration (20% weight)

Does your P10-P90 interval capture the actual outcome, without being
too wide? The simplified formula is:

```
IS = (p90 - p10)^1.3 + 2.5 * max(p10 - actual, 0) + 2.5 * max(actual - p90, 0)
```

The implementation in `hope/scoring/components/calibration.py` adds
two refinements:

1. **Scale-normalisation** — IS is divided by `max(abs(actual), 1.0)`
   so a wide interval around a small actual outcome doesn't get
   penalised the same as a wide interval around a large actual.
2. **Low-resolution episodes** — for `measurement_resolution = "low"`
   episodes (where the source data is noisier), the calibration
   weight is reduced via the
   `CALIBRATION_LOW_RES_REDUCTION = 0.50` constant.

Read `hope/scoring/components/calibration.py` for the exact code; the
`^1.3` exponent (`CALIBRATION_WIDTH_EXPONENT`) and the `2.5` miss
multiplier (`CALIBRATION_MISS_MULTIPLIER`) are pinned in
`hope/constants.py` and unit-tested in
`tests/unit/scoring/test_scoring_components.py`.

**Sweet spot:** Capture ~80% of actuals with the narrowest possible interval.

### 5.3 Directional Accuracy (15% weight)

Did you predict the right direction?

- `1.0` if `sign(p50) == sign(actual)`
- `0.0` if wrong direction and `|actual| > 1.0`
- `0.5` if actual is near zero (ambiguous)

### 5.4 Goal Accuracy (15% weight)

Brier score on goal miss probability:

```
goal_accuracy = 1.0 - (predicted_probability - actual_miss)^2
```

Proper scoring rule — the optimal strategy is honest probability reporting.

### 5.5 Null Penalty

A prediction counts as **escaping null** for a given episode when AT
LEAST ONE of its three metrics (cost / conversions / efficiency)
satisfies BOTH of:

- `|p50| > NEAR_ZERO_THRESHOLD` (signal in the point estimate)
- `(p90 - p10) > MIN_INTERVAL_WIDTH` (meaningful uncertainty range)

The thresholds are pinned in `hope/constants.py`:

- `NEAR_ZERO_THRESHOLD = 2.0` (deci-percent units, i.e. p50 must move
  more than ±2% from zero)
- `MIN_INTERVAL_WIDTH = 3.0`

If more than 40% of your epoch's predictions fail this test (i.e.
all three metrics are simultaneously near-zero with narrow intervals),
a penalty ramps up:

```
penalty = max(0, (near_zero_fraction - 0.40) / 0.45) * 0.60
final_score *= (1.0 - penalty)
```

At 85%+ near-null predictions you lose 60% of your score. The exact
rule lives in `hope/scoring/null_penalty.py` and is unit-tested in
`tests/unit/scoring/test_scoring_components.py`.

### 5.6 Horizon Weighting

For `measurement_resolution = "high"`:
- t7: 40% weight
- t14: 60% weight

14-day predictions matter more than 7-day.

---

## 6. Strategy Guide

### What Wins

1. **Beat the predict-zero baseline.** Your skill score compares you against a model that predicts zero for everything. The bar is low — any signal you extract gives positive skill score.

2. **Derive a direction signal from the pre-window for `BUDGET_CHANGE`.**
   In live epochs the `action.magnitude.spend_change_pct.expected` field
   is often `null` for budget changes (only `TARGET_VALUE_CHANGE` reliably
   populates magnitude). For `BUDGET_CHANGE`, derive your own signal from
   the pre-window time series:
   ```python
   import numpy as np
   cost = np.array(pre_window["campaigns"][cid]["cost_micros"], dtype=float)
   recent = cost[-14:][cost[-14:] > 0]
   early  = cost[:14][cost[:14] > 0]
   trend_pct = ((recent.mean() - early.mean()) / max(early.mean(), 1)) * 100
   # trend_pct ≈ percent change in daily spend over the last 14 days vs
   # the first 14 days; use sign + magnitude as a P50 prior.
   ```
   Then combine with `bundle_summary.has_destructive` / `has_improvement`
   to refine the sign. The baseline model in `hope/miner/models/baseline.py`
   shows this pattern.

3. **Use the training data.** Run `python scripts/train_example_model.py` — it shows how to extract 19 features, train XGBoost, and score 1.5x better than baseline.

4. **Calibrate your intervals.** Wide intervals are safe but penalized (^1.3 convex penalty). Narrow intervals score well when right but get hammered when wrong (2.5x miss penalty). Target 80% coverage.

5. **Campaign pauses are deterministic.** `CAMPAIGN_PAUSE` → cost drops 100%, conversions drop 100%. Predict this with tight intervals and high certainty.

6. **Bid strategy changes are volatile.** 7-14 day learning period means noisy outcomes. Use wider intervals for t7, narrower for t14 as learning stabilizes.

7. **Target value changes have known direction.** `TARGET_VALUE_CHANGE` includes `target_vs_current_pct` — telling you exactly how far the new target is from current performance.

### What Loses

1. **Predicting zero for everything.** The null penalty catches this at 40%+ near-zero predictions.

2. **Extremely wide intervals.** The convex width penalty (^1.3) means doubling your interval width costs more than double.

3. **Ignoring the goal field.** The goal_miss Brier score is 15% of your total. Use `goal.deviation` and `goal.tolerance` to estimate miss probability.

4. **Same prediction for every episode.** Episodes vary dramatically — a 20% budget increase vs. a campaign pause have opposite outcomes.

---

## 7. Baseline Model Walkthrough

The baseline model (`hope/miner/models/baseline.py`) demonstrates the approach:

### Parse the episode

```python
from hope.protocol.episode import Episode

ep = Episode.model_validate(payload)
action = ep.action_bundle.actions[0]
action_type = action.type  # "BUDGET_CHANGE", "CAMPAIGN_PAUSE", etc.
agg = ep.pre_window.account_aggregates
spend_cv = agg.spend_cv  # Volatility signal
```

### Extract magnitude estimates

```python
# Predictions are in the same fractional units as outcome deltas:
# -0.05 means -5%, -1.0 is the floor (full removal).
mag = action.magnitude

if action_type == "BUDGET_CHANGE":
    # `spend_change_pct.expected` is in percent on the episode payload
    # (e.g. 20 means +20%). Outcome deltas are fractional, so divide
    # by 100 to convert to the matching scale.
    spend_pct = mag.get("spend_change_pct") or {}
    expected = (spend_pct.get("expected") if isinstance(spend_pct, dict) else spend_pct) or 0.0
    cost_p50 = float(expected) / 100.0
    conv_p50 = cost_p50 * 0.7  # Diminishing returns

elif action_type == "CAMPAIGN_PAUSE":
    cost_p50 = -1.0  # Deterministic — spend goes to zero
    conv_p50 = -1.0

elif action_type == "TARGET_VALUE_CHANGE":
    # target_vs_current_pct is also in percent on the payload
    pct_change = mag.get("target_vs_current_pct", 0) or 0
    cost_p50 = float(pct_change) / 100.0 * 0.5  # Partial adjustment expected

elif action_type == "BID_STRATEGY_CHANGE":
    cost_p50 = 0.0  # Direction uncertain during learning
    conv_p50 = 0.0
```

### Format and submit

```python
from hope.constants import MIN_INTERVAL_WIDTH
from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction

# Spread is half-width in fractional units. 0.05 = ±5 percentage points
# around the p50. Floor with MIN_INTERVAL_WIDTH so narrow predictions
# don't trip the null detector (see hope/constants.py).
spread = 0.05
if spend_cv > 0.3:
    spread *= 1.5  # Wider for volatile accounts
spread = max(spread, MIN_INTERVAL_WIDTH)

prediction = Prediction(
    episode_id=ep.episode_metadata.episode_id,
    miner_id="my_miner",
    submitted_at=datetime.now(timezone.utc),
    horizons={
        "7": HorizonPrediction(
            cost_delta_pct=QuantilePrediction(p10=cost_p50 - spread, p50=cost_p50, p90=cost_p50 + spread),
            conversions_delta_pct=QuantilePrediction(p10=conv_p50 - spread, p50=conv_p50, p90=conv_p50 + spread),
            efficiency_delta_pct=QuantilePrediction(p10=-spread, p50=0.0, p90=spread),
            goal_miss_probability=0.3,
            instability_risk=0.15,
        ),
        "14": HorizonPrediction(
            # 14-day: dampened, slightly wider
            ...
        ),
    },
)
```

---

## 8. Improving Beyond Baseline

Ordered by expected impact:

1. **Train a model and run it.** Use the bundled training data + reference
   trainer, save the result, and load it into the miner runner:
   ```bash
   # Train + save (one line, end-to-end)
   python scripts/train_example_model.py \
       --data-file data/training/training_episodes.json \
       --save-model ~/sn21-miner-trained.pkl

   # Run the miner with the saved model (substitute for --model baseline)
   hope-miner --model trained --model-file ~/sn21-miner-trained.pkl \
       --validator-url https://validator.adtao.io \
       --wallet-name my_miner --wallet-hotkey default \
       --epoch WR-2026-W18-PUB-E1 \
       --bt-network test --netuid 466 \
       --archive-tier-2 https://adtao-deploy.onrender.com \
       --archive-tier-3 https://adtao-deploy.onrender.com \
       --ed25519-key-file ~/sn21-miner.pem
   ```
   The bundled trainer gets **~1.5× the baseline score on the bundled
   10-example sample dataset** — driven mostly by the calibration term,
   not point-estimate accuracy. The reference XGBoost regressor
   underperforms the mean predictor on R² (`Cost R² ≈ -1.6`,
   `Conv R² ≈ -13.0`); that's expected with N=10. Don't read negative
   R² as a broken trainer — the score that matters in production is
   the validator's full epoch score, where calibration carries 20% and
   compounds rapidly with more training data.

2. **Learn portfolio redistribution.** The interaction between constraint_level, redistribution_likelihood, and action type is the biggest gap in baseline estimates.

3. **Model temporal evolution.** Predict different values for t7 vs t14 based on how effects compound — learning periods stabilize, trends accumulate.

4. **Learn interval calibration.** After initial training, check what % of actuals fall in your P10-P90. Target 80%. Too wide? Narrow. Too narrow? Widen.

5. **Specialise by action type.** Budget changes, bid strategy changes, target value changes, and pauses have fundamentally different outcome distributions.

---

## 9. Verification

After each epoch, the validator reveals outcomes and scoring weights. You can verify your scores independently:

### Check the commitment

Before the epoch started, the validator committed to outcomes by publishing a hash:

```
commitment_hash = SHA256(outcomes_json + salt + weights_json)
```

After scoring, verify:

```python
import hashlib, json, httpx

verification = httpx.get(f"{VALIDATOR_URL}/v1/epochs/{epoch_id}/verification").json()

payload = json.dumps(verification["outcomes"], sort_keys=True) + verification["salt"] + verification["scoring_weights"]
computed = hashlib.sha256(payload.encode()).hexdigest()

assert computed == verification["commitment_hash"], "Validator changed outcomes!"
```

### Score yourself locally

```bash
python scripts/score_predictions.py \
    --training-data data/training/training_episodes.json --run-baseline
```

This runs the exact same scoring pipeline the validator uses, against
the 10-example sample dataset bundled in the repo. Public miners do
not need any operator credentials for this — the `--release` /
`HOPE_API_KEY` path in the script is operator-only.

---

## 10. Validator API Reference

All interaction with the validator is via HTTP at
**`https://validator.adtao.io`**.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | None | Current epoch, episode count |
| `GET` | `/training/episodes` | Hotkey | Training data (episodes + outcomes) |
| `GET` | `/training/summary` | Hotkey | Training data stats |
| `GET` | `/v1/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/v1/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/v1/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `GET` | `/v1/epochs/{id}/commitment` | None | Commitment proof |
| `GET` | `/v1/epochs/{id}/scores` | None | Per-miner scores (post-scoring) |
| `GET` | `/v1/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |

Predictions are submitted **on chain** via your bundled
TimelockEncrypted commit (Layer 9.B), not via an HTTP POST. The
`hope-miner` CLI handles this for you.

**Authentication.** When `REQUIRE_SIGNATURES=true` on the validator
(the launch default), every authenticated request must carry three
headers:

| Header | Value |
|---|---|
| `X-Miner-Hotkey` | Bittensor SS58 hotkey of the requesting miner |
| `X-Miner-Nonce` | Unix timestamp (numeric string), must be within ±NONCE_EXPIRY_SECONDS of validator clock |
| `X-Miner-Signature` | Hex-encoded sr25519 signature over the canonical signed message |

The canonical signed message is constructed from method + path +
hotkey + nonce + body-hash, and is implemented in
`hope/validator/api/auth.py:verify_miner`. The `hope-miner` CLI
constructs this signature automatically when run with a wallet
(`--wallet-name`). Custom HTTP clients should mirror that exact
construction; reading the `verify_miner` function is the source of
truth.

If `REQUIRE_SIGNATURES=false` (development only), only `X-Miner-Hotkey`
is required.

---

## 11. Troubleshooting

### `verify_epoch.py` exits with `bundle has no plaintext yet`

You ran the verifier between bundle submission and the chain's drand
auto-decrypt. The bundle's plaintext is empty until the next subnet
tempo step processes the drand pulse for your `reveal_round`. Wait
~30-72 minutes after the miner success and retry. The verifier no
longer crashes with `CBORDecodeEOF` — it returns a clean reason.

### `verify-reg` says "no Raw payload at the latest block"

Substrate's `Commitments::CommitmentOf` is **single-slot, last-write-wins**
per `(netuid, hotkey)`. After your first `hope-miner` bundle commit,
the slot stores a TimelockEncrypted bundle and the registration
`Raw{N}` payload is no longer at chain head. Either:
- Pass `--block-hash <0x...>` of the original `sn21_keys.py register`
  extrinsic (printed on success — capture it from the `block:` /
  `extrinsic_hash:` lines), against an **archive node** RPC, OR
- Trust the validator's metagraph as the proof of registration.

This is by design — verifiers capture the binding once at hotkey-first-seen
and store the block hash for future audit.

### "do I need my coldkey password every time?"

No. Only `btcli subnet register` and other coldkey-signed extrinsics
prompt for it. The actual mining flow (`hope-miner`) signs with the
**hotkey** only — hotkey files are stored unencrypted by default and
no password prompt fires during prediction submission.

### `btcli ... --quiet` returns no output — did it work?

`btcli` swallows its success output under `--quiet`, which is
indistinguishable from a silent crash. We don't recommend `--quiet`
in this quickstart — it makes "did the registration land?" hard to
answer. If you've already run a `--quiet` command, verify by:
```bash
btcli wallet overview --wallet.name my_miner --subtensor.network test
# Coldkey balance debited by ~0.0005 τ ⇒ registration landed
btcli subnet metagraph --netuid 466 --subtensor.network test
# Your hotkey ss58 in the list ⇒ visible to validators
```

### `bittensor.MaxRetriesExceeded` / `keepalive ping timeout`

The public testnet RPC at `wss://test.finney.opentensor.ai:443` is
load-balanced across multiple nodes. If the connection times out or
metadata fetch hangs (~30s), the chain is briefly slow or syncing.
Retry the same `hope-miner` command 2-3 times — each invocation opens
a fresh WebSocket and may land on a different backend.

### `wallet hotkey is not ed25519; supply --ed25519-key-file`

You forgot `--ed25519-key-file ~/sn21-miner.pem` on the command. The
inner_sig requires the ed25519 PEM you generated in Step 3.

### `Subtensor returned: SpaceLimitExceeded(Module)`

Your hotkey hit the per-pallet-epoch byte budget on the Commitments
pallet (~3100 bytes per `(netuid, hotkey)`). One miner submission
costs ~600B, and the budget resets per pallet-epoch. If you get this
error:
- Don't retry immediately — wait for the next pallet-epoch
- Or spin up a fresh miner hotkey

This typically only hits if you're scripting many submissions in a
short window. Normal once-per-epoch operation never sees this.

### `Subnet mechanism 466.0 does not exist`

`--bt-network` and `--netuid` are inconsistent. For testnet use
`--bt-network test --netuid 466`; for mainnet use
`--bt-network finney --netuid 21`.

### `validator returns 403 Hotkey not registered on subnet`

You haven't run `btcli subnet register` yet, or the registration
hasn't propagated to the metagraph. Re-run:
```bash
btcli subnet metagraph --netuid 466 --subtensor.network test
```
Confirm your hotkey appears in the list.

### `validator returns 422 missing X-Miner-Hotkey`

The miner CLI builds these headers automatically. If you're seeing
this, you're hitting the API directly with `curl` without signing.
Use the CLI, or implement the auth headers per §10.

### `hope-miner runs but reveal never happens`

After submitting your bundle, the chain auto-decrypts at the configured
drand round (default ~1 hour after submission, via
`--blocks-until-reveal 300`). The chain only processes drand pulses at
the next subnet tempo step (~72 min on testnet 466), so the bundle's
plaintext shows up in `RevealedCommitments` shortly after the next
tempo step boundary. To check directly:
```python
import bittensor as bt
sub = bt.Subtensor(network='test')
revealed = sub.substrate.query("Commitments", "RevealedCommitments",
                                [466, "<your-hotkey-ss58>"])
print(revealed)
```

### `archive POST returns 5xx`

The Tier-2 / Tier-3 archive endpoint is briefly down. Retry the miner
run; the archive is restartable and recovers quickly. If persistent,
report in the SN21 Discord channel.

---

## 12. Running your own Tier-3 archive (production)

For testnet bootstrap, sharing the operator's archive at
`https://adtao-deploy.onrender.com` for both Tier-2 and Tier-3 is
fine. For production, you should run your own:

```bash
# On a small VM or a Render Web Service
hope-archive-server --port 8080 --base-dir /var/data/sn21-archive
```

Your archive must:
- Accept POSTs to `/archive/{epoch}/{miner_identity}/{sha256_hex}`
- Serve the same path back via GET
- Be HTTPS-reachable from the public internet

Then point the miner at your URL via `--archive-tier-3
https://miner.yourdomain.example`. The chain commit will announce
this URL inside your bundled TimelockEncrypted plaintext, and
validators will fetch from there at scoring time.

---

## 13. Quick Reference

| Item | Value |
|------|-------|
| Subnet | SN21 (testnet netuid 466 / mainnet netuid 21) |
| Validator URL | https://validator.adtao.io |
| Operator archive (Tier-2 / Tier-3 fallback) | https://adtao-deploy.onrender.com |
| Schema version | v1.9 |
| Horizons | 7-day, 14-day |
| Campaign types | SEARCH only (Phase 1) |
| Action types | BUDGET_CHANGE, BID_STRATEGY_CHANGE, TARGET_VALUE_CHANGE, CAMPAIGN_PAUSE |
| Pre-window length | 60 days |
| Prediction format | P10/P50/P90 quantiles per metric per horizon |
| Metrics | cost_delta_pct, conversions_delta_pct, efficiency_delta_pct |
| Probabilities | goal_miss_probability, instability_risk |
| Scoring weights | Quantile 50%, Calibration 20%, Directional 15%, Goal 15% |
| Horizon weights | t7=40%, t14=60% (high resolution) |
| Null penalty | Ramps from 40% to 85% near-zero predictions, max 60% penalty |
| **Weekly cadence** | Mining: Monday noon EST → Sunday midnight EST (6.5 days) |
| **Scoring window** | Sunday midnight EST → Monday noon EST (12 hours) |
| Chain commit per epoch | ONE TimelockEncrypted bundle (CBOR `{K, sha256(ct), url}`); decrypted by chain at the drand reveal round |
| Bittensor pallet budget | 3,100 bytes per (netuid, hotkey) per pallet-epoch — comfortably above one bundle (~110 B) |
| Training data | `data/training/training_episodes.json` (10 examples) |
| Offline scoring | `python scripts/score_predictions.py --training-data data/training/training_episodes.json --run-baseline` |
| Public verifier | `python scripts/verify_epoch.py --epoch-id <id> --validator-hotkey <ss58>` |
| Faucet (testnet TAO) | https://discord.gg/bittensor → `#testnet-faucet` channel |
