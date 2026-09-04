# SN21 — Scoring (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.4 |
| **Audience** | Miners |
| **Status** | Authoritative for daily-stream scoring |
| **Last updated** | 2026-08-21 |
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

> **Is v2 actually what runs? Check the receipt, not the code default.** In
> the code, v2 sits behind `SN21_SETTLE_SCORING_V2` and the default is off —
> a fresh checkout without that flag would score v1. That default is not what
> production runs: the flag is set on the production scoring validator (there
> is exactly one), and **every daily settle since the first scored day,
> 2026-08-16, has run v2**. No daily-stream results were ever scored with v1.
> You do not have to take this paragraph's word for it: every published daily
> receipt records `formula.version` and the exact weights that scored it, and
> `scripts/verify_day.py` recomputes your scores with the receipt's own
> recorded formula — so a scorer change without a receipt change is
> impossible to hide. Fetch any day and look:
> `https://hope-bittensor-api.onrender.com/v1/daily/2026-08-18/receipt`
> (`document.metrics.formula` → `{"version": "v2", "weights": {"quantile":
> 0.5, "coverage": 0.1, "direction": 0.15, "goal": 0.15, "normaliser":
> 0.9}}`). Train against v2.

| Component | Weight |
| :---- | :---- |
| Quantile accuracy (pinball on P10/P50/P90) | **50%** |
| Calibration | **20%** |
| Directional accuracy | **15%** |
| Goal-metric accuracy | **15%** |

> **Amended 2026-08-24 — missing an episode now costs a full-weight zero.**
> This document previously said a missed prediction "does not insert a zero
> score; it simply adds no evidence." That rule made absence on hard days
> strictly profitable and is retired: from 24 August, every episode of a
> subnet-run day you do not return a scoreable prediction for enters your
> standing as a **zero at the full episode weight** — the same standing mass
> a covered episode contributes. Covering an episode at any honest score
> therefore always leaves a standing at least as high as skipping it. Days
> the subnet fails to run charge nobody, and every applied charge is
> published at `/v1/daily/absence-penalties`. Full rule and the openly
> recorded same-day floor correction: [SN21_REWARDS.md](./SN21_REWARDS.md).

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

### Standing method (rule amendment, published 2026-09-04)

From the effective date below, three things change in how the entries above
are averaged. Nothing changes in how an entry is scored, and the receipts
stay as they are.

1. **Entries are relative to the field on the same change.** Each entry
   becomes your score **minus the mean score of every miner scored on that
   same (episode, horizon)**, computed from the receipt of the settle day. A
   standing of +0.03 means you were 0.03 above the field on the identical
   changes; the mix of change types you were scored on no longer moves your
   number. Every input is in the published receipt.
2. **Half-life 7 days, window 28 days** (were 12 and 35). Your standing is
   your last four weeks, with the most recent week counting most: evidence
   from three weeks ago carries one eighth of the weight of today's, and
   nothing older than four weeks counts at all. No position can be held on
   stale evidence.
3. **Shrinkage toward the field.** The average carries a prior of **250**
   prediction-mass at the field level (0.0 in relative terms): a standing
   starts at the field and moves out only as evidence accumulates. The
   number is the placement floor; a miner at the floor has exactly half of
   its standing decided by evidence.

An uncovered episode (absence rule) enters at the published floor as
before; in relative terms that is the floor minus the field, i.e. below
every honest entry.

| Parameter | Before | From the effective date |
| :---- | :---- | :---- |
| Entry value | score | score − field mean on the same (episode, horizon) |
| Half-life | 12 days | 7 days |
| Prior mass toward the field | none | 250 |
| Window | 35 days | 28 days |
| Champion promotion lead | 5% relative | 0.01 absolute (see rewards doc) |
| Weight-curve score threshold | 0.0 | not applied: the top 20 by standing earn the published shares |

**What this means for a new model.** Its first entries land 15 days after
its first basket (the 7-day horizon plus the 7-day settling window), the
14-day entries a week later, the 28-day entries two weeks after that. From
the first landing it accrues evidence every day; it clears the placement
floor within days at full coverage, the earning-set tenure after seven
scored days, and from then on its rank follows its edge over the field on
the changes of the last four weeks. Incumbents are measured on exactly the
same window, so a better recent model overtakes within two to three weeks
of its first landing, and no faster than the evidence supports.

Effective date: **to be announced**; the parameters in force are published in
each day's allocation audit (`/v1/daily/{day}/allocation-audit`,
`standing_method`). Applied forward, never retroactively.

Cold-start evidence floors (used when placing you for emissions — see rewards doc):

| Floor | Predictions in window | Meaning |
| :---- | :---- | :---- |
| Placement | **250** | Minimum evidence before you can earn under the curve |
| Full standing | **1000** | Full standing confidence |

**First-cycle bootstrap.** During the weekly→daily transition the placement
floor starts at **50** and ramps back to **250** as daily volume accumulates;
the full-standing floor stays **1000**. See the note in [SN21_REWARDS.md](./SN21_REWARDS.md#placement-eligibility).

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

**Pre-amendment behaviour (until 23 August):** if you never submitted the 14-day prediction, the Day-22 row never appeared — absent evidence, no zero.

**From 24 August (absence penalty):** an episode you do not cover at all on a subnet-run day enters your standing as a zero at full episode weight on that day, so skipping is never better than an honest prediction. (A *partially* covered episode still scores only the horizons you submitted; the penalty charges per uncovered episode of the day's basket, not per horizon.) See [SN21_REWARDS.md](./SN21_REWARDS.md#absence-penalty-rule-amendment-published-2026-08-24).

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
- **Checking our arithmetic:** [SN21_VERIFYING.md](./SN21_VERIFYING.md) — every
  scored day is published as a signed receipt (outcomes used, predictions
  verbatim, each score's components) and you can recompute your own scores from
  it with `scripts/verify_day.py`. Censored horizons are stated in the receipt
  with their count and reason, so a dropped horizon is visible rather than
  silent.
