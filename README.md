<div align="center">

# **SN21 — Impact Prediction Subnet**

### Predict ad campaign outcomes. Earn from accuracy alone.

> **Daily stream (from 4 Aug 2026).** SN21 has moved from weekly epochs to a
> **daily** stream: you ship a **container image**, the subnet runs it against a
> fresh basket of real account changes every day, and settled outcomes feed a
> rolling standing that drives emissions. Weekly `hope-miner` prediction
> bundles are obsolete. Why we moved: [docs/SN21_WHY_DAILY.md](docs/SN21_WHY_DAILY.md).
>
> **Reading order:** [Why daily](docs/SN21_WHY_DAILY.md) →
> [Transition plan](docs/SN21_TRANSITION_PLAN.md) →
> [Quickstart](docs/miner_quickstart.md) →
> [Training](docs/SN21_TRAINING.md) → [Scoring](docs/SN21_SCORING.md) →
> [Verifying your score](docs/SN21_VERIFYING.md) → [Rewards](docs/SN21_REWARDS.md) →
> [Staking](docs/SN21_STAKING.md) → [Model spec](docs/MINER_MODEL_SPEC.md)

Every prediction is sealed on chain before the outcome is knowable.
Every score is reproducible by anyone with a chain reader.
No one — including the operator — can rewrite the record after the fact.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/ippcteam/SN21-adtao/actions/workflows/test.yml/badge.svg)](https://github.com/ippcteam/SN21-adtao/actions/workflows/test.yml)

---

Bittensor Subnet 21 · Mainnet `finney` · Testnet `test` (netuid 466)

[Quickstart](docs/miner_quickstart.md) · [Scoring](docs/SN21_SCORING.md) · [Rewards](docs/SN21_REWARDS.md) · [Transition](docs/SN21_TRANSITION_PLAN.md)
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
outcomes, running on Bittensor. Miners ship a **digest-pinned model
container**; the subnet runs it each day against a fresh basket of real
account changes. Models output P10/P50/P90 distributions over **7-, 14-,
and 28-day** campaign deltas. Settled outcomes feed a rolling standing
that drives emissions. Why daily: [docs/SN21_WHY_DAILY.md](docs/SN21_WHY_DAILY.md).

Protocol background (weekly-era sections historical): [Whitepaper](docs/whitepaper.md).

### Two core guarantees

1. **Predictions lock before outcomes exist.** Your on-chain commitment pins the container digest the subnet will run; each day's basket is scored from that run, before outcomes for that basket are knowable.
2. **Scoring is open and reproducible.** Settle logic lives in `hope/scoring/`; standings and weights follow the published curve. Daily accuracy / receipt feeds and the public site mirror the operator's scored record — see [docs/SN21_SCORING.md](docs/SN21_SCORING.md) and [docs/SN21_REWARDS.md](docs/SN21_REWARDS.md). (Weekly-era epochs remain auditable with [`scripts/verify_epoch.py`](scripts/verify_epoch.py).)

---

## How it works

- **Operator** ships a **daily basket** (`BD-*`) of qualifying real account changes (midnight EST day boundary) and later publishes settled outcomes at 7 / 14 / 28 days (+ settle window).
- **Miners** register once, build a container (stdin episodes → stdout predictions), push it digest-pinned, and commit `sn21-model:v1:<repo>@sha256:<digest>` on chain. The subnet pulls, gate-admits, and **runs the container** each live day — miners do not POST daily predictions.
- **Validators / settle clock** score matured (episode × horizon) rows into each miner's **standing**, map standings through the published **weight curve**, and set weights on chain. Emissions follow Bittensor tempo continuously.
- **Champion** (who runs live for customers) is promoted under a stricter multi-day rule than who earns weight — see [docs/SN21_REWARDS.md](docs/SN21_REWARDS.md).
- **Cutover** from the last weekly epoch (scored 3 Aug 2026) is dated in [docs/SN21_TRANSITION_PLAN.md](docs/SN21_TRANSITION_PLAN.md).

---

## Architecture

| Component | Location | Trust model |
|-----------|----------|-------------|
| Daily basket + outcomes | Operator (off chain) → published feeds / digests | Operator-signed outcomes; published settle schedule |
| Miner model | Public registry image + on-chain digest commitment | Digest pin; gate admission before earn |
| Sandbox execution | Operator-run container (`--network=none`, RAM/time budget) | Deterministic contract in [MINER_MODEL_SPEC](docs/MINER_MODEL_SPEC.md) |
| Scoring / standing | `hope/scoring/` (settle day, episode average, weight curve) | Open-source; [SN21_SCORING](docs/SN21_SCORING.md) / [SN21_REWARDS](docs/SN21_REWARDS.md) |
| Weights | Subtensor `set_weights` / commit-reveal path | Yuma consensus |
| Publication | Accuracy + receipt feeds (`hope/publication/`) | Independent check of scored predictions |
| Weekly-era archives / TLE | Historical path only | See whitepaper + `docs/archive/weekly/` |

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

Your container outputs **probabilistic distributions** (P10/P50/P90) per
horizon (**7 / 14 / 28**), not point estimates. You're rewarded for
calibrated uncertainty.

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

Each admitted model is run against the daily basket; every (episode, horizon)
prediction is scored **[0, 1]** once its outcome settles (day 15 / 22 / 36).
Scores enter a **12-day half-life moving average** (your *standing*), weighted
by horizon: 7-day 20%, 14-day 35%, 28-day 45% at high measurement resolution.

The production settle formula blends four components — quantile accuracy
(P10/P50/P90 pinball, 50%), interval coverage (10% — the computable half of the
published 20% calibration weight), direction on the goal metric (15%) and goal
p50 accuracy (15%) — renormalised over 0.90. Full detail, including the
account-goal basis (CPA vs ROAS frozen at reveal) and attrition censoring:
[docs/SN21_SCORING.md](docs/SN21_SCORING.md).

> **You can check our arithmetic.** Every day's scoring is published as a
> signed receipt — the outcomes used, every prediction verbatim, each score's
> components — and anchored on chain. Recompute your own scores with
> `scripts/verify_day.py`; see [SN21_VERIFYING](docs/SN21_VERIFYING.md).

> Predictions also carry `goal_miss_probability` and `instability_risk` fields.
> They are accepted for forward compatibility but **not scored** — no ground
> truth exists for them. Do not spend model capacity there.

## How emissions work

Standing feeds a **rank curve**: 1st place 50% of the pot, 2nd 25%, 3rd 10%,
then a geometric tail (each next rank half the previous), hard cap 20 earners,
zero below the score threshold. Placement requires **250 weighted predictions**
of evidence; full standing at 1,000.

Burn and the alpha-hold ladder follow the published, dated schedule (burn is
indicative and may change to protect alpha value):
[docs/SN21_REWARDS.md](docs/SN21_REWARDS.md) ·
[docs/SN21_STAKING.md](docs/SN21_STAKING.md) ·
[docs/SN21_TRANSITION_PLAN.md](docs/SN21_TRANSITION_PLAN.md).

## Repository structure

```
SN21-adtao/
├── docs/
│   ├── SN21_WHY_DAILY.md         Why we moved to daily (canonical)
│   ├── SN21_TRANSITION_PLAN.md   Cutover / bridge dates
│   ├── miner_quickstart.md       Miner onboarding (daily stream)
│   ├── SN21_TRAINING.md          Train → container → smoke test
│   ├── SN21_SCORING.md           Daily-stream scoring (authoritative)
│   ├── SN21_REWARDS.md           Rank curve + emissions (authoritative)
│   ├── SN21_STAKING.md           Alpha-hold ladder
│   ├── MINER_MODEL_SPEC.md       Container contract
│   ├── whitepaper.md             Protocol design (weekly sections historical)
│   └── archive/weekly/           Obsolete weekly reward / epoch / economics specs
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

The daily-stream miner path is: **register (one-time) → build a container that
reads episodes as NDJSON on stdin and writes predictions as NDJSON on stdout →
push it digest-pinned to a public registry → commit
`sn21-model:v1:<repo>@sha256:<digest>` on chain → the subnet pulls, gates and
runs it against every daily basket.** You do not POST predictions; your
container is executed by the operator.

Follow the step-by-step guide: [docs/miner_quickstart.md](docs/miner_quickstart.md)
(registration, ed25519 binding, container contract, digest commitment,
verifying your submission, training data, troubleshooting).

> **Obsolete:** `hope-miner --epoch WR-...` weekly submission commands. They
> remain in git history only; the last weekly epoch scored on 3 Aug 2026.

## Running a validator

Multiple validators are registered on SN21 — Bittensor's protocol allows open validator registration. The AdTAO operator runs canonical primary and shadow validators against the published scoring specification, and is coordinating with other validator operators to align on the same scoring logic. The Review 4 milestone tracks readiness for a formal third-party validator programme — deployment guides, scoring spec reference implementation, and operator coordination channels.

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
```

**A complete validator runs three independent processes** — all three are required for sustained operation:

| Process | Binary | Cadence | What it does |
|---|---|---|---|
| **HTTP API** | `hope-validator-api` | long-lived daemon | Serves episodes to miners; takes `--port` |
| **Scoring** | `hope-validator` | weekly cron — **historical**; the daily stream scores via `daily_loop` on the settle clock at cutover | Reads miner submissions, scores, commits weights on chain |
| **Heartbeat** | `hope-validator-heartbeat` | cron every 3-4 hours | Re-asserts the last weights commit so Bittensor's `ActivityCutoff` (~16h on mainnet) does not prune your validator from consensus between weekly scoring runs |

Skipping the heartbeat means your validator drops out of emission a day or two after each scoring run — even if your scoring is otherwise flawless.

Quick example (mainnet):

```bash
# Process A — episode API (long-lived)
hope-validator-api --release CURRENT_RELEASE_KEY \
    --host 0.0.0.0 --port 8080 \
    --wallet-name my_validator --wallet-hotkey default

# Process B — weekly scoring (cron after mining deadline)
hope-validator --release CURRENT_RELEASE_KEY \
    --wallet-name my_validator --wallet-hotkey default \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/.sn21/keys/validator.pem

# Process C — heartbeat (cron every 3-4 hours)
hope-validator-heartbeat \
    --wallet-name my_validator --wallet-hotkey default
```

Full guide including cron snippets, hyperparameter notes, and reg-index setup: [validator setup](docs/validator_setup.md). The heartbeat is documented in detail in §10.4.

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

The chain-integrity mode requires only chain access + a Tier-2 URL.
The full-recompute mode additionally needs a `--truth-file` derived
from the operator's 9.A.2 reveal blob; that artifact is operator-published
post-deadline and isn't yet hosted at a stable public URL on testnet 466.
Until that lands, the chain-integrity check is the practical
miner-runnable verifier path.

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
