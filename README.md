# SN21 — Impact Prediction Subnet

**Verifiable prediction markets for Google Ads campaign outcomes,
running on Bittensor.**

> Anyone can submit a prediction. Anyone can verify it was scored
> honestly. The chain is the source of truth.

| | |
|---|---|
| **Subnet** | SN21 (Bittensor mainnet `finney`, netuid **21**) |
| **Testnet** | netuid **466** on `test` |
| **Status** | Pre-mainnet — testnet validation complete |
| **License** | MIT |
| **Repo** | `tao-discovery` |
| **Python package** | `hope-sn21` (installed via `pip install -e .`) |
| **CLIs** | `hope-miner`, `hope-validator`, `hope-score` |

---

## Read this first

* **What it is:** [whitepaper](docs/whitepaper.md) — design, trust
  model, two cryptographic guarantees, adversarial defence matrix.
* **How it was built:** [build journey](docs/build_journey.md) —
  phase-by-phase narrative with the receipts.
* **Are you a miner?** [miner quickstart](docs/miner_quickstart.md) —
  install, register, train, predict, verify.
* **What you earn:** [miner economics](docs/MINER_ECONOMICS.md) (short)
  · [reward mechanism](docs/SN21_REWARD_MECHANISM.md) (full spec) ·
  [epoch structure](docs/SN21_EPOCH_STRUCTURE.md) (phases, horizons,
  consolidation).

If you only read one doc, read the
[whitepaper](docs/whitepaper.md). Its launch-status table is the
canonical record of what ships at launch versus what is roadmap.

---

## What you predict

You receive an **episode** — a structured snapshot of a Google Ads
account at a moment in time:

| Section | What's in it | Size |
|---|---|---|
| `episode_metadata` | ID, schema version, resolution, horizons | ~0.5 KB |
| `account_state` | Customer hash, goal, spend bucket, optional enrichment | ~1 KB |
| `date_index` | 60 date strings for the pre-window | ~0.5 KB |
| `pre_window` | 60-day campaign time series + account aggregates | ~8 KB |
| `action_bundle` | The action(s) being applied: type, magnitude, blast radius, risk | ~2 KB |
| `campaign_metadata` | Campaign type, bid strategy, status | ~0.3 KB |

Total: ~12–15 KB per episode.

You output **probabilistic distributions** (P10/P50/P90), not point
estimates. You're rewarded for calibrated uncertainty.

### Phase 1 action types

Defined in `hope/constants.py:LAUNCH_ACTION_TYPES`:

| Type | What it means | Signal strength |
|---|---|---|
| `BUDGET_CHANGE` | Daily budget increased/decreased | High — magnitude gives expected % |
| `BID_STRATEGY_CHANGE` | Bidding strategy switched | Medium — 7–14 day learning period |
| `TARGET_VALUE_CHANGE` | tCPA/tROAS target adjusted | Medium — known target delta |
| `CAMPAIGN_PAUSE` | Campaign paused | Deterministic — cost/conv ≈ −100% |

Campaign re-enable / resume actions are deferred to a future phase.

---

## How scoring works

Four components combine into one micro-units score per miner per epoch:

| Component | Weight | What it measures |
|---|---|---|
| Quantile Accuracy | 50% | Pinball loss / CRPS on P10/P50/P90 vs actual |
| Calibration | 20% | Interval coverage with convex width penalty |
| Directional | 15% | Sign match on the primary goal metric |
| Goal Accuracy | 15% | Brier score on goal-miss probability |

On top:
* **Null penalty** — up to 60% reduction for near-zero predictions.
* **Skill score** — must beat the conditional-prior baseline (or the
  predict-zero fall-through). Below baseline → zero emission for the
  epoch.

The scoring library is pure-Python, no Bittensor dependency:

```python
from hope.scoring import EpochScorer
scorer = EpochScorer()
scores = scorer.score_epoch(predictions, episodes, outcomes)
```

---

## How emissions work

