# Validator Setup Guide

**For:** Running the HOPE SN21 validator
**Prerequisite:** Python 3.10+, Bittensor wallet (for testnet/mainnet)

---

## 1. Installation

```bash
git clone https://github.com/ippcteam/tao-discovery.git
cd tao-discovery
pip install -e .
```

---

## 2. Quick Start (Local Testing)

Run a single epoch against the live HOPE data API:

```bash
hope-validator --release WR-2026-W18-PUB-E1 --port 8080 --score-now
```

This will:
1. Fetch 101 episodes from the HOPE API
2. Compute commitment hash
3. Score immediately (no miner interaction)
4. Print results

Output:
```
Epoch started: WR-2026-W18-PUB-E1
Episodes: 101
Commitment: 2c55f687a5dedf81...
Deadline: 2026-04-25T11:15:23+00:00
```

---

## 3. Running with Miners

### Start the validator

```bash
hope-validator --release WR-2026-W18-PUB-E1 --port 8080
```

This starts the FastAPI server and waits for miners to connect. The validator:
- Fetches episodes from HOPE API
- Commits outcome hash before distributing
- Serves episodes at `https://validator.adtao.io/epochs/{epoch_id}/episodes`
- Accepts predictions at `POST /epochs/{epoch_id}/predictions`

### Tell miners your endpoint

Miners connect with:

```bash
hope-miner --validator-url https://validator.adtao.io --hotkey MINER_HOTKEY --epoch WR-2026-W18-PUB-E1
```

### Score after deadline

Once miners have submitted predictions, trigger scoring:

```python
from hope.validator.runner import ValidatorRunner

runner = ValidatorRunner()
result = runner.score_epoch()
print(result)
```

Or programmatically in a script:

```python
import asyncio
from hope.validator.runner import ValidatorRunner

async def run():
    runner = ValidatorRunner(port=8080)

    # Start epoch
    await runner.run_epoch("WR-2026-W18-PUB-E1")

    # Start API server for miners
    runner.start_api_server()

    # Wait for predictions (in production: wait until deadline)
    import time
    time.sleep(300)  # 5 minutes for testing

    # Score and reveal
    result = runner.score_epoch()
    for miner_id, score in result["scores"].items():
        print(f"{miner_id}: {score['final_score']:.4f}")

asyncio.run(run())
```

---

## 4. API Endpoints

Once running, the validator exposes:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | None | Validator status |
| `GET` | `/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `POST` | `/epochs/{id}/predictions` | Hotkey | Submit predictions |
| `GET` | `/epochs/{id}/commitment` | None | Commitment proof |
| `GET` | `/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |
| `GET` | `/epochs/{id}/scores` | None | Per-miner scores (post-scoring) |

### Authentication

Miners authenticate with the `X-Miner-Hotkey` header. For launch (simplified), any non-empty hotkey is accepted. Future versions will verify ed25519 signatures against the subnet metagraph.

### Interactive docs

FastAPI auto-generates interactive API docs at:
- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`

---

## 5. Epoch Lifecycle

The validator manages epochs through these states:

```
IDLE → PREPARING → COMMITTED → DISTRIBUTING → COLLECTING → SCORING → REVEALING → COMPLETE
```

| State | What Happens |
|-------|-------------|
| **PREPARING** | Fetch episodes + outcomes from HOPE API, verify package hash |
| **COMMITTED** | Compute SHA-256 hash of outcomes + salt + weights |
| **DISTRIBUTING** | Start serving episodes to miners via HTTP |
| **COLLECTING** | Accept predictions until deadline (48 hours default) |
| **SCORING** | Run scoring pipeline on all submitted predictions |
| **REVEALING** | Publish outcomes + salt for verification |
| **COMPLETE** | Log summary, archive epoch |

---

## 6. Commitment Verification

Before distributing episodes, the validator commits:

```
commitment_hash = SHA256(outcomes_json + salt + weights_json)
```

After scoring, it reveals the salt and outcomes. Anyone can verify:

```python
import hashlib, json

