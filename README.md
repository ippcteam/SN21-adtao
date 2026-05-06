<div align="center">

# **SN21 — Impact Prediction Subnet**

### Predict ad campaign outcomes. Earn from accuracy alone.

Every prediction is sealed on chain before the outcome is knowable.
Every score is reproducible by anyone with a chain reader.
No one — including the operator — can rewrite the record after the fact.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

Bittensor Subnet 21 · Mainnet `finney` · Testnet `test` (netuid 466)

[Whitepaper](docs/whitepaper.md) · [Reward mechanism](docs/SN21_REWARD_MECHANISM.md) · [Miner quickstart](docs/miner_quickstart.md)
</div>

---

- [Overview](#overview)
- [How it works](#how-it-works)
- [What you predict](#what-you-predict)
- [How scoring works](#how-scoring-works)
- [How emissions work](#how-emissions-work)
- [Repository structure](#repository-structure)
- [Quick start (miners)](#quick-start-miners)
- [Verifying any epoch](#verifying-any-epoch)
- [Tests](#tests)
- [License](#license)

---

## Overview

SN21 is a verifiable prediction market for Google Ads campaign
outcomes, running on Bittensor. Miners predict P10/P50/P90
distributions over 7-day and 14-day campaign deltas. Validators score
those predictions against measured outcomes. Every step — predictions,
outcomes, scores, weights — is cryptographically anchored on chain.

Read the [Whitepaper](docs/whitepaper.md) for the full protocol.

### Two core guarantees

1. **Predictions are bound to the miner that produced them.** Every prediction is signed (`inner_sig`) and committed on chain before the outcome is knowable.
2. **Scoring is a deterministic function of public state.** Anyone running [`scripts/verify_epoch.py`](scripts/verify_epoch.py) can independently reproduce any validator's scoring decision.

---

## How it works

- **Miners** receive structured episodes (account state, action context, 60-day pre-window) and submit P10/P50/P90 distributions per (campaign × horizon). Submissions are AES-GCM encrypted, the AES key is timelock-encrypted to a future drand round, and the ciphertext SHA + key + archive URL are committed on chain.
- **Validators** read miner commits after the timelock reveals, fetch the encrypted predictions from a three-tier archive, run an 8-check scoreability rule, score against measured outcomes, and submit weights via `commit_timelocked_weights`.
- **Anyone** can re-run the verifier against any past epoch and confirm — or contradict — the validator's scoring.

---

## What you predict

You receive an **episode** — a structured snapshot of a Google Ads
account at a moment in time:

| Section | What's in it | Size |
|---|---|---|
| `episode_metadata` | ID, schema version, resolution, horizons | ~0.5 KB |
| `account_state` | Customer hash, goal, spend bucket, optional enrichment | ~1 KB |
| `pre_window` | 60-day campaign time series + account aggregates | ~8 KB |
| `action_bundle` | The action(s) being applied: type, magnitude, blast radius, risk | ~2 KB |
| `campaign_metadata` | Campaign type, bid strategy, status | ~0.3 KB |

You output **probabilistic distributions** (P10/P50/P90), not point estimates. You're rewarded for calibrated uncertainty.

### Phase 1 action types

Defined in [`hope/constants.py:LAUNCH_ACTION_TYPES`](hope/constants.py):

| Type | What it means |
|---|---|
| `BUDGET_CHANGE` | Daily budget increased / decreased |
| `BID_STRATEGY_CHANGE` | Bidding strategy switched |
| `TARGET_VALUE_CHANGE` | tCPA / tROAS target adjusted |
| `CAMPAIGN_PAUSE` | Campaign paused |

---

## How scoring works

Four components combine into one micro-units score per miner per epoch:

| Component | Weight | What it measures |
|---|---|---|
| Quantile accuracy | 50% | Pinball loss / CRPS on P10/P50/P90 vs actual |
| Calibration | 20% | Interval coverage with convex width penalty |
| Directional | 15% | Sign match on the primary goal metric |
| Goal accuracy | 15% | Brier score on goal-miss probability |

On top:

- **Null penalty** — up to 60% reduction for near-zero predictions.
- **Skill score** — must beat the conditional-prior baseline. Below baseline → zero emission.

The scoring library is pure Python with no Bittensor dependency:

```python
from hope.scoring import EpochScorer
scorer = EpochScorer()
scores = scorer.score_epoch(predictions, episodes, outcomes)
```

---

## How emissions work

**At launch.** The default `hope-validator` CLI runs simple
score-normalization with a 95% burn to UID 0. Tier mechanics
(participation gate, EMA tier placement, Elite floor, pool shares)
are implemented in [`hope/validator/tiered_weights.py`](hope/validator/tiered_weights.py)
and unit-tested, but **not** the runner default at launch — they're
opt-in via `WeightSetter(tiered_allocator=TieredAllocator())`. Tiers
become the runner default after Review 1.

Full spec: [SN21_REWARD_MECHANISM.md](docs/SN21_REWARD_MECHANISM.md).

---

## Repository structure

```
tao-discovery/
├── docs/
│   ├── whitepaper.md             Protocol design + trust model + adversarial matrix
│   ├── miner_quickstart.md       Miner onboarding tutorial
│   ├── validator_setup.md        Validator deployment guide
│   ├── SN21_REWARD_MECHANISM.md  Full reward spec (gates, tiers, EMA, governance)
│   ├── SN21_EPOCH_STRUCTURE.md   Phases, horizons, consolidation
│   └── MINER_ECONOMICS.md        Short reference for emissions
│
├── hope/
│   ├── protocol/                 Episode / Prediction / Outcome models
│   ├── commitment/               Crypto primitives (CBOR, IMT, ed25519, drand TLE, archive client, scoreability)
│   ├── scoring/                  Pure-Python scoring (4 components + skill score + null penalty + per-episode)
│   ├── miner/                    Miner SDK + runner + reference baseline model
│   ├── validator/                Validator runner, scoring orchestration, tiered weight allocator, FastAPI
│   ├── archive_server/           FastAPI archive (Tier-2 / Tier-3 storage)
│   ├── hope_outcomes/            Outcome signer (release_commit + reveal_blob)
│   └── hope_shadow_validator/    Shadow validator (independent scoring)
│
├── scripts/
│   ├── verify_epoch.py           Public verifier — anyone can audit any epoch
│   ├── score_predictions.py      Offline scoring (miners)
│   ├── train_example_model.py    Reference XGBoost training (miners)
│   ├── generate_training_data.py Pull a release into training format
│   └── sn21_keys.py              ed25519 key-management CLI
│
├── tests/
│   ├── adversarial/              12 attack scenarios with passing defences
│   ├── e2e/                      Full miner flow against a running validator
│   ├── commitment/               Crypto primitives
│   ├── scoring/                  Scoring components + adapter
│   ├── miner/                    Miner runtime
│   ├── validator/                Validator runtime
│   └── scripts/                  Public verifier
│
├── data/training/                10 sample episodes with known outcomes
├── min_compute.yml               Hardware requirements (miner + validator)
└── CONTRIBUTING.md               How to file PRs and propose protocol changes
```

---

## Quick start (miners)

```bash
# Clone + install
git clone <repo-url>
cd tao-discovery
pip install -e ".[miner]"

# Train on bundled sample data (10 episodes, known outcomes)
python scripts/train_example_model.py --data-file data/training/training_episodes.json

# Run miner against a live validator (specify --bt-network test --netuid 466 for testnet)
hope-miner --wallet-name my_miner --validator-url <validator-url>

# Score yourself offline against a release
python scripts/score_predictions.py --release CURRENT_RELEASE_KEY --run-baseline
```

Full guide: [miner quickstart](docs/miner_quickstart.md).

---

## Verifying any epoch

The chain is the source of truth. The verifier supports two modes:

| Mode | Checks | What you need |
|---|---|---|
| Chain integrity (default) | `inner_sig` + IMT roots + weights-binding cross-check + per-miner scoreability | Bittensor RPC + at least one tier-2 archive URL |
| Full score recomputation | All of the above + independent score recomputation | + `--truth-file` derived from the 9.A.2 reveal blob |

```bash
python scripts/verify_epoch.py \
    --epoch-id WR-2026-W18-PUB-E1 \
    --validator-hotkey 5GxVLdpRGZN... \
    --tier-2-base https://archive.example.io \
    --block-hash 0x<the block where 9.C.2 landed>

# Add --truth-file path/to/truth.json for full score recomputation
```

For block-pinned reads of past epochs, the chain RPC must be an
**archive node**. Standard `finney` RPCs only retain the last ~256
blocks.

---

## Tests

```bash
pytest tests/                    # Full suite
pytest tests/adversarial/ -v     # Adversarial scenarios (every claimed defence has a test)
ruff check hope/ scripts/ tests/ # Lint
```

CI runs the same on every push to `main`.

---

## License

MIT — see [LICENSE](LICENSE).
