# Validator Setup Guide

**For:** Running the SN21 validator
**Prerequisite:** Python 3.10+, Bittensor wallet (for testnet/mainnet)

> **Note on registration.** Validator registration on SN21 itself is open
> by Bittensor protocol — any operator meeting the chain's permit and
> stake requirements can register and submit weights. To run the
> canonical scoring against published episodes and outcomes, a validator
> additionally needs operator-issued data API credentials
> (`HOPE_API_KEY`, `HOPE_API_URL`). Operators wishing to obtain
> credentials at launch should contact the operator team; a formal
> third-party validator programme is tracked at Review 4.

---

## 1. Installation

```bash
git clone <repo-url>
cd tao-discovery
pip install -e .
```

---

## 2. Quick Start (Local Testing)

Run a single epoch against the live data API:

```bash
hope-validator --release WR-2026-W18-PUB-E1 --port 8080 --score-now
```

This will:
1. Fetch 101 episodes from the data API
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
- Fetches episodes from the data API
- Commits outcome hash before distributing
- Serves episodes at `<validator-url>/v1/epochs/{epoch_id}/episodes`
- Accepts predictions at `POST /v1/epochs/{epoch_id}/predictions`

### Tell miners your endpoint

Miners connect with:

```bash
hope-miner --validator-url <validator-url> --epoch WR-2026-W18-PUB-E1
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
| `GET` | `/v1/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/v1/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/v1/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `POST` | `/v1/epochs/{id}/predictions` | Hotkey | Submit predictions |
| `GET` | `/v1/epochs/{id}/commitment` | None | Commitment proof |
| `GET` | `/v1/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |
| `GET` | `/v1/epochs/{id}/scores` | None | Per-miner scores (post-scoring) |

### Authentication

Miners authenticate with ed25519 signatures. Each request must include:
- `X-Miner-Hotkey` — the miner's ss58 address (must be registered on the subnet)
- `X-Miner-Nonce` — numeric timestamp (valid for 5 minutes, single-use)
- `X-Miner-Signature` — ed25519 signature of `SHA256(hotkey:nonce:METHOD:path:body_hash)`

Signatures are verified against the metagraph. Unregistered hotkeys are rejected (403). Invalid or missing signatures are rejected (401).

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
| **PREPARING** | Fetch episodes + outcomes from the data API, verify package hash |
| **COMMITTED** | Compute SHA-256 hash of outcomes + salt + weights |
| **DISTRIBUTING** | Start serving episodes to miners via HTTP |
| **COLLECTING** | Accept predictions until deadline (156 hours / ~6.5 days, per `PREDICTION_DEADLINE_HOURS` in `hope/constants.py`) |
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
outcomes = [...]  # from /v1/epochs/{id}/verification
salt = "abc..."   # from /v1/epochs/{id}/verification
weights = "{...}" # from /v1/epochs/{id}/verification

# Recompute
payload = json.dumps(outcomes, sort_keys=True) + salt + weights
computed = hashlib.sha256(payload.encode()).hexdigest()

# Compare with commitment
assert computed == commitment_hash
```

This proves the validator did not change outcomes after seeing miner predictions.

---

## 7. Data API

The validator fetches releases from the operator's data API:

| Endpoint | Purpose |
|----------|---------|
| `GET {base}/internal/bittensor/v1/releases` | List available releases |
| `GET {base}/internal/bittensor/v1/releases/{key}/package` | Full challenge package (episodes + outcomes + signatures) |
| `GET {base}/internal/bittensor/v1/governance/summary` | Governance stats |

`{base}` is the value of the `HOPE_API_URL` environment variable
provided on validator registration. The path prefix
(`/internal/bittensor/v1/`) is fixed by the data client at
`hope/validator/data_client.py`.

**Authentication:** `X-API-Key` header or `?api_key=` query parameter.

**Live endpoint:** Set via `HOPE_API_URL` environment variable (provided on validator registration).

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
| `HOPE_API_KEY` | *(required)* | data API key — provided on validator registration |
| `HOPE_API_URL` | *(required)* | data API base URL — provided on validator registration |
| `REQUIRE_SIGNATURES` | `true` | Require signed miner requests (set to `false` only for development) |

The miner submission deadline is **156 hours** (~6.5 days), pinned in
`hope/constants.py:PREDICTION_DEADLINE_HOURS` to match the weekly
mining window in `docs/SN21_EPOCH_STRUCTURE.md`. It is not configured
via env var.

The HTTP port is set via `--port` (default `8080`) on the CLI, not
via an env var.

### CLI arguments

```
hope-validator
  --release KEY          Release key to process (e.g., WR-2026-W18-PUB-E1)
  --api-key KEY          data API key
  --port PORT            API server port (default: 8080)
  --score-now            Score immediately without waiting for miners
```

---

## 10. Production Deployment

### Requirements

- Python 3.10-3.12
- 2GB RAM minimum (episodes are ~15KB each, 300 episodes = ~4.5MB)
- Stable internet for data API access
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
1. A new release is available from the operator (e.g., `WR-2026-W19-PUB-E1`)
2. Restart the validator with the new release key
3. Miners have until the weekly deadline (~6.5 days) to submit predictions
4. Trigger scoring after deadline
5. Outcomes revealed, weights set

---

## 11. Troubleshooting

| Issue | Solution |
|-------|----------|
| `Package hash verification failed` | Network issue — retry fetch |
| `Epoch not found` (404 from miners) | Miner using wrong epoch_id |
| `Prediction deadline has passed` | Miner submitted too late |
| `No valid tokens` from data API | Check API key |
| Low miner scores | Expected for baseline model — miners should build better models |
