# Bittensor Subnet (SN21) — Reward Mechanism

| | |
| :---- | :---- |
| **Version** | 1.2.1 |
| **Last updated** | 2026-04-27 |
| **Authoritative for launch** | Yes — gates, tier shape, EMA, governance. |
| **Companion** | [SN21_EPOCH_STRUCTURE.md](./SN21_EPOCH_STRUCTURE.md) |

**Implementation note:** This document is the authoritative spec for the launch reward mechanism *as designed*. The default `hope-validator` CLI at launch ships a simpler path — score-normalization + 95% burn — while the chain-side scoring pipeline is exercised end-to-end during the first operational cycle. The full tiered allocator is implemented in `hope/validator/tiered_weights.py:TieredAllocator` and unit-tested. The chain validator runner now wires it behind the `SN21_TIERED_WEIGHTS` opt-in — when enabled, the per-miner weight vector is built via the participation gate + Elite/Competitive/Participating pools (single proportional pool under 15 qualifying miners) instead of flat score-normalization (the legacy `WeightSetter(tiered_allocator=TieredAllocator())` path remains for the HTTP runner). The gate's baseline is the predict-zero score against the epoch truth. It defaults off pending Review 1, after which it becomes the default.

## Purpose

This document defines how TAO emissions are allocated to miners on SN21: v1 launch structure, worked economics, standard review cadence, and emergency intervention protocol.

The reward mechanism is designed to:

- Attract capable prediction model builders regardless of Google Ads domain depth  
- Reward predictive capability over gaming or low-effort participation  
- Provide incentives that scale with epoch complexity and model sophistication  
- Stay simple enough at launch that miners can reason about expected returns  
- Evolve through evidence-based reviews rather than arbitrary adjustment  

## Governing principles

### This is a prediction model problem

SN21 sits on a Google Ads data surface, but miners are prediction specialists, not ad practitioners. Domain familiarity helps; it is not required.

### Prediction accuracy is the business product

The operator’s decision engine handles action selection. Miners predict outcomes given account state and action bundles. What miners are scored on matches what the operator derives value from.

### Simple at launch, refined with evidence

v1 is intentionally simple. Complexity deters miners who need to model returns before investing effort.

### Centrally governed at v1, procedurally transparent

SN21 is centrally governed by the subnet operator at launch. Parameter changes are published with rationale and lead time. Decentralisation is on the Review 4 agenda. Governance is transparent and structured, but centralised until then.

What is centralised at v1 is the canonical scoring spec and the operator-run canonical primary + shadow validators — not the on-chain consensus that determines emissions. Weight aggregation on SN21 is already multi-validator: every registered validator submits its own weight vector and Yuma stake-weighted median combines them. Convergence of registered validators on the canonical scoring code is what Phase 3 of the validator architecture (see whitepaper §10.2) is about.

## Validator architecture

Validator registration on SN21 is **open by Bittensor protocol** — any operator meeting the chain's permit and stake requirements can register and submit weights. At launch, the AdTAO operator runs the canonical primary and shadow validators against the published scoring specification, and is coordinating with other registered validator operators to converge on canonical scoring. Validator decentralisation criteria — what a formal third-party operator programme looks like — is on the Review 4 agenda.

The scoring implementation is open source: anyone can clone the repo and run the validator binary locally against the chain to reproduce scoring. A formal third-party validator programme (deployment guide, scoring spec reference implementation, operator coordination channels) is tracked at Review 4.

### Open-source scoring code

Validator and scoring code live in this repository. The repo includes the scoring implementation, reproducibility paths, and versioned changes announced through the review cadence before taking effect when applicable.

Miners can run scoring locally to verify behaviour against published inputs where exposed by the deployment.

### Transparency and external review (no fixed program yet)

**What we commit to now:** open code, published scoring rules (this document and companion specs), and clear communication when parameters change.

**What we are not committing to yet:** a standing independent third-party audit panel, fixed quarterly audit cadence, NDA-governed auditor access to holdout data, or dispute-triggered forensic reviews. Those remain **under intentional evaluation** before mainnet and beyond. When and if a formal external review or audit program is adopted, it will be announced with scope, cadence, and constraints — not implied by this document.

## V1 launch structure

The v1 reward mechanism has four components: participation gate, tiered emission bands, epoch type multipliers, and a diversity bonus.

### Component 1 — Participation gate

A miner must meet **all** of the following to earn emissions in an epoch:

1. **Beat the conditional prior baseline.** Per episode, the baseline is the historical mean actual outcome for the same `(campaign_type, action_type)` over a rolling training window. Mean episode score for the epoch must exceed this baseline or emissions for that epoch are zero regardless of rank.  
2. **Submit on at least 80% of episodes** in the epoch (anti cherry-picking).  
3. **Meet per-bucket coverage.** For each `(campaign_type × measurement_resolution)` bucket with ≥10 episodes, submit on ≥60% of episodes in that bucket. For buckets with &lt;10 episodes, submit on at least three (or all if fewer than three).  

