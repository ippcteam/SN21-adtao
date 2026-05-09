<div align="center">

# **SN21 — Impact Prediction Subnet**

### Predict ad campaign outcomes. Earn from accuracy alone.

Every prediction is sealed on chain before the outcome is knowable.
Every score is reproducible by anyone with a chain reader.
No one — including the operator — can rewrite the record after the fact.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/ippcteam/SN21-adtao/actions/workflows/test.yml/badge.svg)](https://github.com/ippcteam/SN21-adtao/actions/workflows/test.yml)

---

Bittensor Subnet 21 · Mainnet `finney` · Testnet `test` (netuid 466)

[Whitepaper](docs/whitepaper.md) · [Reward mechanism](docs/SN21_REWARD_MECHANISM.md) · [Miner quickstart](docs/miner_quickstart.md)
</div>

---

- [Overview](#overview)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [What you predict](#what-you-predict)
- [How scoring works](#how-scoring-works)
- [How emissions work](#how-emissions-work)
- [Repository structure](#repository-structure)
- [Hardware requirements](#hardware-requirements)
- [Running a miner](#running-a-miner)
- [Running a validator](#running-a-validator)
- [Verifying any epoch](#verifying-any-epoch)
- [Development](#development)
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

1. **Predictions are bound to the miner that produced them.** Every prediction is signed (`inner_sig`) and committed on chain via timelock encryption before the outcome is knowable. Late or rewritten predictions are detectable from chain state alone.
2. **Validator scoring is independently reproducible.** Every input that affects a miner's score is anchored on chain through a Merkle root. [`scripts/verify_epoch.py`](scripts/verify_epoch.py) reads the chain, fetches the off-chain artifacts, re-runs the open-source scoring code, and either confirms or contradicts the validator's claim.

---

## How it works

- **Outcome signer** publishes a weekly stream of prediction problems (episodes) drawn from real Google Ads management data and commits the release-package digest on chain at T=0.
- **Miners** receive structured episodes (account state, action context, 60-day pre-window) and submit P10/P50/P90 distributions per (campaign × horizon). Submissions are AES-GCM encrypted, the AES key is timelock-encrypted to a future drand round, and the ciphertext SHA + key + archive URL are committed on chain.
- **Validators** read miner commits after the timelock reveals, fetch the encrypted predictions from a three-tier archive, run an 8-check scoreability rule, score against measured outcomes, and submit weights via `commit_timelocked_weights`. Pre- and post-scoring artifacts are committed as IMT roots on chain.
- **A shadow validator** runs the same code on a separate hotkey and commits its own scoring artifacts. Mismatches between primary and shadow are publicly auditable.
- **Anyone** can re-run the verifier against any past epoch and confirm — or contradict — the validator's scoring.

---

## Architecture

| Component | Location | Trust model |
|-----------|----------|-------------|
| Episode + outcome publication | Operator (off chain) → digest on chain | Hash-anchored on chain via 9.A.1 + 9.A.2 |
| Miner predictions | AES_ct off chain → SHA + TLE'd K + URL on chain | ed25519 inner_sig bound to miner hotkey |
| Three-tier archive | Tier-1 (validator), Tier-2 (operator), Tier-3 (miner self) | Content-addressed by SHA-256; chain commit is the integrity anchor |
| Scoring orchestration | Each validator | Open-source `hope/scoring/` and `hope/validator/onchain_runner.py`; reproducible by `scripts/verify_epoch.py` |
| Pre/post scoring artifacts | On chain via TimelockEncrypted commits | IMT roots; ed25519 inner_sig; chain auto-decrypts |
| Weights commit | Standard Subtensor `commit_timelocked_weights` | Bittensor v4 commit-reveal |
| Drand quicknet beacon | External (League of Entropy) | BLS-on-BLS12-381 distributed beacon |
| Yuma consensus | Subtensor runtime | Standard Bittensor weight aggregation |

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
├── hope/                         Core Python package (`pip install -e .`)
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
├── tests/                        412 tests
│   ├── adversarial/              12 attack scenarios with passing defences
│   ├── e2e/                      Full miner flow against a running validator
│   ├── commitment/               Crypto primitives (243 tests)
│   ├── scoring/                  Scoring components + adapter (49 tests)
│   ├── miner/                    Miner runtime
│   ├── validator/                Validator runtime
│   └── scripts/                  Public verifier
│
├── data/training/                10 sample episodes with known outcomes
├── min_compute.yml               Hardware requirements (miner + validator)
├── CONTRIBUTING.md               How to file PRs and propose protocol changes
└── pyproject.toml                Python 3.10+ package config + entry points
```

---

## Hardware requirements

Full specs in [`min_compute.yml`](min_compute.yml).

### Miner

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 cores @ 2.0 GHz | 4 cores @ 3.0 GHz |
| RAM | 4 GB | 8 GB+ |
| Disk | 5 GB SSD | 20 GB SSD |
| GPU | Not required | Optional (larger models) |
| Network | 100 Mbps down / 20 Mbps up | — |

The reference baseline runs on CPU. GPU only matters if you train a heavier model.

### Validator

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 4 cores @ 2.5 GHz | 8 cores @ 3.5 GHz |
| RAM | 8 GB | 16 GB |
| Disk | 10 GB SSD | 50 GB SSD |
| GPU | Not required | — |
| Network | 100 Mbps down / 20 Mbps up | — |

Validators run scoring orchestration + an HTTP API for miners + a Tier-1 archive cache. CPU bottleneck is `EpochScorer` over all miners' decrypted predictions.

---

## Running a miner

The example below targets **testnet 466** — current open environment.
For mainnet, swap `test` → `finney` and `466` → `21`.

```bash
# Clone + install
git clone https://github.com/ippcteam/SN21-adtao.git
cd SN21-adtao
pip install -e ".[miner]"

# Create + register a Bittensor wallet (one-time)
btcli wallet new_coldkey --wallet.name my_miner
btcli wallet new_hotkey --wallet.name my_miner --wallet.hotkey default
btcli subnet register --netuid 466 \
    --wallet.name my_miner --wallet.hotkey default \
    --subtensor.network test
# Need testnet TAO? Bittensor Discord faucet: https://discord.gg/bittensor

# Generate an ed25519 key for inner_sig (one-time, separate from the wallet hotkey)
python scripts/sn21_keys.py generate --role miner --output ~/sn21-miner.pem

# Register the hotkey ↔ ed25519 binding on chain (one-time)
python scripts/sn21_keys.py register --role miner \
    --network test --netuid 466 \
    --wallet-name my_miner --wallet-hotkey default \
    --key ~/sn21-miner.pem

# Train on bundled sample data (optional — 10 episodes, known outcomes)
python scripts/train_example_model.py --data-file data/training/training_episodes.json

# Run miner against the live validator
hope-miner --validator-url https://validator.adtao.io \
    --wallet-name my_miner --wallet-hotkey default \
    --epoch WR-2026-W18-PUB-E1 \
    --bt-network test --netuid 466 \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --archive-tier-3 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/sn21-miner.pem

# Score yourself offline against the bundled sample dataset (no API key needed)
python scripts/score_predictions.py \
    --training-data data/training/training_episodes.json --run-baseline
```

Full guide with troubleshooting: [miner quickstart](docs/miner_quickstart.md).

---

## Running a validator

Third-party validator registration is **not open at launch** — the operator runs the canonical primary + shadow validators. The codebase is the same one a third-party validator would run; the Review 4 milestone tracks readiness.

If you want to run the validator code locally for testing or to mirror what the operator runs:

```bash
# Install
pip install -e .

# Generate ed25519 key + register on chain (same as miner)
python scripts/sn21_keys.py generate --role validator --output ~/.sn21/keys/validator.pem
python scripts/sn21_keys.py register --role validator \
    --network finney --netuid 21 \
    --wallet-name my_validator --wallet-hotkey default \
    --key ~/.sn21/keys/validator.pem

# Run validator (HTTP API on :8080, --no-chain for offline development)
hope-validator --release CURRENT_RELEASE_KEY --port 8080
```

Full guide: [validator setup](docs/validator_setup.md).

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

## Development

```bash
# Full unit + adversarial + e2e suite (412 tests)
pytest tests/

# Adversarial scenarios only — every claimed defence has a passing attack test
pytest tests/adversarial/ -v

# Lint
ruff check hope/ scripts/ tests/
```

CI runs the same on every push to `main` (`.github/workflows/test.yml`).

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit-message style, and the protocol-change PR process.

---

## License

MIT — see [LICENSE](LICENSE).
