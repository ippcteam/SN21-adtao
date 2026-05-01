# TAO Discovery — HOPE Impact Prediction Subnet (SN21)

Predict the impact of Google Ads interventions on campaign performance. Miners receive structured episodes describing account state and actions taken, then predict what happens to cost, conversions, and efficiency over 7 and 14 days.

**Subnet:** SN21 on Bittensor
**Schema:** v1.9 (Phase 1 — Search campaigns, campaign-level actions)
**Status:** Pre-launch (targeting May 2026)

---

## How It Works

```
HOPE Platform                    Validator                      Miners
─────────────                    ─────────                      ──────
Google Ads data ──┐
                  │
Campaign metrics ─┤
                  ├──→ Weekly challenge    ──→ Commit outcome   ──→ Announce epoch
Changelog events ─┤      package                hash on-chain       to miners
                  │
Archetype data ───┘                             ──→ Serve episodes via HTTP
                                                         │
                                                    Miners fetch episodes
                                                    Run prediction models
                                                    Submit predictions
                                                         │
                                                ←── Score predictions
                                                    Reveal outcomes
                                                    Set weights on-chain
```

**Episodes** are structured prediction challenges: 60 days of campaign performance history, the actions taken, account context, and environmental signals. Miners predict what happens next.

**Scoring** uses four components: quantile accuracy (50%), calibration (20%), directional correctness (15%), and goal accuracy (15%). Miners output P10/P50/P90 distributions — rewarded for calibrated uncertainty, not just point accuracy.

**Verification** is cryptographic: the validator commits to outcomes before distributing episodes. After scoring, outcomes are revealed and anyone can verify the commitment matches.

### Specifications (economics & roadmap)

- **[Miner economics (short)](docs/MINER_ECONOMICS.md)** — gates, tiers, multipliers, how this repo fits  
- **[SN21 Reward Mechanism](docs/SN21_REWARD_MECHANISM.md)** — emissions, tiers, reviews, emergency rules  
- **[SN21 Epoch Structure](docs/SN21_EPOCH_STRUCTURE.md)** — phases, campaign types, consolidation, announcements  

---

## Quick Start

### For Miners

```bash
# Install
git clone https://github.com/ippcteam/tao-discovery.git
cd tao-discovery
pip install -e ".[miner]"

# Train on historical data first (recommended)
python scripts/train_example_model.py --data-file data/training/training_episodes.json

# Run miner (auto-discovers current epoch from validator)
hope-miner --validator-url https://validator.adtao.io --hotkey YOUR_HOTKEY

# Or run continuously (polls for new epochs)
hope-miner --validator-url https://validator.adtao.io --hotkey YOUR_HOTKEY --continuous

# Check your score after an epoch
python scripts/score_predictions.py --release CURRENT_RELEASE_KEY --run-baseline
```

**Validator URL:** `https://validator.adtao.io`

**Training data:** 10 episodes with known outcomes are bundled in `data/training/`. Use these to build a model before predicting on live epochs.

Read the full guide: [Miner Quickstart](docs/miner_quickstart.md) · [Miner economics](docs/MINER_ECONOMICS.md)

### For Validators

```bash
# Install
pip install -e .

# Run validator
hope-validator --release CURRENT_RELEASE_KEY --port 8080
```

Read the setup guide: [Validator Setup](docs/validator_setup.md)

**Operations (cadence, workload, TBDs for launch):** [Miner operations](docs/MINER_OPERATIONS.md) · [Validator operations](docs/VALIDATOR_OPERATIONS.md)

---

## Repository Structure

