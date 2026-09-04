# SN21 — Rewards (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners |
| **Status** | Authoritative for daily-stream emissions |
| **Last updated** | 2026-08-04 |
| **Update independently of** | [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_STAKING.md](./SN21_STAKING.md) · [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |

This document explains **how emissions are allocated** once you have a standing. It does **not** explain how individual predictions are scored — that is [SN21_SCORING.md](./SN21_SCORING.md).

---

## In one line

Your **standing** (moving-average score) is mapped through a **published weight curve** into on-chain weights; emissions flow continuously on Bittensor’s tempo from those weights. The model that **runs live** (champion) is chosen by a separate, stricter rule.

> **Where you see it.** The public dashboard updates itself daily: each
> day's report publishes automatically when the scoring run completes
> (from 20 August 2026). Only corrections and weekly-era reports go
> through manual review.

> **Leaderboard vs chain — the timing, stated once.** Scores, the
> leaderboard, and on-chain weights update on three clocks:
> 1. **Scoring** happens once per day, when the daily run settles matured
>    outcomes (late morning UTC).
> 2. **The leaderboard** publishes minutes after that run — it reads the
>    fresh standings directly.
> 3. **The chain** lags by design: the validator commits the new weight
>    vector under Bittensor's commit-reveal, which reveals roughly **72
>    minutes** after the commit, and emissions then move on the chain's own
>    tempo. So on-chain rankings normally reflect a leaderboard change
>    within **one to three hours**, not instantly.
>
> If the chain has not caught up with the leaderboard within ~6 hours,
> that is not the mechanism — report it in the miner channel. (This
> happened 2026-08-22→24: a scheduler fault stopped fresh commits while
> the activity heartbeat kept re-signing the previous vector, so the chain
> looked alive but frozen. A miner's report surfaced it; the commit path
> no longer depends on the retired weekly cadence.)

## From standing to weight

1. Scoring produces a per-miner standing ([SN21_SCORING.md](./SN21_SCORING.md)).
2. Standings pass through the **weight curve** below.
3. Validators set weights on chain; emissions follow Bittensor’s own tempo (~every 72 minutes). There is **no weekly payout event** in the steady-state daily stream.

During cutover, bridge rules and **indicative** burn rates may temporarily modify who is paid — see [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md). Burn is not fixed; it may be adjusted at any time (see below).

## The weight curve

Steep, but **no winner-take-all cliff**. Rank-based shares among earners; below the score threshold → zero weight. Hard ceiling on how many miners earn.

| Rank | Share of miner emissions (before re-normalisation) |
| :---- | :---- |
| 1st | **50%** |
| 2nd | **25%** |
| 3rd | **10%** |
| 4th+ | Geometric tail: each next rank gets **50%** of the previous share |
| Cap | At most **20** earners |
| Floor | Standing **below** the published score threshold → **0** weight |

If fewer than the full curve of earners qualify, shares are re-normalised so weights still sum to 1.0 (ratios preserved).

**If you ranked outside the cap, your row says so.** The paid set is a fixed
size, so on any day where more than 20 miners qualify, qualifying miners will
place outside it. That is not a penalty and nothing is held against you: your
score stands, your model keeps being executed daily, and tenure keeps
accruing. The daily report shows this on your own row as **"Ranked below the
earning cut"**, so an unfunded row always carries a reason rather than being
left blank.

Ties break deterministically: higher standing first; equal standing → miner id ascending.

**Published score threshold:** starts at **0.0** (review-adjustable). At threshold or above earns; strictly below does not.

> Under the relative standing method ([SN21_SCORING.md](./SN21_SCORING.md),
> rule amendment of 2026-09-04) a standing of 0.0 means "level with the
> field", so the threshold is **not applied** there: the top **20** by
> standing earn the shares above, exactly as this table states. The threshold
> in force is published in each day's allocation audit.

## Placement eligibility

You need enough scored predictions in the standing window before the curve can place you:

- **250** scored predictions → eligible for placement
- **1000** → full standing confidence

> **First-cycle bootstrap.** During the weekly→daily transition the settled
> evidence is thin by construction — the earliest daily baskets are still
> maturing — so the placement floor starts at **50** and rises back to **250**
> as daily volume accumulates. By the time it returns to 250, a miner
> predicting daily is well past it, so the step-up seats and unseats no one.
> The full-standing floor stays **1000** throughout, so thin early standings
> are weighted conservatively.

(Details in [SN21_SCORING.md](./SN21_SCORING.md).)

## Absence penalty (rule amendment, published 2026-08-24)

A board position must be defended every day the subnet runs. From the
published effective date:

**Every episode of a subnet-run day that you do not return a scoreable
prediction for enters your standing as a zero (floor score 0.00), carried
at the full episode weight — the same 1.0 standing mass a covered episode
contributes across its horizons.** Miss a 400-episode day, carry 400
full-weight zeros in your average. Cover the day fully and this rule never
touches you.

> **Corrected 2026-08-24, the same day the rule went live, before any
> charge was ever applied** (the public penalty log was still empty). The
> floor was first published as 0.30, argued from day-MEAN score bands.
> That was wrong in two ways, and we would rather say so than let anyone
> optimise against it: 11.4% of real per-entry honest scores fall below
> 0.30 (measured over 134,425 production entries), so on those episodes
> the "penalty" would have paid better than an honest prediction; and the
> single 0.20-weight charge carried a fifth of a covered episode's
> standing mass, so dropping a below-average episode stayed rational.
> Both corrected: floor 0.00 at full weight, so covering an episode at
> any honest score always leaves a standing at least as high as dropping
> it. No penalty was ever applied under the old numbers.

The design, stated plainly so nobody has to reverse-engineer it:

0. **What counts as covered.** An episode is covered when your output carries a
   non-empty block for every horizon the episode lists in
   `episode_metadata.outcome_horizons_days`. An empty, partial or null
   `horizons` is an abstention and the episode is charged as uncovered.
1. **No threshold to duck under.** The 75% participation bar still gates
   *payment* exactly as before, but the score penalty charges every
   uncovered episode — covering 80% of a day still charges the other 20%.
   Predicting only the comfortable episodes is not a strategy.
2. **No exit.** You are charged for every uncovered subnet-run day for as
   long as you hold any entries in the 35-day standing window. Going
   quiet does not freeze your number — it bleeds it toward the floor
   until you return, or your entries age out and you leave the board the
   natural way. This is deliberate: a standing you are not defending is
   not a standing.
3. **A penalty can never beat honesty.** The floor (0.00) is at or below
   every achievable honest score, and the charge carries the same weight
   a covered episode would have — so predicting always weakly dominates
   skipping, episode by episode, with no tail exception. The floor value
   is published here and any change to it is a published rule change.
4. **Days the subnet fails to run charge nobody.** Our downtime is never
   your penalty.
5. **One charge per (day, miner), forever** — re-runs and catch-up sweeps
   cannot double-charge, and every applied charge is published at
   `/v1/daily/absence-penalties` beside the receipts, so your standing —
   penalties included — reproduces from public documents alone.

Why: under the previous rules, absence touched only transient weight and
never the score, so a miner absent on hard days kept an average built
only on easy days — and held first place with it while every participant
moved. That is not a board worth topping. This amendment makes absence a
scored event, which is the only way "show up every day" means anything.

## Earning-set activation (rule amendment, published 2026-08-26)

Two rules governing WHO the curve pays are active from **2026-08-26**,
applied from that day's run forward, never retroactively. Standings,
receipts, and past weights are unchanged.

**1. One payer per model — switched ON.** The rule below ("One payer per
model") was published with its switch-on pending; that switch-on is now
effective 2026-08-26. Detected groups are published with their working in
the day's allocation audit, as promised.

**2. Earning-set tenure — new.** A hotkey enters the paid earning set only
once it has scored entries on at least **7 distinct settle days** within the
standing window. Before that:

- the hotkey appears on the leaderboard as usual — scores are facts and
  accrue from the first settled entry;
- its model keeps being executed daily, which is exactly how tenure
  accrues: keep a model committed and running, and the horizons maturing
  do the rest;
- it holds no seat in the paid curve, so a standing computed over a small
  number of settled days cannot out-earn a track record.

Why: a standing is an average, and an average over one or two settled days
carries no evidence of sustained accuracy. Payment follows a track record.
The day count uses the same distinct-scored-days measure the champion rules
already use; absence-penalty entries count as scored days (sitting out
accrues days only at full-weight zero scores, which is not a path into the
curve anyone would want). The gate never removes the last remaining
eligible miners: if applying it would empty the curve entirely, it stands
down for that day and the allocation audit records that it did.

## Copy detection: point estimates and lineage (rule amendment, published 2026-08-29)

Two additions to "One payer per model" below, effective **2026-08-29**,
applied from that day's run forward, never retroactively. Standings,
receipts and past weights are unchanged.

**1. Matching point estimates count as the same model.** The rule below
grouped hotkeys whose predictions were byte-identical. A prediction carries
a point estimate and an interval around it, so two submissions could carry
the same point estimate on every episode and still be counted as separate
payees because their interval bounds differed. From the effective date the
point estimates are also compared on their own: hotkeys whose point
estimates match exactly across the day's shared episodes form one group and
pay one principal.

Both tests are exact. There is no tolerance and no parameter to sit outside,
and both publish their groups in the day's allocation audit, so anyone
holding the day's receipt can recompute the grouping and check it.

**2. Behavioural lineage is in force, at parameter version `lineage-v1`.**
The four-signal test described in [the threat
model](./SN21_THREAT_MODEL.md#12-copy-a-model-and-perturb-the-output-slightly)
is switched on. As published there, we give the mechanism and the parameter
version rather than the four numbers; the version in force is recorded in
every day's allocation audit alongside the groups it produced.

The exemptions in "One payer per model" apply unchanged to both tests. In
particular the published reference model is exempt: a group containing a
hotkey running the reference stands down as a whole rather than paying one
member and excluding the rest. The allocation audit records whether an
exemption was in force on the day, so its absence is visible rather than
assumed.

## Champion vs earner (two different seats)

| | **Weights (who earns)** | **Champion (who runs live)** |
| :---- | :---- | :---- |
| Decides | Emission share via the curve | Which model runs across real accounts |
| Changes when | Standings move through the curve | All three promotion tests pass |

### Champion promotion

The champion changes **only** when a challenger:

1. leads the incumbent’s moving average by at least **5%** (relative) — under
   the relative standing method ([SN21_SCORING.md](./SN21_SCORING.md), rule
   amendment of 2026-09-04) this becomes an **absolute** lead of at least
   **0.01**, because a standing measured against the field sits near zero
   and a percentage of it means nothing — **and**
2. has held that lead for **7 consecutive days**, **and**
3. has at least **14 scored days** of history.

Miss any one → incumbent stays. Weights can shift gradually while the champion seat does not.

## One payer per model (copied models)

Models must be public and pullable, so copying one is trivial. The rules
make it unprofitable rather than pretending it is impossible:

- **Ties go to the first submission.** Ranking is standing, then precedence,
  then hotkey — precedence being when a model was first committed on chain,
  and for identical behaviour, when the published daily receipts first
  record a miner producing it. You can grind a hotkey; you cannot commit
  before the model you copied existed, and you cannot appear in the
  receipts before its author did.
- **Identical models pay once.** When several hotkeys run the same model —
  same digest, or a different digest producing the same behaviour — only the
  earliest submission earns. The rest are excluded from that day's earning
  set. Standings are untouched and the container keeps running: the
  exclusion lapses the moment the hotkey runs a model of its own. Same
  behaviour means any of three tests: byte-identical predictions, matching
  point estimates, or one lineage under the four-signal test (see the
  amendment above for the second and third).
- **The published reference model is exempt.** Running the reference
  unchanged is participation, not plagiarism. It is also how everyone
  starts. It cannot earn on its own, because admission requires beating the
  naive baseline by a published margin and the reference model does not clear
  its own bar — you are expected to start from it and improve on it.
- **Evidence, not accusation.** Every detected group is published with its
  working — the shared digest, or the matching prediction fingerprint,
  which anyone can recompute from the day's receipt. Applies from the day
  of switch-on, never retroactively.

Rebuilding your own image is safe: identity over a behaviour follows the
published record, so a rebuild does not reset your seniority to someone who
copied the old build. Protect a new model before its first commitment by
committing the digest while the image is still private — see the
[quickstart](./miner_quickstart.md).

## Quiet days

No special weekend multiplier. Thin baskets simply produce fewer scored predictions; your standing is per-prediction, so volume self-scales.

---

## Worked examples

### Example A — three earners (curve re-normalised)

Standings on a given day (all above threshold, all placement-eligible):

| Miner | Standing | Rank |
| :---- | ---: | ---: |
| Alice | 0.82 | 1 |
| Bob | 0.75 | 2 |
| Carol | 0.70 | 3 |

Raw curve shares: 50% / 25% / 10%. Sum = **0.85**. Re-normalise:

| Miner | Raw share | Weight after re-norm | Share of miner emissions |
| :---- | ---: | ---: | ---: |
| Alice | 0.50 | 0.50 / 0.85 ≈ **0.588** | ~58.8% |
| Bob | 0.25 | 0.25 / 0.85 ≈ **0.294** | ~29.4% |
| Carol | 0.10 | 0.10 / 0.85 ≈ **0.118** | ~11.8% |
| **Total** | 0.85 | **1.000** | 100% |

There is no cliff: if Bob’s standing rises toward Alice’s, ranks can swap and weights move — but nothing jumps from 0% to 100% on a single tick.

### Example B — five earners (geometric tail)

| Rank | Miner | Standing | Raw share |
| ---: | :---- | ---: | ---: |
| 1 | Alice | 0.84 | 0.50 |
| 2 | Bob | 0.78 | 0.25 |
| 3 | Carol | 0.72 | 0.10 |
| 4 | Dave | 0.68 | 0.10 × 0.5 = **0.05** |
| 5 | Eve | 0.65 | 0.05 × 0.5 = **0.025** |

Raw sum = **0.925**. After re-norm: Alice ≈ 54.1%, Bob ≈ 27.0%, Carol ≈ 10.8%, Dave ≈ 5.4%, Eve ≈ 2.7%.

Miners outside the top 20, or below the score threshold, get **0**.

### Example C — burn during cutover (does not change the curve)

Burn rates are **planned and indicative only** — they may change at any time. Using the *illustrative* 30% burn from the transition plan on **12 August**: if Alice’s curve weight is 0.588 of the *miner* pool after burn:

- **70%** of subnet miner emissions are paid out under the curve  
- Alice receives `0.588 × 70%` ≈ **41%** of total miner-side emissions that day  
- The burned share is not redistributed through the curve  

Stake hold is a separate gate: fail the alpha hold → **0** payout that day even with a high standing.

### Example D — champion vs earner

| Day | Alice standing | Bob standing | Bob lead vs Alice | Bob consecutive lead days | Bob scored days | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | :---- |
| 1–6 | 0.80 | 0.86 | 7.5% (≥ 5%) | 1…6 | 20 | Bob earns more via the curve if ranked higher; **Alice stays champion** (hold not yet 7 days) |
| 7 | 0.80 | 0.86 | 7.5% | **7** | 20 | **Bob promoted** to champion (margin + hold + history all met) |

If on day 7 Bob’s lead shrinks to 3% (&lt; 5%), the hold clock resets — Alice remains champion.

## What replaces weekly tiers / EMA

The daily stream **does not** use Elite / Competitive / Participating bands or four-week EMA pools. Those were weekly-epoch machinery. The curve + standing replace them.

## Burn rate and stake

- **Burn** during cutover is published as a **plan only** in [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md). Figures are **indicative**. Because Bittensor’s emissions-allocation methodology is changing significantly, SN21 may **adjust burn at any time** to **protect and grow alpha value for all holders**. Adjustments will be announced as soon as practical; do not treat the transition table as a locked schedule.
- **Alpha hold / staking** requirements are in [SN21_STAKING.md](./SN21_STAKING.md).

Neither changes how an individual prediction is scored.

## Parameter reviews

Numeric curve parameters (threshold, shares, cap) are restated at **four-weekly** published reviews. Changes are announced in advance; nothing silent between reviews.

## Related

- Scoring: [SN21_SCORING.md](./SN21_SCORING.md)
- Staking: [SN21_STAKING.md](./SN21_STAKING.md)
- Cutover: [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)
- Why daily: [SN21_WHY_DAILY.md](./SN21_WHY_DAILY.md)
