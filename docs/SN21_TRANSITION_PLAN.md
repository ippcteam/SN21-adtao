# SN21 — Transition plan (weekly → daily)

| | |
| :---- | :---- |
| **Version** | 1.4 |
| **Audience** | Miners |
| **Status** | Authoritative for cutover dates and bridge rules |
| **Last updated** | 2026-08-20 |
| **Update independently of** | [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_STAKING.md](./SN21_STAKING.md) |

This document is the **cutover schedule** from the last weekly epoch to the daily prediction stream. Steady-state scoring and rewards are in the companion docs; this file is only about **what happens when**.

**Daily wall clock:** miner submissions use a **midnight EST** cut-off each day (same clock as the daily basket / prediction-lock cycle). See [miner_quickstart.md](./miner_quickstart.md) §3 and [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md).

---

## Where we are

| Fact | Detail |
| :---- | :---- |
| Last weekly epoch concluded | **Sunday 2 August 2026** |
| Last weekly scored | **Monday 3 August 2026** |
| Weekly payout window | **Monday 3 August noon EST → midnight EST Sunday 9 August 2026** |
| Daily stream starts | **Tuesday 4 August 2026** — the day the first live basket was delivered |

> **Two dates, and they are different.** A basket is named for the day whose
> changes it contains, and it is delivered the **next** morning. The first live
> basket is `BD-2026-08-03`: it covers **Monday 3 August** and was delivered on
> **Tuesday 4 August**. Every basket follows that pattern — `BD-<date>` always
> means "the changes that happened on `<date>`", delivered the morning after.
> Scoring clocks run from the basket's own date, which is why the first 7-day
> scores land on 18 August (3 Aug + 1 + 7 + 7).

After the bridge period, emissions follow [SN21_REWARDS.md](./SN21_REWARDS.md). Stake rules: [SN21_STAKING.md](./SN21_STAKING.md).

---

## Master timeline

All dates **2026**. Times in **EST** where stated.  
**Alpha** = minimum hold that day ([SN21_STAKING.md](./SN21_STAKING.md)).  
**Burn** = planned share of miner emissions burned (not paid to miners) — **indicative only** (see note below).

### Burn rates — planned and indicative only

Burn percentages in this document are a **working plan**, not a guarantee.

Bittensor’s emissions-allocation methodology is changing significantly. SN21 may **adjust burn at any time** — up or down, on or off the dates below — in order to **protect and grow alpha value for all holders**. When a burn change is made, it will be announced through the usual miner channels as soon as practical; miners should not model returns as if the table below were fixed.

