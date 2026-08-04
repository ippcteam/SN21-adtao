# SN21 — Scoring (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners |
| **Status** | Authoritative for daily-stream scoring |
| **Last updated** | 2026-08-04 |
| **Update independently of** | [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_STAKING.md](./SN21_STAKING.md) · [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |

This document explains **how your predictions are scored**. It does **not** explain who gets paid or how much — that is [SN21_REWARDS.md](./SN21_REWARDS.md).

---

## In one line

Each day a basket of real account changes is revealed; you predict outcomes; later, each prediction is scored **exactly once** against a settled outcome; those scores feed a **per-prediction moving average** that is your standing.

## The daily clock (scoring side)

| When | What happens |
| :---- | :---- |
| **Each day** | A fresh basket ships. Your predictions for that basket are locked **before** any outcome exists. |
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

| Component | Weight |
| :---- | :---- |
| Quantile accuracy (pinball on P10/P50/P90) | **50%** |
| Calibration | **20%** |
| Directional accuracy | **15%** |
| Goal-metric accuracy | **15%** |

Missing a prediction for a settled episode does **not** insert a zero score. It simply adds no evidence that day — your standing is diluted only by having fewer scored entries.

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

## What scoring does **not** decide

- **Who earns emissions** and the share curve → [SN21_REWARDS.md](./SN21_REWARDS.md)
- **Alpha stake / hold requirements** → [SN21_STAKING.md](./SN21_STAKING.md)
- **Cutover dates and bridge payouts** → [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)

## Related

- Direction overview: [DAILY_STREAM_DIRECTION.md](./DAILY_STREAM_DIRECTION.md)
- Model / container contract: [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)
