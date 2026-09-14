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
| **Horizon + settle** | Outcomes are measured at **7 / 14 / 28 days**, then held for a **2-day settling window** so late-reported conversions are included. |
| **Settle day** | That (episode, horizon) is scored once. The score enters your standing. It is never re-scored. |

Settle date for a horizon:

`action_window_end + 1 day + horizon days + 2-day settling window`

> **Corrected 2026-09-14.** This document said "7-day settling window" and
> "~day 15 / 22 / 36" from launch. The platform has always run a **2-day**
> settling window, and every published receipt shows it: `finalized_on` is
> **10 / 17 / 31** days after the basket day for the 7 / 14 / 28-day horizons
> (the basket day is the action-window end). Nothing in scoring changed; the
> text now matches what runs. Found by a miner from the receipts.

Examples (from action-window end):

| Horizon | First scoreable (approx.) |
| :---- | :---- |
| 7-day | day 10 |
| 14-day | day 17 |
| 28-day | day 31 |

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

**Example.** Action-window ends day 0. Horizons first score on day 10 / 17 / 31. The account leaves the network on **day 12**:

| Horizon | Settle (approx.) | What happens |
| :---- | :---- | :---- |
| 7-day | day 10 | Already settled → **scored and kept** |
| 14-day | day 17 | Not yet settled → **dropped** (`left_system`) |
| 28-day | day 31 | Not yet settled → **dropped** (`left_system`) |

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

### Measurement resolution (rule amendment, published 2026-09-05)

The table above has always been published with three rows. From the
effective date below the row an episode uses is **derived from what the
episode touched**, per the April 2026 design; until then every episode was
scored as *high*.

| Resolution | When | 7d / 14d / 28d blend | Episode weight |
| :---- | :---- | :---- | :---- |
| High | every change in the episode is campaign-level (budget, campaign pause, bid strategy, target, campaign criteria, campaign assets) | 0.20 / 0.35 / 0.45 | 1.0 |
| Medium | any change sits below the campaign (ad group, ad, keyword, ad-group criterion or asset), measured on its parent campaign | 0.15 / 0.30 / 0.55 | 0.7 |
| Low | a sub-campaign change whose share of the parent is below the published impact floor | 0.00 / 0.20 / 0.80 | 0.4 |

A composite episode is judged on **every** constituent: all campaign-level
is high; any sub-campaign constituent is medium. *Low* needs the impact
ratio, which is not yet recorded per episode; until it is, no episode is
low. The episode weight multiplies the entry's standing mass, so a medium
entry carries 0.7 of a high entry's evidence.

The resolution and the resulting entry weight are written on every receipt
entry (`resolution`, `weight`) from the effective date, and the resolution
you are being scored at is the `measurement_resolution` in the episode
payload you receive.

Effective date: **2026-09-05** — applied to entries finalised on or after
that date. Entries already in the standing ledger are unchanged.

## Your standing (moving average)

Your published standing is an **episode-age-weighted** mean of scored entries — **not** a per-day average.

- Each scored (episode, horizon) enters at its settle day (a second amendment, announced below with its effective date to follow, will age it from its **prediction day**).
- Weight decays with age: half-life **12 days**, window **35 days** (amended below).
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

The half-life, prior mass and window belong to this amendment and take effect
with it, on its effective date; a run before that date keeps the "before"
column whatever the operator has configured ahead of time.

**What the public board shows.** A relative standing sits near zero by
construction (a field-average model reads 0.000), so the leaderboard's
headline number is the **absolute accuracy** — the age-weighted mean of the
absolute scores over the same window and half-life, without the prior — and the
relative standing is shown beneath it as a signed edge over the field. Rows are
ranked by the relative standing, which is what pays. Both numbers are published
per hotkey in each day's allocation audit (`standings`: `relative`, `absolute`,
`rank`). A hotkey whose evidence is still under the placement floor has no
standing and no rank yet; it is listed all the same, with its accuracy so far
and its evidence against the floor, and the audit's `placement` block carries
the same numbers.

