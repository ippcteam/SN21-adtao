# SN21 — Staking & alpha hold

| | |
| :---- | :---- |
| **Version** | 1.2 |
| **Audience** | Miners |
| **Status** | Authoritative for stake / hold requirements during and after cutover |
| **Last updated** | 2026-09-13 |
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

## Enforcement (in force from 13 September 2026)

The hold is applied as a **hard gate on the daily weight vector**, on the subnet's own enforcement rather than a native chain mechanism:

- Each day, before the paid set is formed, every placement-eligible hotkey's **alpha held on the subnet** is read from the metagraph and compared with the hold in force that day (table above). A hotkey **below the hold is not seated** that day; the next-ranked miner who meets the hold takes the seat, so the paid set keeps its published size. Standing, scores and receipts are untouched: this is eligibility for payment, not a score.
- The same check runs again on the validator at commit time, against the metagraph at that moment.
- The check **fails open**: a hotkey the chain cannot be read for is kept and named in the audit as `unreadable_kept`; if the check would leave nobody earning it is not applied and the audit says why.
- Meeting the hold again restores eligibility from the **next daily run**; nothing is owed for the days missed and nothing is paid back for them.

The working is published every day in the allocation audit (`/v1/daily/{day}/allocation-audit`, block `alpha_hold`): the floor in force, whether the gate was enforced on that vector, each hotkey found below the floor with the alpha the gate read, and the list actually unseated. `controls.alpha_hold` carries the same status as a count, alongside the other controls, and a miner's row in the daily report names the control when it acted on them.

| Date | Change | Effective |
| :---- | :---- | :---- |
| 13 September 2026 | The published hold becomes a hard gate on paid weight, as described above. Until this date the hold was recorded but not applied to weights. | Validator commits from the evening of 13 September 2026 (UTC) and the 14 September run onward; never retroactively |

## Native chain enforcement

Bittensor is rolling out native registration / collateral mechanisms. SN21 will use those when available. Until then, the subnet applies the published hold as the **eligibility rule for emissions** described above. When native `min_locked` (or equivalent) activates, the same published floors apply; miners will be given clear notice of the switch.

## What staking does **not** change

- How predictions are scored → [SN21_SCORING.md](./SN21_SCORING.md)
- The weight curve shape → [SN21_REWARDS.md](./SN21_REWARDS.md)

## Related

- Transition dates (burn there is indicative only): [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)
- Rewards: [SN21_REWARDS.md](./SN21_REWARDS.md)
