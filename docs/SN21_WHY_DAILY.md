# SN21 — Why the daily stream

| | |
| :---- | :---- |
| **Version** | 1.0 |
| **Status** | Published — the canonical why-daily statement; announcements derive from this |
| **Audience** | Miners, partners, and the wider Bittensor / ads audience (Discord / site copy derived from this) |
| **Owner** | AdTAO / SN21 operator |
| **Companions** | [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) · [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_STAKING.md](./SN21_STAKING.md) · [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md) |

This is the canonical why-daily statement. [DAILY_STREAM_DIRECTION.md](./DAILY_STREAM_DIRECTION.md) is a stub pointing here. Discord / [adtao.io/sn21](https://adtao.io/sn21/) copy should be **derived from this**, not the other way around.

---

## What's changing

SN21 is moving from weekly release cycles to a **daily stream**.

Every day, the changes made across our connected Google Ads accounts go out as one mixed basket. Your admitted **model** predicts each change's outcome that day, before any outcome exists. Outcomes are measured at **7, 14, and 28 days**, then a further **7-day settling window** lets late-reporting conversions land — so the number you're scored against is final and never revised.

Because a different day's basket matures every day, you get fresh scores every day. Weights follow your moving average and update daily, and emissions flow continuously from current weights — no more waiting on a weekly cycle for anything. New models are admitted when they pass the backtest gate (see model spec); there is no weekly admission window.

How to build and ship a model: [miner_quickstart.md](./miner_quickstart.md).  
Dates and bridge rules: [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md).

---

## Why we're making the change

### Why we started with weekly, single-type epochs

When we joined Bittensor we were encouraged to give miners a problem they could actually solve — a barrier high enough to be real, low enough that people could learn the network without drowning.

So we designed for **clean, linear progress**:

- **One type of problem** at a time (e.g. bid-strategy changes in a focused week).
- **One weekly epoch** to solve it, score it, and move on.

If the network got good at one class of change this week, it could build on that the next. For onboarding a new subnet and a new miner set, that was the right first shape.

### What we learned from that approach

Two problems showed up in practice — and they matter as much to customers and observers as to miners.

1. **It is slow.** Progress moves in weeks. Feedback, model iteration, and coverage of the real decision space all wait on the same weekly clock. That was our rule, not the chain's, and it throttled everyone's improvement loop — including ours.

2. **It sanitises reality.** In live Google Ads accounts, changes rarely happen in isolation. They arrive in **baskets and clusters**. Artificially separating a single change type makes the contest cleaner and the learning path clearer — but it also makes the result less like the world we deploy into, and therefore less valuable when the winning model has to recommend what to do *next* on a real account.

There was a trust problem as well. Epochs were built from **historical** settled work. This meant that should we have wished to (which we never did) we could potentially self mine with a miner who already has the settled outcomes and therefore win on a consistent basis.  The challenge was never raised to us, but that doesn't mean that the attack vector doesn't exist.

### What daily delivers

Moving to a daily stream of **mixed, live** baskets fixes the shape of the problem:

1. **It is reality.** Each day’s basket is the real mix of qualifying changes across connected accounts — not a curated single-type puzzle. Representative because it *is* the production stream.

2. **It is hard to game.** Predictions lock before outcomes exist. The operator has no privileged view of the future. Bad faith would not help; the future is as opaque to us as to every miner.

3. **Coverage compounds faster.** Scoring every day, across change types and account types, expands prediction capability far more quickly than one sanitised class per week. That speed is not only a miner UX win — it is the commercial point of the subnet: better forecasts of “what happens if we make this change” mean better recommendations for customers, sooner.

4. **The miner loop tightens.** Scores land as older baskets mature; ship a better model and watch the moving average day by day; the champion is meant to stay live across the portfolio — which is the product.

5. **Outcomes stay final.** The settling window means a score you earn is not quietly re-measured later.

6. **Rewards concentrate on quality.** Under the weekly mechanism we scored and paid a long tail of qualifying miners — often **well over a hundred** in a given week. Each slice of emissions was thin enough that serious long-term effort was hard to justify. The daily mechanism (standing → published weight curve, capped earning set) concentrates rewards: **fewer miners may earn, but those who do earn materially more.** We want high-quality models and miners committed to the long-term success of the subnet — not a wide, dilute payout that underfunds the work.

### What this unlocks commercially

Accurate predictions across a **wide range of changes** on a **wide range of accounts** are not an academic scoreboard. They are the missing input for automated decisioning in Google Ads.

With that coverage we can:

1. **Recommend** the most appropriate **basket of actions** for an account at a given time — not a single sanitised lever in isolation.
2. **Execute** those changes (through AdTAO’s management path) when the operator and customer choose to run the champion live.
3. **Measure** executed, model-driven changes against human-managed baselines.

Our expectation is that a proven prediction model will **outperform unaided human optimisation**, because no human operator has a continuously scored, network-wide forecast of “what this change does next.” In the Google Ads space that is a rare commercial position: once the daily stream has produced trustworthy coverage, the subnet’s winning models become a durable optimisation advantage for customers — and a clear product story for AdTAO — that the weekly, single-type contest could not deliver at the same speed or realism.

### Why change now

We have been live in the Bittensor ecosystem for on the order of **ten weeks**. The weekly, single-type design did its job: it let miners and the subnet acclimatise, proved the integrity machinery, and taught us how value actually gets created here.

We are changing because we now know how to deliver **more value, faster** — for Bittensor (a harder, realer prediction contest with continuous weights and concentrated rewards for serious miners) and for AdTAO customers (live-basket coverage that compounds into recommend → execute → measure against humans). The daily stream is that next step.

---

## What stays the same — and what goes

**Stays**

- **Scoring components:** 50% quantile accuracy, 20% calibration, 15% direction, 15% goal metric — each prediction judged against that account's own goal ([SN21_SCORING.md](./SN21_SCORING.md)).
- **Integrity:** predictions lock before outcomes exist; signed / anchored outcomes; independent validation. The commit shape evolves (model digest + sandbox run), but the guarantee does not.
- **Ranking via moving average.** One good or bad day moves little. Consistency wins.
- **Predictable rules.** The weekly epoch is retired, but its real protection stays: parameters are reviewed every **four weeks** on a published calendar, changes are announced in advance, and nothing silent lands between reviews.

**Goes**

- Fixed weekly mining windows and waiting a week for the only score that matters.
- Weekly tier / EMA bands as the primary emission allocator (replaced by the daily weight curve — [SN21_REWARDS.md](./SN21_REWARDS.md)).
- Dust emissions to inactive UIDs that never participate on the daily stream or never meet the alpha hold.
- Wide, thin payouts across a very large qualifying miner set — replaced by a steeper curve and a smaller earning set so top models are worth building and defending.

---

## One thing to build for (roadmap)

Today, account types compete in one basket. Lead-gen and ecommerce accounts have different goals; scoring already respects that — every prediction is measured against its own account's goal.

At a **published trigger** — when ecommerce reaches a set share of daily volume, or when basket-level error patterns justify it — the basket will split into a lead-gen contest and an ecommerce contest, each with its own champion. Same subnet, same rules, two crowns. Trigger conditions will be published before they can fire. If you want to be first to a specialist crown, start building now.

---

## Cutover in brief

Full table: [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md).

| Phase | Idea |
| :---- | :---- |
| Through **9 Aug 2026** | Last weekly score still pays (indicative burn applies). |
| From **10 Aug** | **Bridge:** weekly-derived weights continue only for miners who **submit** on the daily stream and meet the **alpha hold**. |
| From **18 Aug / 25 Aug / 8 Sep** | Daily **7-day**, then **14-day**, then **28-day** settled scores feed standings. |
| Through **15 Sep** | Alpha hold ramps to **1,000**; burn plan steps down (indicative only). |

Burn rates are **planned and indicative only** and may change at any time to protect and grow alpha for holders.

**Cold start:** full scoring depth for a live basket is reached about **35 days** after that basket (28-day horizon + settle). Until then your standing is real but thinner — see scoring doc.

Leaderboard / changelog (site updating this week): [https://adtao.io/sn21/](https://adtao.io/sn21/).

---

## Still open (ops / site — not blockers for this doc)

- [ ] Derive Discord + site announcement from this file.
- [ ] Align any remaining governance-amendment / GAP-1 wording if still open.

---

*In AdTAO, we TRUST.*
