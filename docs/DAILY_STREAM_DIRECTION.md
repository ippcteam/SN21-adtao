# SN21 — Moving to a Daily Prediction Stream

| | |
| :---- | :---- |
| **Version** | 0.1 (draft direction) |
| **Status** | Proposed — published for miner review ahead of a formal governance amendment |
| **Owner** | AdTAO / SN21 operator |
| **Companion (current rules)** | [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md) · [SN21_EPOCH_STRUCTURE.md](./SN21_EPOCH_STRUCTURE.md) · [MINER_ECONOMICS.md](./MINER_ECONOMICS.md) |

**Read this first:** Nothing changes on-chain today. The weekly epoch, current scoring, and current payouts continue exactly as they are until a formal amendment is published with advance notice. This document explains the *direction* we're proposing so you can plan and give feedback early.

---

## In one line

We're proposing to replace the **weekly epoch** with a **daily prediction stream**: fresh baskets and fresh scores every day, weights that update daily through a published curve, and one live "champion" model that only changes under a deliberate rule — never on a single day's luck.

## Why we're doing this

You asked for faster feedback. A weekly epoch makes you wait a week to learn how you did and a week to see it reflected in emissions. A daily stream gives you a **daily improvement loop**, and keeps the network's best model live across the whole portfolio at all times.

## The daily clock

| When | What happens |
| :---- | :---- |
| **Each day** | A fresh basket of real, qualifying account changes is revealed. You predict each outcome that day and your predictions are locked on chain — before any outcome exists. |
| **7 / 14 / 28 days later** | Outcomes are measured at each horizon, plus a short **settling window** so late-reporting conversions are counted *before* we score. |
| **Every day** | Baskets maturing today are scored and folded into your moving average; weights update through the curve; the champion-promotion rule is checked. |
| **Continuously** | Emissions flow on Bittensor's own tempo (~every 72 min) from current weights — no payout events to wait for. |
| **Every 4 weeks** | A published parameter review — the only fixed calendar rhythm. All rule changes are announced in advance; nothing changes between reviews. |

## How daily scoring stays honest

We never re-score the same change. Each day a *different* day's basket finishes maturing. Every prediction is scored **exactly once**, against a **final** outcome — because of the settling window, the number you're scored against never moves after the fact. (This is the same "sealed before the outcome is knowable, reproducible by anyone" guarantee SN21 runs on today.)

## How rewards work

- Your weight follows your **moving-average score** through a **published curve** — steep, but with **no winner-take-all cliff.** The top model earns a large share, second place less, then a decaying tail; below a published score threshold, weight is zero.
- As a challenger closes the gap, weight shifts **gradually** — there is nothing that flips 100% on a single day.
- Earning while you climb is intended: it funds your collateral and keeps the contest worth entering. In practice we expect a **small earning set** (a handful of live models), with a published hard ceiling per basket.

## Going live — the "champion"

The champion is the model that actually runs across real accounts — so we won't swap it on a statistical tie. It changes **only** when a challenger:

1. leads the moving average by a **published margin**,
2. has **held that lead for several consecutive days**, and
3. has **enough scored history** behind it.

Miss any one and the incumbent stays. Weights (who earns) and the champion (who runs live) are deliberately two different things.

## Fair scoring on quiet days (weekends, holidays)

Your standing is a **per-prediction** moving average, not a per-day one. A thin Saturday simply contributes fewer predictions and counts proportionally less — automatically, with no special rule and no threshold to game. One unusual day can't swing your average.

## What counts

Baskets are drawn from real, qualifying account changes under a **published eligibility list.** Some change types that carry no genuine predictive signal (for example, automated defensive-hygiene churn) are excluded, so scoring stays a real contest between models rather than a source of easy points.

## Skin in the game

Earning slots will be backed by on-chain **registration collateral**, using Bittensor's native mechanism as the network rolls it out. There is **no upfront buy-in** — collateral fills from your earnings as your model starts winning, and existing scoring miners are carried over on this path.

## What replaces the weekly epoch

- **A four-weekly parameter review** on a published calendar. This keeps the real protection the epoch gave you — **predictable rules, announced ahead of time** — without the fixed weekly wait.
- At cutover, weight flows **only** through the new curve. The long tail of dust payouts to inactive/squatting UIDs ends. This is announced plainly and in advance; it's what makes the active earning slots worth defending.

## What stays the same

- **What you predict** and the **per-prediction scoring formula** (quantile accuracy, calibration, direction, goal metric) are unchanged.
- **Every prediction is still sealed on chain before the outcome is knowable, and every score is reproducible** by anyone with a chain reader.
- **Governance stays transparent:** parameter changes are published with rationale and lead time, exactly as today.

## Transparency

We'll publish a **daily prediction-accuracy feed** — our scored predictions against final outcomes — with its integrity anchored on-chain, so anyone can independently verify the scoring is real.

## What this means for you right now

- **Nothing to do today.** Keep mining as you are.
- **If you build models:** the daily loop, the published curve, and the promotion rule are the shape to design toward.
- **Feedback is open now** — this is a draft, and miner input shapes the final parameters.

## Timeline

This is a **proposed direction under active development.** The formal governance amendment — with exact parameters, the eligibility list, and a cutover date — will be published with advance notice before anything changes on-chain. Until then, the current weekly rules remain authoritative.

---

*In AdTAO, we TRUST.*