**At launch.** The default `hope-validator` CLI runs simple
score-normalization with a 95% burn to UID 0:

1. Compute each miner's raw score (the four-component scoring path
   in `hope/scoring/`).
2. Normalize to weights summing to 1.0.
3. Apply 95% burn (UID 0 receives 0.95, miners share 0.05).
4. Submit weights via `commit_timelocked_weights`.

This deliberately uses a simpler emission mechanism for the first
operational cycle, while the chain-side scoring + verification
pipeline is exercised end-to-end on mainnet.

**Available, opt-in.** The full tiered allocator is implemented in
[`hope/validator/tiered_weights.py`](hope/validator/tiered_weights.py)
and unit-tested in
[`tests/unit/validator/test_tiered_weights.py`](tests/unit/validator/test_tiered_weights.py).
It enforces:

1. Participation gate — beat the baseline, ≥ 80% epoch coverage,
   per-bucket coverage thresholds.
2. Four-epoch EMA tier placement (alpha = 0.5).
3. Tier bands — Elite (top 20%) 60% / Competitive (next 40%) 30% /
   Participating (bottom 40%) 10%.
4. Elite quality floor — top 20% must clear baseline + 1·sigma to
   form Elite; otherwise 60% pool redistributes 30:10 to
   Competitive:Participating.

Operators wire it explicitly:

```python
WeightSetter(burn_fraction=0.95, tiered_allocator=TieredAllocator())
```

Tier mechanics are scheduled to become the runner default after
Review 1, when there is enough operational data to tune gate
thresholds against real miner behaviour.

Full spec in [SN21_REWARD_MECHANISM.md](docs/SN21_REWARD_MECHANISM.md).

---

## Quick start (miners)

```bash
# Clone + install
git clone <repo-url>
cd tao-discovery
pip install -e ".[miner]"

# Train on the bundled sample training set (10 episodes, known outcomes).
# NOTE: this is a pre-launch sample — it includes BID_STRATEGY_CHANGE +
# CAMPAIGN_ENABLE actions. The launch action enum is BUDGET_CHANGE,
# BID_STRATEGY_CHANGE, TARGET_VALUE_CHANGE, CAMPAIGN_PAUSE — see
# `hope/constants.py:LAUNCH_ACTION_TYPES`. CAMPAIGN_ENABLE will not
# appear in live epochs; TARGET_VALUE_CHANGE will. The fresh release
# pulled via `scripts/generate_training_data.py` matches the launch enum.
python scripts/train_example_model.py --data-file data/training/training_episodes.json

# Run a miner against the live validator (auto-discovers the current epoch).
# IMPORTANT: --bt-network defaults to "finney" (mainnet) and --netuid to 21.
# For testnet, pass --bt-network test --netuid 466 explicitly.
hope-miner --wallet-name my_miner --validator-url <validator-url>

# Or run continuously
hope-miner --wallet-name my_miner --validator-url <validator-url> --continuous

# Score yourself offline (same scoring the validator runs)
python scripts/score_predictions.py --release CURRENT_RELEASE_KEY --run-baseline

# Verify any past epoch independently — anyone can run this
python scripts/verify_epoch.py \
    --epoch-id <release-key> \
    --validator-hotkey <ss58> \
    --tier-2-base <archive-url> \
    --truth-file path/to/truth.json
```

Full guide: [miner quickstart](docs/miner_quickstart.md).

---

## What's in this repo