**Why conditional prior vs predict-zero:** Predict-zero is too easy with trivial heuristics. The conditional prior forces improvement over a transparent historical average for the action class. Baseline values per episode are published at epoch start.

**Evolution:** The conditional prior is the v1 launch baseline. As operator-managed episodes grow, the baseline may shift toward platform system estimates (announced ≥4 weeks in advance).

Gates and thresholds are published before each epoch opens.

### Component 2 — Tiered emission bands

#### Tier placement (EMA)

From epoch 4 onward, tier placement uses an exponentially-weighted moving average of the last four epoch scores with α = 0.5:

`EMA = 0.50×current + 0.25×(t−1) + 0.125×(t−2) + 0.0625×(t−3)`

Cold start: epoch 1 current only; epoch 2 two-epoch average; epoch 3+ full EMA.

#### Tier assignment

| Tier | Rank | Additional rule | Share of emissions | Within-tier order |
| :---- | :---- | :---- | :---- | :---- |
| **Elite** | Top 20% | EMA ≥ baseline + k·σ (k = 1.0) | 60% | Current-epoch score |
| **Competitive** | Next 40% | — | 30% | Current-epoch score |
| **Participating** | Bottom 40% | — | 10% | Current-epoch score |

**Elite quality floor:** If no one in the top 20% meets the k = 1.0 floor, Elite is empty for that epoch. Elite’s pool redistributes to Competitive and Participating in ratio 30:10 (of the unused 60%: 30/40 to Competitive, 10/40 to Participating). σ is from the distribution of `(miner_score − baseline_score)` for qualifying miners. k is reviewed at Review 2.

**Within tier:** `miner_emission = tier_pool × (miner_score / Σ scores_in_tier)`.

**Minimum pool for tiers:** If fewer than 15 miners pass the gate, all qualifying miners share one proportional pool (no tier split). Elite floor rules still apply when the pool is large enough.

**Tie-break at tier boundary:** (1) higher current-epoch score, (2) earlier submission time, (3) lower UID.

**Why tiers:** Flatter than a continuous exponential rank curve; clearer step targets for the middle of the pool.

### Component 3 — Epoch type multiplier

Scales epoch emissions before tier split; reflects difficulty and strategic value.

| Epoch type | Multiplier | Rationale |
| :---- | :---- | :---- |
| Search — campaign-level | 1.0 | Baseline |
| Search — sub-campaign | 1.2 | Redistribution, medium resolution |
| PMax | 1.5 | Opaque, noisy, high value |
| Shopping | 1.3 | Different feature space |
| Consolidation | 2.0 | Cross-campaign |
| Championship | 3.0 | Max complexity, quarterly |

Multipliers are published with epoch announcements.

**Championship vs consolidation:** Championship uses multi-type accounts, simultaneous cross-type actions, account-level goal accuracy over 28d. Consolidation tests cross-type interactions within defined pairings.

### Component 4 — Diversity bonus

Small additive bonus (up to 0.05) on emissions when a miner’s predictions differ meaningfully from the cohort median while staying within accuracy bounds — so pure copying is dominated. Heavier anti-copy handling lives in the prediction model spec and Emergency Trigger 1.

## Scoring formula

Per-horizon episode score:

`episode_horizon_score = 0.50×quantile + 0.20×calibration + 0.15×directional + 0.15×goal`

**Quantile (50%):** Pinball loss on P10, P50, P90.  
**Calibration (20%):** Target interval coverage with convex width penalty.  
**Directional (15%):** Correct direction of primary goal metric.  
**Goal (15%):** P50 on the account’s primary goal metric.  

**Implementation status.** The launch scorer (`hope/scoring/onchain_adapter.py:score_one_miner`) shipped a simplified `0.7×CRPS + 0.3×Brier` proxy whose quantile term is band-insensitive (a perfect P50 and one a few percent off score identically, compressing the dynamic range). The full four-component score above is implemented in `score_one_miner_v2` (pinball quantile + coverage/width + directional + P50-goal) and enabled via `SN21_SCORING_V2` (default off; the matching verifier must set the same flag — recording the scorer version on chain is a tracked follow-up). It is scheduled to become the default after Review 1.

**Horizon aggregation** (by measurement resolution).

**Launch (Phase 1)** uses 7-day and 14-day horizons only:

| Resolution | 7d | 14d |
| :---- | :---- | :---- |
| High | 0.40 | 0.60 |
| Medium | 0.35 | 0.65 |
| Low | 0.30 | 0.70 |

These weights match `hope/constants.py:HORIZON_WEIGHTS`.

**Future (post-launch)** — when the 28-day horizon is added, the table is expected to evolve toward the schedule below. **These values are roadmap, not launch behavior:**