```
tao-discovery/
├── hope/
│   ├── protocol/           # Pydantic models — Episode, Prediction, Outcome
│   │   ├── episode.py      # v1.9 episode schema (miner input)
│   │   ├── prediction.py   # P10/P50/P90 quantile predictions (miner output)
│   │   ├── outcomes.py     # Ground truth (validator-only)
│   │   └── synapse.py      # Bittensor Synapse definitions
│   │
│   ├── scoring/            # Scoring library (pure Python, no Bittensor dep)
│   │   ├── components/     # 4 scoring components
│   │   │   ├── quantile_accuracy.py   # Pinball loss + CRPS (50%)
│   │   │   ├── calibration.py         # Interval score (20%)
│   │   │   ├── directional.py         # Sign match (15%)
│   │   │   └── goal_accuracy.py       # Brier score (15%)
│   │   ├── episode_scorer.py   # Score one episode across horizons
│   │   ├── scorer.py           # EpochScorer — score all miners
│   │   ├── null_penalty.py     # Near-zero prediction penalty
│   │   ├── skill_score.py      # Compare vs predict-zero baseline
│   │   └── weights.py          # Configurable scoring weights
│   │
│   ├── validator/          # Validator implementation
│   │   ├── api/            # FastAPI server for miners
│   │   │   ├── server.py       # App factory
│   │   │   ├── episodes.py     # Episode fetch endpoints
│   │   │   ├── predictions.py  # Prediction submit endpoint
│   │   │   ├── commitments.py  # Commitment proof endpoint
│   │   │   └── verification.py # Post-scoring reveal
│   │   ├── data_client.py      # Fetches data from HOPE API
│   │   ├── epoch_manager.py    # Epoch lifecycle state machine
│   │   └── runner.py           # Validator entry point
│   │
│   └── miner/              # Miner SDK
│       ├── prediction_engine.py    # Abstract base class
│       ├── episode_client.py       # HTTP client to fetch episodes
│       ├── prediction_client.py    # HTTP client to submit predictions
│       ├── runner.py               # Miner entry point
│       └── models/
│           └── baseline.py         # Reference baseline model
│
├── docs/
│   ├── MINER_ECONOMICS.md       # Gates, tiers, multipliers (summary)
│   ├── SN21_REWARD_MECHANISM.md # Emissions & governance spec
│   ├── SN21_EPOCH_STRUCTURE.md  # Phases & epoch roadmap
│   ├── miner_quickstart.md      # Tutorial & scoring detail
│   ├── validator_setup.md      # Validator deployment
│   └── PHASE1_BUILD_PLAN.md    # Build plan and architecture
│
├── scripts/
│   └── score_predictions.py    # Offline scoring tool
│
└── tests/
    ├── unit/                   # 22 unit tests
    └── integration/            # 7 integration tests (live API)
```

---

## Episode Format (v1.9)

Each episode contains:

| Section | Description | Size |
|---------|-------------|------|
| `episode_metadata` | ID, schema version, resolution, horizons | ~0.5 KB |
| `account_state` | Customer hash, goal, spend bucket, optional TRUST enrichment | ~1 KB |
| `date_index` | 60 date strings for the pre-window | ~0.5 KB |
| `pre_window` | 60-day campaign time series + account aggregates | ~8 KB |
| `action_bundle` | Actions with type, magnitude, blast radius, risk | ~2 KB |
| `campaign_metadata` | Campaign type, bid strategy, status | ~0.3 KB |

Total: ~12-15 KB per episode.

### Action Types (Phase 1)

| Type | Description | Predictability |
|------|-------------|---------------|
| `BUDGET_CHANGE` | Daily budget increased/decreased | High — magnitude gives expected % |
| `BID_STRATEGY_CHANGE` | Bidding strategy switched | Medium — 7-14 day learning period |
| `CAMPAIGN_PAUSE` | Campaign paused | Deterministic — cost/conv = -100% |
| `CAMPAIGN_ENABLE` | Campaign re-enabled | Medium — recovery depends on pause duration |

---

## Scoring

| Component | Weight | What It Measures |
|-----------|--------|-----------------|
| Quantile Accuracy | 50% | Pinball loss on P10/P50/P90 vs actual |
| Calibration | 20% | Interval coverage with convex width penalty |
| Directional | 15% | Did you predict the right direction? |
| Goal Accuracy | 15% | Brier score on goal miss probability |

Plus **null penalty** (up to 60% reduction for near-zero predictions) and **skill score** (must beat the predict-zero baseline).

The scoring library has zero Bittensor dependency:

```python
from hope.scoring import EpochScorer
scorer = EpochScorer()
scores = scorer.score_epoch(predictions, episodes, outcomes)
```

---

## Data Source

Episodes are generated from real Google Ads management data by the AdTAO platform. The data pipeline:

- **4,312 accounts** normalised into governance registry
- **572 episode candidates** classified from changelog events
- **65,000+** campaign-day rows with daily metrics
- **Permanent bucket assignment** — 60% public training, 25% hidden external, 15% hidden internal
- **Contamination guard** — hidden data never leaks into public releases

Weekly releases delivered via authenticated API with SHA-256 integrity hash.

---

## Phase 1 Scope

Phase 1 (launch) is deliberately narrow:

- **Search campaigns only** (PMax, Shopping, Display deferred to Phase 2)
- **Campaign-level actions only** (ad group, keyword deferred to Phase 1 Epoch 3)
- **7-day and 14-day horizons** (28-day deferred)
- **60-day pre-window** (not 90)
- **TRUST enrichment optional** — episodes tagged `baseline` or `trust_enriched`

This scope matches the Bittensor epoch structure's Phase 1 Epoch 1 definition and gives miners the cleanest possible signal on day one.

---

## License

MIT
