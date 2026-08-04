# SN21 — Staking & alpha hold

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners |
| **Status** | Authoritative for stake / hold requirements during and after cutover |
| **Last updated** | 2026-08-04 |
| **Update independently of** | [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |

This document explains the **alpha stake (hold) requirement**. It does **not** explain scoring or the emission curve.

---

## In one line

To remain eligible for emissions under the daily stream, you must **hold a published amount of subnet alpha** on your miner. There is **no upfront buy-in** — the requirement ramps on a published schedule, and earnings can fill the hold as you start winning.

## Why this exists

Skin in the game: earning slots should be backed by stake so inactive / squatting UIDs do not siphon emissions. Existing scoring miners are carried onto this path; you are not asked to buy a ticket to enter.

## Alpha hold ramp (published)

Amounts are **SN21 alpha** (subnet token), not TAO, unless a later notice says otherwise.

| Effective from | Minimum alpha hold |
| :---- | :---- |
| Through Sunday **9 August 2026** | **0** |
| Monday **10 August 2026** | **150** (start of ramp) |
| Tuesday **18 August 2026** | **300** |
| Tuesday **25 August 2026** | **450** |
| Tuesday **8 September 2026** | **700** |
| Tuesday **15 September 2026** onward | **1,000** (terminal floor) |

Cutover behaviour (who is paid if you miss the hold) is in [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md).

## How the hold works

- **No upfront buy-in.** You can start predicting without locking 1,000 alpha on day one.
- **Ramp.** The required hold steps up on the dates above.
- **Earnings path.** As your model earns, incentive can be directed into meeting the floor (capture / escrow path) until the published hold is met; then normal payouts resume.
- **Zero weight freezes obligation growth from earnings.** If you are not earning, nothing drains from a lock you never filled — but you also do not receive emissions until you meet the then-current hold and other eligibility rules.

## Native chain enforcement

Bittensor is rolling out native registration / collateral mechanisms. SN21 will use those when available. Until then, the subnet applies the published hold as an **eligibility rule for emissions** (soft bookkeeping → hard gate as announced). When native `min_locked` (or equivalent) activates, the same published floors apply; miners will be given clear notice of the switch.

## What staking does **not** change

- How predictions are scored → [SN21_SCORING.md](./SN21_SCORING.md)
- The weight curve shape → [SN21_REWARDS.md](./SN21_REWARDS.md)

## Related

- Transition dates (burn there is indicative only): [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)
- Rewards: [SN21_REWARDS.md](./SN21_REWARDS.md)
