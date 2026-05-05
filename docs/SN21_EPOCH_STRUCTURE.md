# Bittensor Subnet (SN21) — Epoch Structure

| | |
| :---- | :---- |
| **Version** | 1.2 |
| **Last updated** | 2026-04-17 |
| **Companion** | [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md) |

## Purpose

This document defines the episode and epoch progression for SN21: campaign type sequence, sub-campaign depth progression, consolidation rhythm, and principles for timeline flexibility.

The epoch structure is designed to:

- Attract and retain high-quality miners by starting with the cleanest, highest-signal episode types  
- Build miner model sophistication progressively before introducing harder prediction problems  
- Periodically test cross-campaign understanding through consolidation epochs  
- Provide a predictable public roadmap miners can plan development around  

## Governing principles

### Fixed weekly epochs

Every epoch is exactly one week. Payout occurs at the end of each epoch. This is non-negotiable: miners can model expected returns on a guaranteed weekly cycle regardless of phase, campaign type, or other variables. No epoch will be extended, shortened, or delayed.

### Progressive complexity

Each phase introduces a new dimension of difficulty — campaign type, sub-campaign depth, or cross-campaign interaction — only after miners have had time to establish a model at the current level. Complexity is additive, not replacing.

### Cumulative pool

When a new campaign type is introduced, earlier campaign types remain in the episode pool. Each phase adds to the active episode set. Consolidation epochs score miners on the full portfolio of active campaign types.

### Consolidation milestone trigger

Consolidation epochs activate when there are at least two active campaign types in the pool and sufficient episode volume across both to make cross-campaign scoring meaningful. Consolidation epochs are the most complex and highest-emission epoch in a given cycle.

### Phase flexibility

The number of weekly epochs within a phase is indicative. The operator may extend a phase when episode volume, miner pool readiness, or infrastructure warrant it. Compression is possible but rare. Two commitments are firm:

- Every phase has a minimum of two weekly epochs before it can be consolidated or succeeded.  
- Any phase change is announced at least two weeks before activation.  

Extension does not affect payout cadence. Miners are paid every week regardless of phase extension.

## Episode dimensions

Every episode is characterised by five dimensions. These determine difficulty, scoring weight, and which epoch the episode belongs to.

| Dimension | Values | Notes |
| :---- | :---- | :---- |
| **Campaign type** | Search, PMax, Shopping, Video/Display | Primary epoch filter |
| **Action type** | Budget, bid strategy, keyword, creative, structural, targeting | Sub-phase filter |
| **Blast radius** | Parent-equivalent, significant, batch, single | Determines measurement resolution |
| **Measurement resolution** | High, medium, low | Derived from blast radius and action scope |
| **Action source** | System-recommended, manual, google_auto | Affects baseline availability |

### Measurement resolution guide

| Resolution | Condition | Scoring implication |
| :---- | :---- | :---- |
| **High** | All actions campaign-level or above | Outcome directly measurable. Full scoring weight. |
| **Medium** | Mixed levels, or sub-campaign with impact ratio ≥ 0.40 | Moderate signal. Reduced 7-day horizon weight. |
| **Low** | All sub-campaign with impact ratio < 0.10 | Signal buried in noise. Directional accuracy is primary. Zero 7-day weight. |

### Episode weight formula

Episode weight is deterministic from two factors:

`episode_weight = resolution_weight × campaign_type_weight`

**Resolution**

| Resolution | Weight |
| :---- | :---- |
| High | 1.0 |
| Medium | 0.7 |
| Low | 0.4 |

**Campaign type**

| Campaign type | Weight |
| :---- | :---- |
| Search | 1.0 |
| PMax | 1.0 |
| Shopping | 1.0 |
| Video/Display | 0.8 |

Video/Display is slightly discounted because measurement noise reduces informativeness of scores, not because predictions are less valuable. No other factors (account spend, industry, etc.) affect episode weight. Miners can compute every episode’s weight before submitting.

### Scoring baseline

At launch, many episodes come from accounts not managed through the operator's decision engine. Where no platform system estimate exists, **episode-level** comparison uses the **conditional prior** and related rules in [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md). The platform system-estimate baseline will be introduced progressively as operator-managed episodes grow as a share of the pool.

## Episode difficulty spectrum

