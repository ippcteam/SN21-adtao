# Miner economics at a glance (SN21)

Short reference for how emissions relate to your behaviour. **Authoritative detail:** [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md) · [SN21_EPOCH_STRUCTURE.md](./SN21_EPOCH_STRUCTURE.md).

## Cadence

- **Epoch length:** 7 days; **payout** at epoch end.  
- **Phase / epoch type** (Search → PMax → …) follows the epoch structure doc; some counts are *indicative*; **announcements** (subnet social channels, Discord `#announcements`) run **≥2 weeks** before phase/consolidation changes (championships: **4 weeks**).  
- **Implementation:** On-chain weight timing and exact UTC cutovers follow Bittensor tempo — see team announcements when live; not all chain detail is in this repo.  

## How you get paid (v1 design)

1. **Pass the participation gate** (all must hold):  
   - Score **above the conditional prior baseline** (historical mean by action class — values published at epoch start).  
   - Submit on **≥80%** of episodes (`COVERAGE_GATE_FRACTION = 0.80` in `hope/validator/tiered_weights.py`).  
   - Meet **per-bucket** coverage (≥60% in each large `(campaign_type × resolution)` bucket; ≥3 submissions in small buckets).  

   **Note on two coverage thresholds:** the **scoring** library applies a separate **score-coverage** rule at `MIN_COVERAGE_FRACTION = 0.50` in `hope/scoring/scorer.py` — drop below 50% and your *raw score* is reduced via a quadratic penalty (independent of emissions). The 80% in this list is the **emission-qualification gate** in the tiered allocator. Pass both, or you forfeit emissions.

2. **Epoch type multiplier** scales the week’s “pie” (Search campaign-level = 1.0×; consolidation / championship **higher** — see full table in reward spec).

3. **Tiers** (when **≥15** qualifying miners; else single proportional pool):  
   - **Elite** top ~20% by EMA, **and** must clear **quality floor** (EMA ≥ baseline + 1·σ) → **~60%** of miner emissions.  
   - **Competitive** next ~40% → **~30%**.  
   - **Participating** remainder → **~10%**.  
   - **Within a tier:** split by **this epoch’s** score.  
   - **EMA:** four-epoch window, α = 0.5 (from epoch 4 onward; earlier weeks have shorter windows — see reward doc).  

4. **Small diversity bonus** (up to +0.05) if your outputs differ from the cohort median in a useful way; copying is still discouraged by the full anti-copy and collusion rules.

## How you are scored (episode level)

- **50%** quantile (pinball on P10/P50/P90) · **20%** calibration · **15%** directional · **15%** goal metric.  
- **Horizon mix** and **7/14/28d** weights depend on **measurement resolution** (high / medium / low).  
- **Episode weight** in the mean = `resolution_weight × campaign_type_weight` (Epoch Structure).  

**This repo** (`hope/scoring`) implements the *scoring* shape used for development and testing; **tiering, EMA, and on-chain emissions** are specified in the reward doc and may be enforced by the production validator / runtime — check release notes for parity.

## Practical checks before you commit effort

- Read the **worked alpha example** in [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md) (illustrative; α→TAO is market-driven).  
- **Baseline values** and **gate thresholds** are **published before** the epoch opens.  
- **No commitment** in this doc to a **formal third-party audit schedule**; transparency and any future program are described in the reward spec.  

## Related

- Task & API detail: [miner_quickstart.md](./miner_quickstart.md)  