# The revealed data
outcomes = [...]  # from /epochs/{id}/verification
salt = "abc..."   # from /epochs/{id}/verification
weights = "{...}" # from /epochs/{id}/verification

# Recompute
payload = json.dumps(outcomes, sort_keys=True) + salt + weights
computed = hashlib.sha256(payload.encode()).hexdigest()

# Compare with commitment
assert computed == commitment_hash
```

This proves the validator did not change outcomes after seeing miner predictions.

---

## 7. HOPE Data API

The validator fetches data from HOPE's internal API:

| Endpoint | Purpose |
|----------|---------|
| `GET /internal/bittensor/v1/releases` | List available releases |
| `GET /internal/bittensor/v1/releases/{key}/package` | Full challenge package |
| `GET /internal/bittensor/v1/governance/summary` | Governance stats |

**Authentication:** `X-API-Key` header or `?api_key=` query parameter.

**Live endpoint:** `https://hope-bittensor-api.onrender.com`

The data client handles this automatically:

```python
from hope.validator.data_client import HopeDataClient

client = HopeDataClient(api_key="your-api-key")
data = await client.fetch_epoch_data("WR-2026-W18-PUB-E1")
print(f"Episodes: {data.episode_count}")
print(f"Package hash: {data.package_hash}")
```

---

## 8. Scoring Pipeline

The validator uses the scoring library to evaluate predictions:

```python
from hope.scoring import EpochScorer

scorer = EpochScorer()
scores = scorer.score_epoch(
    all_predictions={"miner_1": [...], "miner_2": [...]},
    episodes=episodes,
    outcomes=outcomes,
)

for miner_id, score in scores.items():
    print(f"{miner_id}: raw={score.raw_score:.4f} "
          f"skill={score.skill_score:.4f} "
          f"penalty={score.null_penalty:.4f} "
          f"final={score.final_score:.4f}")
```

### Scoring weights (launch defaults)

| Component | Weight | Range |
|-----------|--------|-------|
| Quantile Accuracy | 0.50 | 0.45-0.55 |
| Calibration | 0.20 | 0.15-0.25 |
| Directional | 0.15 | 0.10-0.20 |
| Goal Accuracy | 0.15 | 0.10-0.20 |

Weights must sum to 1.0 and stay within published ranges.

---

## 9. Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOPE_API_KEY` | `hope-bt-internal-2026` | HOPE data API key |
| `HOPE_API_URL` | `https://hope-bittensor-api.onrender.com` | HOPE API base URL |
| `VALIDATOR_PORT` | `8080` | HTTP API port |
| `PREDICTION_DEADLINE_HOURS` | `48` | Hours miners have to submit |

### CLI arguments

```
hope-validator
  --release KEY          Release key to process (e.g., WR-2026-W18-PUB-E1)
  --api-key KEY          HOPE API key
  --port PORT            API server port (default: 8080)
  --score-now            Score immediately without waiting for miners
```

---

## 10. Production Deployment

### Requirements

- Python 3.10-3.12
- 2GB RAM minimum (episodes are ~15KB each, 300 episodes = ~4.5MB)
- Stable internet for HOPE API access
- Open port for miner HTTP connections

### Recommended setup

```bash
# Use a process manager
pip install -e .
nohup hope-validator --release WR-2026-W18-PUB-E1 --port 8080 > validator.log 2>&1 &

# Monitor
tail -f validator.log
```

### Weekly epoch cycle

Each Monday:
1. A new release is available from HOPE (e.g., `WR-2026-W19-PUB-E1`)
2. Restart the validator with the new release key
3. Miners have 48 hours to submit predictions
4. Trigger scoring after deadline
5. Outcomes revealed, weights set

---

## 11. Troubleshooting

| Issue | Solution |
|-------|----------|
| `Package hash verification failed` | Network issue — retry fetch |
| `Epoch not found` (404 from miners) | Miner using wrong epoch_id |
| `Prediction deadline has passed` | Miner submitted too late |
| `No valid tokens` from HOPE API | Check API key |
| Low miner scores | Expected for baseline model — miners should build better models |