| Date | What happens | Burn *(indicative)* | Alpha |
| :---- | :---- | ---: | ---: |
| **Sun 2 Aug** | Last weekly epoch **concludes**. | — | 0 |
| **Mon 3 Aug** | Last weekly epoch **scored**. | 45% | 0 |
| **Mon 3 Aug noon EST → Sun 9 Aug midnight EST** | Emissions paid **as normal** from that weekly score. | 45% | 0 |
| **Tue 4–5 Aug** | **Training bundle** published — settled weekly-era episodes with full 7/14/28-day outcomes (later refreshed to 10,791 records). **Fetch (superseded for training):** [`SN21_training_bundle.jsonl`](https://github.com/ippcteam/SN21-adtao/releases/tag/training-bundle-2026-08). | 45% | 0 |
| **Tue 4 Aug** | First **live daily basket** delivered — `BD-2026-08-03`, covering Monday 3 August. | 45% | 0 |
| **Mon 10 Aug** | **Bridge starts:** weekly score still drives weights, but only **bridge-eligible** miners are paid (see below). | **30%** | **150** |
| **Tue 18 Aug** | First **daily 7-day** settled scores begin feeding payouts. | 30% | **300** |
| **Wed 20 Aug** | **Rich training data v2** published — 29,129 reconstructed daily-stream episodes, all change types (not only budget/pause), 60-day baseline + 8-week series, archetypes, source mix. The same expanded types are **already in live daily baskets** from this date. **Fetch:** [`SN21_rich_training_v2.jsonl`](https://github.com/ippcteam/SN21-adtao/releases/tag/training-v2-2026-08). How to train: [SN21_TRAINING.md](./SN21_TRAINING.md). | 30% | 300 |
| **Tue 25 Aug** | First **daily 14-day** settled scores begin feeding payouts. | **15%** | **450** |
| **Tue 8 Sep** | First **daily 28-day** (35-day settled) scores begin feeding payouts. | 15% | **700** |
| **Tue 15 Sep** | **Terminal alpha hold** in force; burn steps to target. Steady-state daily stream. | **0%** | **1,000** |

### Same timeline — stake ramp only

| Effective from | Minimum alpha hold |
| :---- | ---: |
| Through 9 Aug | 0 |
| 10 Aug | 150 |
| 18 Aug | 300 |
| 25 Aug | 450 |
| 8 Sep | 700 |
| 15 Sep onward | **1,000** |

### Same timeline — burn only (indicative)

| From | Planned burn (indicative) |
| :---- | ---: |
| 3 Aug → 9 Aug (weekly payout window) | 45% |
| 10 Aug → 24 Aug | 30% |
| 25 Aug → 14 Sep | 15% |
| 15 Sep onward | **0%** |

These figures may change without waiting for the next four-weekly parameter review if needed to protect alpha.

### Same timeline — when daily horizons first pay

| Horizon | First payout date |
| :---- | :---- |
| 7-day | **Tue 18 Aug** |
| 14-day | **Tue 25 Aug** |
| 28-day (35 settled) | **Tue 8 Sep** |

### Emissions while horizons are still ramping in — **full pool, thinner signal**

Qualifying miners are **not** paid a reduced share of emissions just because only 7-day (or only 7+14-day) rows have settled yet.

- After the indicative **burn**, **100% of the miner emission pool** still flows through current weights (bridge, then the daily curve) to **eligible** miners.
- Horizons are **not** separate emission pots. We do **not** “give 100% to 7-day, then resplit when 14-day arrives.”
- What ramps is **evidence in your standing**: each settled (episode × horizon) enters at its published blend weight (see [SN21_SCORING.md](./SN21_SCORING.md)). Early on you have fewer rows, so standings are thinner / noisier — but the pie paid to the earning set is still the full post-burn miner pool.
- When 14-day and 28-day rows land, they **add** to standings; they do not unlock a larger emission budget by themselves.

You can still earn **zero** if you fail bridge eligibility, miss the alpha hold, or (once on the curve) fall outside the earning set — that is eligibility, not a horizon-based haircut on the pool.

---

## Bridge eligibility (from Monday 10 August)

From **Monday 10 August**, carried-over weekly weights are paid **only** to miners who meet **both** of the following.

### 1. Submitted (participating on the daily stream)

Days follow the **midnight EST** wall clock. A miner **submitted** on a day if, when the subnet ran a live daily basket that day:

- they delivered valid predictions for at least **75%** of the episodes in that day’s basket  
  (`predictions_out / episodes_in ≥ 0.75`), **and**
- delivery means **usable prediction payloads**, not merely “the container exited 0”.

**Not counted against you:**

- Days the subnet did not ship a basket (operator / infrastructure outage) — **subnet-down**, not a miner miss.
- Days with an empty basket (nothing to predict).

**Miss decay on the bridge:**

| Consecutive missed live days (ignoring subnet-down) | Bridge weight multiplier |
| :---- | ---: |
| 0 | 100% |
| 1 | 50% |
| 2 | 25% |
| 3+ | **0%** |

Only **consecutive misses at the end of the window** matter: miss Monday, submit every day since → you are participating again.

**“Submitted some scores” (bridge):** at least one **submitted** live day under the rule above, and consecutive-miss streak below the zero threshold.

### 2. Hold criteria (alpha stake)

Hold at least the **alpha required that day** (table above).  
Fail the hold → **not paid**, even if you submitted.

---

## What miners should do

| When | Action |
| :---- | :---- |
| **From 4 Aug** | Run against **live daily baskets**. |
| **Before 10 Aug** | Be ready to submit every live day; hold **≥150 alpha**. |
| **From 20 Aug** | Train on the **rich v2 bundle**. Expanded change types are already in live daily baskets; the weekly-era bundle does not cover them. |
| **Through 15 Sep** | Follow the stake ramp; expect 7d → 14d → 28d settlements on the dates in the master table. |

Scoring & rewards: [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_REWARDS.md](./SN21_REWARDS.md).

---

## What ends / what stays

**Ends:** weekly mining windows after the 3–9 Aug payout window; weekly tier/EMA as the primary allocator; dust to inactive UIDs that never participate or never meet the hold.

**Stays:** sealed predictions before outcomes; reproducible scores; published scoring components; announced parameter changes (four-weekly reviews in steady state).

---

## Related

- [SN21_WHY_DAILY.md](./SN21_WHY_DAILY.md)
- [SN21_TRAINING.md](./SN21_TRAINING.md)
- [SN21_SCORING.md](./SN21_SCORING.md)
- [SN21_REWARDS.md](./SN21_REWARDS.md)
- [SN21_STAKING.md](./SN21_STAKING.md)
