# HOPE Impact Prediction Subnet (SN21) — Miner Quickstart

**For:** Miners joining the HOPE Impact Prediction Subnet on Bittensor
**Subnet:** SN21 (testnet netuid: 466)
**Schema:** v1.9 (Phase 1 Epoch 1 — Search campaigns, campaign-level actions)
**Horizons:** 7-day and 14-day predictions

---

## 1. What You're Doing

You receive an **episode** — a structured dataset describing a Google Ads account's state before a set of interventions were applied. Your job: predict what happens to performance metrics over the next 7 and 14 days as a result of those interventions.

You output **probabilistic distributions** (P10/P50/P90), not point estimates. You're rewarded for calibrated uncertainty — accurate predictions with honest confidence intervals.

**You don't need to be a Google Ads expert.** Each episode is a structured prediction problem: given features (time series, categorical context, action descriptions), output calibrated distributions. Domain knowledge helps, but the core task is probabilistic prediction.

---

## 2. Getting Started

### Step 1: Install

```bash
git clone https://github.com/ippcteam/tao-discovery.git
cd tao-discovery
pip install -e ".[miner]"
```

### Step 2: Register on the subnet

You **must** register on-chain to participate and earn emissions. Registration burns a small amount of TAO.

```bash
# Create a wallet (if you don't have one)
btcli wallet create --wallet.name my_miner

# Create a hotkey for mining
btcli wallet new_hotkey --wallet.name my_miner --hotkey default --n-words 12

# Register on SN21 (testnet)
btcli subnet register --wallet.name my_miner --hotkey default --netuid 466 --network test

# Verify registration
btcli subnet metagraph --netuid 466 --network test
```

Your hotkey address (e.g. `5Hoo2cR...`) is what you pass to `--hotkey`. The validator identifies you by this address when setting weights on-chain. You can find your hotkey address with:

```bash
btcli wallet list --wallet.name my_miner
```

**Requirements:**
- TAO in your wallet (testnet: ask in Discord for testnet TAO, mainnet: purchase TAO)
- Registration costs ~τ1-3 (burned to the subnet)
- You get a UID on the metagraph after registration

### Step 3: Train on historical data (recommended)

Before predicting on live epochs, train a model on past episodes with known outcomes:

```bash
# Download training data (10 episodes with measured t7/t14 outcomes)
python scripts/generate_training_data.py

# Train an example XGBoost model and compare with baseline
python scripts/train_example_model.py --data-file data/training/training_episodes.json
```

The training set is also bundled in `data/training/training_episodes.json`. Each example has:
- `input` — the full episode payload (what you receive during a live epoch)
- `outcome` — the actual t7/t14 deltas (what really happened)

Use these to train: given input features → predict outcome deltas.

### Step 4: Run the miner

```bash
# Connect to the validator and submit predictions
hope-miner --validator-url https://validator.adtao.io --hotkey YOUR_HOTKEY

# Or specify an epoch explicitly
hope-miner --validator-url https://validator.adtao.io --hotkey YOUR_HOTKEY --epoch WR-2026-W18-PUB-E1

# Or run continuously (polls validator for new epochs every 30s)
hope-miner --validator-url https://validator.adtao.io --hotkey YOUR_HOTKEY --continuous
```

**Validator URL:** `https://validator.adtao.io` — this is the official AdTAO validator.

If no `--epoch` is provided, the miner auto-discovers the current epoch from the validator's `/health` endpoint.

### Step 5: Check your score

After an epoch is scored, check your results:

```bash
# Check scores via the validator API
curl https://validator.adtao.io/epochs/WR-2026-W18-PUB-E1/scores

# Or score yourself offline (exact same scoring the validator uses)
python scripts/score_predictions.py --release WR-2026-W18-PUB-E1 --run-baseline

# Verify the validator didn't cheat (commitment verification)
curl https://validator.adtao.io/epochs/WR-2026-W18-PUB-E1/verification
```