```
tao-discovery/
├── docs/
│   ├── whitepaper.md                Design + trust model + adversarial matrix
│   ├── build_journey.md             Phase-by-phase build narrative (A–H + design wave)
│   ├── miner_quickstart.md          Miner onboarding tutorial
│   ├── validator_setup.md           How to run a validator
│   ├── operator_runbook.md          Operator playbook (running primary + shadow)
│   ├── MINER_ECONOMICS.md           Gates, tiers, multipliers (short)
│   ├── SN21_REWARD_MECHANISM.md     Full reward spec
│   ├── SN21_EPOCH_STRUCTURE.md      Phases, horizons, consolidation
│   └── proposals/q26_*.md           Upstream Bittensor RFC for chain-side anchor
│
├── hope/
│   ├── protocol/                    Episode / Prediction / Outcome models
│   ├── commitment/                  Cryptographic primitives (CBOR, IMT, ed25519, drand TLE, archive client, scoreability)
│   ├── scoring/                     Pure-Python scoring library (4 components + skill score + null penalty + per-episode scorer)
│   ├── miner/                       Miner SDK + runner + reference baseline model
│   ├── validator/                   Validator runner, scoring orchestration, tiered weight allocator, FastAPI server
│   ├── archive_server/              FastAPI archive (Tier-2/Tier-3 storage)
│   ├── hope_outcomes/               Outcome signer (release_commit + reveal_blob)
│   └── hope_shadow_validator/       Shadow validator (independent scoring)
│
├── scripts/
│   ├── verify_epoch.py              Public verifier — anyone can audit any epoch
│   ├── score_predictions.py         Offline scoring tool (miners)
│   ├── train_example_model.py       Reference XGBoost training (miners)
│   ├── generate_training_data.py    Pull a release into training format
│   └── sn21_keys.py                 ed25519 key-management CLI (miners + validators)
│
├── data/training/                   10 episodes with known outcomes
├── deploy/archive_server/           Docker / systemd archive deployment
├── deploy/grafana/                  Sample observability dashboard
└── tests/                           488 tests: unit, adversarial, e2e, fixtures
```

---

## Verifying any epoch (anyone)

The chain is the source of truth. The verifier supports two modes,
gated by what inputs you provide:

| Mode | What it checks | What you need |
|---|---|---|
| **Chain integrity** (default) | `inner_sig` on 9.C.1 + 9.C.2; IMT root reconstruction; weights-binding cross-check at `weights_commit_block_hash`; per-miner scoreability re-derivation | A Bittensor RPC endpoint (archive node for past epochs) + at least one tier-2 / tier-3 archive URL |
| **Full score recomputation** | All of the above PLUS independent recomputation of every miner's score against ground truth | Same as above PLUS `--truth-file` derived from the 9.A.2 reveal blob — see `tests/fixtures/recorded_epoch/recorded_epoch.json` for the schema |

```bash
# Chain-integrity check
python scripts/verify_epoch.py \
    --epoch-id WR-2026-W18-PUB-E1 \
    --validator-hotkey 5GxVLdpRGZN... \
    --tier-2-base https://archive.example.io \
    --block-hash 0x<the block where 9.C.2 landed>

# Full score recomputation (add --truth-file)
python scripts/verify_epoch.py \
    --epoch-id WR-2026-W18-PUB-E1 \
    --validator-hotkey 5GxVLdpRGZN... \
    --tier-2-base https://archive.example.io \
    --block-hash 0x<the block where 9.C.2 landed> \
    --truth-file truth_2026_W18.json
```

Without `--truth-file`, score recomputation returns zero for every
miner and `final_score_match` will fail by design — the warning is
printed at startup. Use chain-integrity mode when you don't have the
reveal blob handy; use full recomputation when you do.

For block-pinned reads of past epochs, the chain RPC must be an
**archive node**. Standard `finney` RPCs only retain the last ~256
blocks; older blocks return empty state. See `docs/operator_runbook.md`
§8.1 for archive-node operator setup.

Match → validator is honest. Mismatch → exactly one of {validator,
verifier} has a bug or is malicious; the divergence is publicly
auditable.

---

## Tests

```bash
# Full suite (488 tests)
pytest tests/

# Just adversarial scenarios
pytest tests/adversarial/ -v

# Lint
ruff check hope/ scripts/ tests/
```

Pinned by `pyproject.toml`. CI runs the same on push to `main`.

---

## License

MIT — see [LICENSE](LICENSE).