| Resolution | 7d | 14d | 28d |
| :---- | :---- | :---- | :---- |
| High | 0.20 | 0.35 | 0.45 |
| Medium | 0.15 | 0.30 | 0.55 |
| Low | 0.00 | 0.20 | 0.80 |

A 28-day horizon addition will be announced ≥4 weeks ahead of activation.

**Epoch score:** episode-weighted mean:  
`miner_score = Σ(episode_score × episode_weight) / Σ(episode_weight)`  
Episode weights: see Epoch Structure (`resolution_weight × campaign_type_weight`).

Full numeric detail: `prediction_model_spec` (if published) and [miner_quickstart.md](./miner_quickstart.md) for the code path in this repo.

## Pre-launch red-team simulation

Before mainnet, scoring is stress-tested with synthetic strategies (null, action-prior, interval inflation, copy variants, bucket avoider, etc.). **Pass bar:** no gaming strategy 1–8 may beat median emissions in 20/50/100-miner sims; honest-weak should beat gaming in larger pools. Findings and code location will be published when the program is run — **separate** from any future third-party audit program (see *Transparency* above).

## Review cadence

Structured review every **four** epochs (roughly four-week blocks). Parameter changes do not apply mid-epoch; they start at the next epoch. Outcomes go to subnet social channels and SN21 Discord when locked.

- **Review 1 — after epoch 4:** Tiers, baseline, Elite floor.  
- **Review 2 — after epoch 8:** Consolidation multiplier effects; optional calibration multiplier candidate; optional tier boundary smoothing.  
- **Review 3 — after epoch 12:** Diversity bonus; anti-copy stack interaction.  
- **Review 4 — after epoch 16:** Championship parameters; **validator decentralisation criteria** (roadmap).  

## Emergency intervention protocol

### Trigger 1 — Gross collusion

Cluster of 5+ miners with pairwise similarity above a disclosed mechanism threshold across a material share of episodes, inconsistent with independent work. **Response:** suspend weights for the epoch, 72h contestation, published outcome, zero emissions if confirmed, reinstate if not. Threshold details withheld; mechanism class disclosed.

### Trigger 2 — Dataset quality misalignment

Pool average inconsistent with data error pattern vs miner failure. **Response:** stop scoring affected batch, investigate, follow **epoch-local rules:** rescore within 72h, drop bad episodes, or **cancel epoch** (emissions burned; EMA treats score as missing, not zero). **No** forward redistribution to the next epoch.

## Worked economics example

Illustrative only — alpha/TAO is market- and protocol-determined.

Assume **7,200 α/day** → **50,400 α per week** at subnet level; **41%** to miners → **20,664 α** miner pool for a **1.0×** Search week. With **50** qualifying miners and full Elite (10 / 20 / 20 by tier), tier pools follow 60% / 30% / 10% of 20,664 α (see version 1.1 tables in project history for per-tier averages). If Elite is empty, pools redistribute as in Component 2.

**Alpha → TAO** depends on subnet performance and network dynamics; the subnet operator does not control conversion.

**Early-miner note:** New subnets often have cheaper alpha in early epochs; that is a structural market dynamic, not a promise of price appreciation.

## Future reward components (not active at launch)

- **Calibration multiplier** (Review 2 candidate): 1.0–1.2 for exceptional calibration.  
- **System-estimate baseline:** phased replacement of conditional prior.  
- **Economic-value weighting** (post–Review 4 candidate): bounded spend/impact weighting.  

## Summary reference

| Component | v1 | Parameter | Review |
| :---- | :---- | :---- | :---- |
| Gate — baseline | Active | Beat conditional prior | 1 |
| Gate — coverage | Active | 80% + per-bucket rules | 1 |
| Tiers | Active | 20/40/40, 60/30/10 | 1 |
| Elite floor | Active | EMA ≥ baseline + 1.0·σ | 1 |
| EMA | Active | 4-epoch, α=0.5 | 2 |
| Epoch multipliers | Active | 1.0–3.0 | 2 |
| Diversity bonus | Active | 0–0.05 | 3 |
| Min pool for tiers | Active | 15 | 1 |
| Open-source code | Active | SN21-adtao | — |
| External audit program | **Not committed** | TBD / intentional | — |
| Red-team simulation | Pre-launch goal | 9-strategy bar | — |
| Epoch-local accounting | Active | rescore / drop / cancel | — |

## Revision history

| Version | Date | Changes |
| :---- | :---- | :---- |
| 1.0 | 2026-04-16 | Initial |
| 1.1 | 2026-04-16 | Validator section; EMA; tiers; diversity; economics example |
| 1.2 | 2026-04-17 | Conditional prior; Elite floor; Championship scope; collusion/epoch rules; **repo** |
| 1.2.1 | 2026-04-27 | Repo copy: third-party **audit program descoped** to transparency + TBD; canonical link SN21-adtao |