| Tier | Campaign | Action scope | Resolution | Relative difficulty |
| :---- | :---- | :---- | :---- | :---- |
| 1 | Search | Campaign-level (budget, bid strategy, pause) | High | Easiest — launch baseline |
| 2 | Search | Sub-campaign (ad group, keyword, match type) | Medium | Moderate — portfolio redistribution |
| 3 | PMax | Any | Medium–low | Hard — opaque optimisation |
| 4 | Shopping | Any | Medium | Moderate-hard — different feature space |
| 5 | Multi-type | Cross-campaign interactions | Mixed | Hardest — consolidation epochs |
| 6 | Video/Display | Any | Low | Hard — weakest measurement |

## Epoch progression

### Phase 1 — Search (minimum 4 weekly epochs; firm: first 2)

**Objective:** Establish baseline prediction quality on the cleanest, highest-signal episode type.

**Epochs 1–2: Campaign-level actions only**

Episodes: Search campaigns with parent-equivalent or significant blast radius — budget changes, bid strategy transitions, campaign-level pauses and enables. Measurement resolution: high. Learning-period dynamics (e.g. bid strategy changes and 7–14 day volatility) are the main complexity.

**Epochs 3–4: Sub-campaign actions**

Search extended to ad group and keyword-level actions. Resolution drops to medium/low for those episodes. Portfolio redistribution becomes the main differentiator.

No consolidation epoch in Phase 1 (only one campaign type active).

### Phase 2 — PMax (minimum 3 weekly epochs; firm: first 2)

**Objective:** Introduce the hardest single-type problem and build cross-type volume ahead of first consolidation.

PMax is automated, opaque, and noisier than Search. PMax episodes carry higher emission weight via epoch type multiplier (see Reward Mechanism). Search episodes stay in the pool. Announced two weeks in advance.

### First consolidation: Search + PMax (1 weekly epoch, indicative)

Cross-campaign episodes from accounts running Search and PMax. Miners model budget flow, interaction effects, and portfolio-level goals. Highest-emission epoch in the cycle.

### Phase 3 — Shopping (minimum 3 weekly epochs; firm: first 2)

**Objective:** Third campaign type, different feature space (feeds, products, shopping-specific bidding). Search and PMax remain active.

### Second consolidation: Search + PMax + Shopping (1 weekly epoch, indicative)

Full three-type portfolio consolidation.

### Phase 4 — Video/Display (indicative)

Lowest volume, weakest measurement; directional accuracy dominates. Specialist category with elevated emission weight.

### Ongoing — Specialist and Championship epochs

- **Monthly specialist epochs:** Focused action types across campaign types; announced two weeks ahead.  
- **Quarterly championship epochs:** Maximum complexity — multi-type accounts, simultaneous cross-type actions, account-level goal accuracy over 28-day horizon (see Reward Mechanism for distinction vs consolidation).  

**Calendar collision:** If specialist and championship fall in the same week, championship wins; specialist moves to the following week.

## Timeline summary

| Epoch | Phase | Focus | Status |
| :---- | :---- | :---- | :---- |
| 1–2 | 1 — Search | Campaign-level actions | **Firm** |
| 3–4 | 1 — Search | Sub-campaign actions | Indicative |
| 5–6 | 2 — PMax | PMax introduced | **Firm** |
| 7 | 2 — PMax | PMax sub-actions | Indicative |
| 8 | Consolidation 1 | Search + PMax | Indicative |
| 9–10 | 3 — Shopping | Shopping introduced | **Firm** |
| 11 | 3 — Shopping | Extended | Indicative |
| 12 | Consolidation 2 | Search + PMax + Shopping | Indicative |
| 13+ | 4 — Video/Display | Introduced | Indicative |
| Ongoing | Specialist | Action-type focus | Indicative |
| Quarterly | Championship | Max complexity, highest emissions | Indicative |

Indicative epochs may be added if the operator extends a phase. Each epoch is exactly one week; payout at the end of every epoch.

## Epoch announcement protocol

Each new phase and each consolidation epoch is announced at least two weeks before activation. Championship epochs: four weeks in advance.

Announcements are published on:

- Subnet social channels — technical framing and prep  
- SN21 Discord `#announcements` — full detail including episode mix and emission weights  
- Miner quickstart — updated before new episode types go live  

## Revision history

| Version | Date | Changes |
| :---- | :---- | :---- |
| 1.0 | 2026-04-16 | Initial |
| 1.1 | 2026-04-16 | Fixed weekly epochs; phase flexibility; deterministic episode weight; collision rule; reward cross-refs |
| 1.2 | 2026-04-17 | Championship scope vs consolidation; aligned with reward mechanism 1.2 |