**What this means for a new model.** Its first entries land 10 days after
its first basket (the 7-day horizon plus the 2-day settling window), the
14-day entries a week later, the 28-day entries two weeks after that. From
the first landing it accrues evidence every day; it clears the placement
floor within days at full coverage, the earning-set tenure after seven
scored days, and from then on its rank follows its edge over the field on
the changes of the last four weeks. Incumbents are measured on exactly the
same window, so a better recent model overtakes within two to three weeks
of its first landing, and no faster than the evidence supports.

Effective date: **2026-09-05** (the daily run of that date and every run
after it). The parameters in force are published in each day's allocation
audit (`/v1/daily/{day}/allocation-audit`, `standing_method`); a run before
the effective date reports `absolute` there. Applied forward, never
retroactively: no published score, receipt or past weight changes.

### Current model, current form (rule amendment, announced 2026-09-14 — effective date to follow)

Under the settle-day rule above, a replaced model kept deciding a miner's
rank for weeks: nothing the new model predicts settles for 10 days, the old
model's 14- and 28-day results keep landing at full weight for 31 days, and a
bad settle day then takes a week to lose half its weight. A miner who fixed
their model saw accuracy rise and rank stay flat. From the effective date
below, two things change in how entries are aged and weighted. Nothing
changes in how an entry is scored, the receipts stay as they are, and
everything below is recomputable from published documents.

1. **Entries age from the day the prediction was made.** An entry's age is
   the number of days since its **basket day**, not since its settle day.
   Receipts from this date carry the basket day as `predicted_on`; for
   older receipts the operator uses the basket the episode was released in
   (the daily basket feed names it), and where that is unavailable derives it
   as `finalized_on − horizon − 3` (the settle schedule: action-window end +
   1 day + horizon + 2-day settling window).
   The half-life stays **7 days**. The window becomes **42 days** of
   prediction age, so the 28-day horizon, which lands at age 31, still
   counts. The prior toward the field becomes **100** prediction-mass: with
   entries entering already aged, the effective evidence behind a standing
   is smaller, and 250 would over-shrink a model that is new but good.
2. **A replaced model fades faster.** Each hotkey's **current model** is the
   digest it runs, dated from the first basket day that digest ran (published
   per hotkey in the allocation audit, `standing_method.model_since`).
   Entries predicted **before** that day count at **one quarter** of their
   weight — but only once the current model's own entries inside the window
   carry at least **250** prediction-mass (the placement floor). Until then
   the previous model's entries count in full: a new commit cannot shed a
   bad month before it has shown anything, and a good new model is ranked on
   its own work as soon as it has the floor's worth of it.
3. Everything else is unchanged: the relative-to-field entry value, the
   absence rule (an uncovered basket day is dated by that day under both
   rules), tenure (still counted in settle days), the placement floor, the
   curve.

| Parameter | Settle-day rule (2026-09-05) | From the effective date |
| :---- | :---- | :---- |
| Age measured from | settle day | prediction (basket) day |
| Half-life | 7 days | 7 days |
| Window | 28 days | 42 days |
| Prior mass toward the field | 250 | 100 |
| Previous model's entries | full weight | × 0.25 once the current model carries 250 mass in the window |

**What this means for a new model.** Its first entries still land 10 days
after its first basket; outcomes must mature and no rule changes that. From
that day, each entry enters at the age of its prediction, so the new model's
first landing already carries its own weight against the old model's late
results instead of being outweighed by them; and once the new model holds
250 mass of evidence, the old model's entries count at a quarter. A better
model shows in the standing within days of its first landing rather than
weeks, and a bad month stops counting against a miner about three weeks
earlier than under the settle-day rule. The leaderboard's headline accuracy
uses the same dating and weights, so the number a miner watches and the
number that ranks them move together.

Effective date: **to be announced**, with notice, in the miner channels and
here. Until that date the settle-day rule above ranks every day, and each
day's allocation audit reports `standing_method.age_basis: settle_day`.
From the effective date it reports `prediction_day` together with
`window_days`, `prior_mass`, `previous_model_weight`,
`previous_model_threshold`, `model_since`, and `previous_model` naming the
hotkeys discounted that day. Applied forward, never retroactively: no
published score, receipt or past weight changes.

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
