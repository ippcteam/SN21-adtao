# SN21 — Scoring (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.3 |
| **Audience** | Miners |
| **Status** | Authoritative for daily-stream scoring |
| **Last updated** | 2026-08-04 |
| **Update independently of** | [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_STAKING.md](./SN21_STAKING.md) · [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |

This document explains **how your predictions are scored**. It does **not** explain who gets paid or how much — that is [SN21_REWARDS.md](./SN21_REWARDS.md).

---


> **Reading a basket's name.** `BD-2026-08-03` contains the changes that
> happened on **3 August** and was delivered to miners on the morning of
> **4 August**. Every scoring clock in this document counts from the basket's
> own date, not its delivery date.

## In one line

Each day a basket of real account changes is revealed; you predict outcomes; later, each prediction is scored **exactly once** against a settled outcome; those scores feed a **per-prediction moving average** that is your standing.

## The daily clock (scoring side)

**Miner submission wall clock:** **midnight EST** each day. That is the day boundary for the daily basket and prediction lock.

| When | What happens |
| :---- | :---- |
| **Each day (midnight EST cut-off)** | A fresh basket ships. Your predictions for that basket are locked **before** any outcome exists. |
| **Horizon + settle** | Outcomes are measured at **7 / 14 / 28 days**, then held for a **7-day settling window** so late conversions are included. |
| **Settle day** | That (episode, horizon) is scored once. The score enters your standing. It is never re-scored. |

Settle date for a horizon:

`action_window_end + 1 day + horizon days + 7-day settling window`

Examples (from action-window end):

| Horizon | First scoreable (approx.) |
| :---- | :---- |
| 7-day | ~day 15 |
| 14-day | ~day 22 |
| 28-day | ~day 36 |

## What you predict

Unchanged from launch: probabilistic distributions (P10 / P50 / P90) for the published metrics at each horizon, plus goal-related fields required by the episode schema.

Horizons in the daily stream: **7, 14, and 28 days**.

## Per-prediction score (episode × horizon)

Each finalised (episode, horizon) receives a score in **[0, 1]** from four components:

> **Production formula (what the settle scorer actually runs).** The published
> component weights are below; in production, calibration's interval-coverage
> half is the computable part, so the live blend is **quantile 0.50 · coverage
> 0.10 · direction 0.15 · goal-p50 0.15, renormalised over 0.90**
> (`hope/scoring/settle_day_flow.py:score_entry_v2`). Direction and goal are
> scored on the **account's goal metric** (basis frozen at reveal). The
> `goal_miss_probability` and `instability_risk` fields are **not scored** —
> no ground truth exists for them.

| Component | Weight |
| :---- | :---- |
| Quantile accuracy (pinball on P10/P50/P90) | **50%** |
| Calibration | **20%** |
| Directional accuracy | **15%** |
| Goal-metric accuracy | **15%** |

Missing a prediction for a settled episode does **not** insert a zero score. It simply adds no evidence that day — your standing is diluted only by having fewer scored entries.

## Account attrition (stop spending / leave the network)

Some accounts stop spending or disconnect after a change is already in a basket. Scoring keeps whatever has **already settled**, and **explicitly drops** every horizon that has not yet settled — with a recorded reason. This is the same for every miner: no reward and no penalty on the dropped horizons.

| Rule | Effect |
| :---- | :---- |
| Horizon whose settle day is **already past** when the account leaves / goes unmeasurable | **Still scored** once, as usual. Never re-opened. |
| Horizon that has **not yet settled** | **Dropped** from scoring for all miners. Recorded as censored with a reason (e.g. `left_system`, `spend_inactive`). Not a zero. |
| Longer horizons after a censor | Also dropped. Censoring a horizon implies censoring all later unsettled horizons on that episode. |
| Horizon blend weights | Already-scored horizons keep their published blend weight. Dropped horizons contribute no standing entry and are **not** renormalised onto the survivors. |

**Example.** Action-window ends day 0. Horizons first score around day 15 / 22 / 36. The account leaves the network on **day 18**:

| Horizon | Settle (approx.) | What happens |
| :---- | :---- | :---- |
| 7-day | ~day 15 | Already settled → **scored and kept** |
| 14-day | ~day 22 | Not yet settled → **dropped** (`left_system`) |
| 28-day | ~day 36 | Not yet settled → **dropped** (`left_system`) |

Your standing keeps the 7-day entry only. That is absent evidence for 14- and 28-day — the same shape as a missing prediction, except the reason is account attrition, not a miner miss.

Accounts that have already left or gone quiet are also filtered out of **future** baskets (they no longer contribute new qualifying changes). Attrition above is only about episodes that were already revealed.

## How horizons blend into your standing

When a horizon finalises, it enters your standing with a **horizon blend weight** that depends on measurement resolution. Across an episode’s three horizons, blend weights sum to **1.0**.

| Resolution | 7-day | 14-day | 28-day |
| :---- | :---- | :---- | :---- |
| High | 0.20 | 0.35 | 0.45 |
| Medium | 0.15 | 0.30 | 0.55 |
| Low | 0.00 | 0.20 | 0.80 |

Longer horizons weigh more; noisier (lower) resolution shifts weight further toward 28-day.

## Your standing (moving average)

Your published standing is an **episode-age-weighted** mean of scored entries — **not** a per-day average.

- Each scored (episode, horizon) enters at its settle day.
- Weight decays with age: half-life **12 days**, window **35 days**.
- A thin Saturday contributes fewer entries and therefore less influence — automatically. No special weekend rule.

Cold-start evidence floors (used when placing you for emissions — see rewards doc):

| Floor | Predictions in window | Meaning |
| :---- | :---- | :---- |
| Placement | **250** | Minimum evidence before you can earn under the curve |
| Full standing | **1000** | Full standing confidence |

---

## Worked examples

### Example A — one (episode × horizon) score

Suppose the four component scores for a single high-resolution 7-day prediction are:

| Component | Raw score | Weight | Contribution |
| :---- | ---: | ---: | ---: |
| Quantile | 0.80 | 0.50 | 0.40 |
| Calibration | 0.70 | 0.20 | 0.14 |
| Directional | 1.00 | 0.15 | 0.15 |
| Goal | 0.60 | 0.15 | 0.09 |
| **Episode×horizon score** | | | **0.78** |

That **0.78** is what enters the standing machinery for this finalisation (before horizon blend weight).

### Example B — one episode across three horizons (high resolution)

Same episode, three horizons finalise on different days. High-resolution blend weights: 0.20 / 0.35 / 0.45.

| Horizon | Settle day | Score | Blend weight | Standing entry weight |
| :---- | :---- | ---: | ---: | ---: |
| 7-day | Day 15 | 0.78 | 0.20 | 0.20 |
| 14-day | Day 22 | 0.72 | 0.35 | 0.35 |
| 28-day | Day 36 | 0.70 | 0.45 | 0.45 |
| **Episode total** | | | **1.00** | **1.00** |

Each row is a separate standing entry on its settle day. Nothing is re-scored when a later horizon lands.

### Example C — standing with age decay (half-life 12 days)

As of **Day 36**, you have only the three entries above (ages 21, 14, and 0 days). Age weight = `0.5 ** (age / 12)`:

| Entry | Score | Blend | Age (days) | Age weight | Effective weight (`blend × age`) | Weighted score |
| :---- | ---: | ---: | ---: | ---: | ---: | ---: |
| 7-day | 0.78 | 0.20 | 21 | 0.5^(21/12) ≈ **0.297** | 0.20 × 0.297 ≈ **0.059** | 0.78 × 0.059 ≈ **0.046** |
| 14-day | 0.72 | 0.35 | 14 | 0.5^(14/12) ≈ **0.445** | 0.35 × 0.445 ≈ **0.156** | 0.72 × 0.156 ≈ **0.112** |
| 28-day | 0.70 | 0.45 | 0 | **1.000** | 0.45 × 1.000 = **0.450** | 0.70 × 0.450 = **0.315** |
| **Totals** | | | | | **≈ 0.665** | **≈ 0.473** |

**Standing** ≈ `0.473 / 0.665` ≈ **0.71**.

A thin day with fewer entries would simply add fewer rows — there is no per-day average step.

### Example D — missing a prediction

If you never submitted the 14-day prediction for this episode, the Day-22 row never appears. Your standing uses only the 7-day and 28-day entries. That is **not** a zero score for 14-day; it is absent evidence (and less total weight toward the placement floors).

### Example E — account leaves after 7-day has settled

Same high-resolution episode as Example B. The account disconnects on **day 18**.

| Horizon | Settle day | Result |
| :---- | :---- | :---- |
| 7-day | Day 15 | **Scored** (0.78 × blend 0.20) — already settled before disconnect |
| 14-day | Day 22 | **Dropped** — reason `left_system`; no standing entry for anyone |
| 28-day | Day 36 | **Dropped** — reason `left_system`; no standing entry for anyone |

Standing uses only the 7-day row. Blend weight stays **0.20** for that entry; the 0.35 / 0.45 from the dropped horizons are not redistributed.

## What scoring does **not** decide

- **Who earns emissions** and the share curve → [SN21_REWARDS.md](./SN21_REWARDS.md)
- **Alpha stake / hold requirements** → [SN21_STAKING.md](./SN21_STAKING.md)
- **Cutover dates and bridge payouts** → [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)

## Related

- Why daily: [SN21_WHY_DAILY.md](./SN21_WHY_DAILY.md)
- Model / container contract: [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)