The `/scores` endpoint shows your raw score, null penalty, and final score. The `/verification` endpoint reveals outcomes + salt so you can independently verify the commitment hash.

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
      "cost_delta_pct": {"p10": -5.0, "p50": -2.5, "p90": 1.0},
      "conversions_delta_pct": {"p10": -6.0, "p50": -3.5, "p90": 0.5},
      "efficiency_delta_pct": {"p10": -3.0, "p50": 1.0, "p90": 4.0},
      "goal_miss_probability": 0.30,
      "instability_risk": 0.15
    },
    "14": {
      "cost_delta_pct": {"p10": -6.0, "p50": -3.0, "p90": 0.0},
      "conversions_delta_pct": {"p10": -7.0, "p50": -4.0, "p90": -0.5},
      "efficiency_delta_pct": {"p10": -4.0, "p50": 0.5, "p90": 3.5},
      "goal_miss_probability": 0.35,
      "instability_risk": 0.10
    }
  }
}
```

### What "Delta Percent" Means

All deltas are relative percentage change from the pre-window baseline:

```
delta_pct = ((post_window_avg - pre_window_avg) / pre_window_avg) * 100
```

A prediction of `cost_delta_pct.p50 = -5.0` means "I expect daily cost to drop 5% compared to the 60-day average."

### Validation Rules

| Rule | Constraint | If Violated |
|------|-----------|-------------|
| Quantile ordering | `p10 <= p50 <= p90` | Rejected |
| Both horizons required | `"7"` and `"14"` must be present | Rejected |
| All three metrics required | cost, conversions, efficiency | Rejected |
| Probabilities in [0, 1] | goal_miss and instability_risk | Rejected |
| Minimum interval width | `p90 - p10 >= 0.5` | Penalized |
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

Does your P10-P90 interval capture the actual outcome, without being too wide?

```
IS = (p90 - p10)^1.3 + 2.5 * max(p10 - actual, 0) + 2.5 * max(actual - p90, 0)
```

The `^1.3` exponent means very wide intervals are penalized super-linearly. But missing the interval (actual outside P10-P90) is penalized 2.5x.

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

If more than 40% of your predictions have `|p50| < 1.0` for all metrics, a penalty ramps up:

```
penalty = max(0, (near_zero_fraction - 0.40) / 0.45) * 0.60
final_score *= (1.0 - penalty)
```

At 85%+ near-zero predictions, you lose 60% of your score. Don't predict zero for everything.

### 5.6 Horizon Weighting

For `measurement_resolution = "high"`:
- t7: 40% weight
- t14: 60% weight

14-day predictions matter more than 7-day.

---

## 6. Strategy Guide

### What Wins

1. **Beat the predict-zero baseline.** Your skill score compares you against a model that predicts zero for everything. The bar is low — any signal you extract gives positive skill score.

2. **Use the magnitude field.** For budget changes, `magnitude.spend_change_pct.expected` is the system's estimate. Start from it, then improve.

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
mag = action.magnitude

if action_type == "BUDGET_CHANGE":
    cost_p50 = mag["spend_change_pct"]["expected"]  # e.g., 20.0 (= +20%)
    conv_p50 = cost_p50 * 0.7  # Diminishing returns

elif action_type == "CAMPAIGN_PAUSE":
    cost_p50 = -100.0  # Deterministic
    conv_p50 = -100.0

elif action_type == "TARGET_VALUE_CHANGE":
    # target_vs_current_pct tells you the gap
    pct_change = mag.get("target_vs_current_pct", 0)
    cost_p50 = pct_change * 0.5  # Partial adjustment expected

elif action_type == "BID_STRATEGY_CHANGE":
    cost_p50 = 0.0  # Direction uncertain during learning
    conv_p50 = 0.0
```

### Format and submit

```python
from hope.protocol.prediction import Prediction, HorizonPrediction, QuantilePrediction

spread = 5.0
if spend_cv > 0.3:
    spread *= 1.5  # Wider for volatile accounts

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

1. **Use the training data.** `data/training/training_episodes.json` has 10 examples with known outcomes. `scripts/train_example_model.py` shows how to build a 1.5x baseline model with XGBoost.

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

verification = httpx.get(f"{VALIDATOR_URL}/epochs/{epoch_id}/verification").json()

payload = json.dumps(verification["outcomes"], sort_keys=True) + verification["salt"] + verification["scoring_weights"]
computed = hashlib.sha256(payload.encode()).hexdigest()

assert computed == verification["commitment_hash"], "Validator changed outcomes!"
```

### Score yourself locally

```bash
python scripts/score_predictions.py --release WR-2026-W18-PUB-E1 --run-baseline
```

This runs the exact same scoring pipeline the validator uses.

---

## 10. Validator API Reference

All interaction with the validator is via HTTP:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | None | Current epoch, episode count |
| `GET` | `/training/episodes` | None | Training data (episodes + outcomes) |
| `GET` | `/training/summary` | None | Training data stats |
| `GET` | `/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `POST` | `/epochs/{id}/predictions` | Hotkey | Submit predictions |
| `GET` | `/epochs/{id}/commitment` | None | Commitment proof |
| `GET` | `/epochs/{id}/scores` | None | Per-miner scores (post-scoring) |
| `GET` | `/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |

**Authentication:** Set the `X-Miner-Hotkey` header to your Bittensor hotkey address.

**Validator URL:** `https://validator.adtao.io`

---

## 11. Quick Reference

| Item | Value |
|------|-------|
| Subnet | SN21 (testnet netuid: 466) |
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
| Training data | `data/training/training_episodes.json` (10 examples) |
| Offline scoring | `python scripts/score_predictions.py --run-baseline` |
