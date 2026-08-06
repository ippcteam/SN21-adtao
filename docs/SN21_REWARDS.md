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

Ties break deterministically: higher standing first; equal standing → miner id ascending.

**Published score threshold:** starts at **0.0** (review-adjustable). At threshold or above earns; strictly below does not.

## Placement eligibility

You need enough scored predictions in the standing window before the curve can place you:

- **250** scored predictions → eligible for placement
- **1000** → full standing confidence

(Details in [SN21_SCORING.md](./SN21_SCORING.md).)

## Champion vs earner (two different seats)

| | **Weights (who earns)** | **Champion (who runs live)** |
| :---- | :---- | :---- |
| Decides | Emission share via the curve | Which model runs across real accounts |
| Changes when | Standings move through the curve | All three promotion tests pass |

### Champion promotion

The champion changes **only** when a challenger:

1. leads the incumbent’s moving average by at least **5%** (relative), **and**
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
  same digest, or different digest with byte-identical predictions — only
  the earliest submission earns. The rest are excluded from that day's
  earning set. Standings are untouched and the container keeps running:
  the exclusion lapses the moment the hotkey runs a model of its own.
- **The published reference model is exempt.** Running the reference
  unchanged is participation, not plagiarism. It is also how everyone
  starts; note it cannot outrank anything, since admission requires
  beating the baseline it defines.
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
