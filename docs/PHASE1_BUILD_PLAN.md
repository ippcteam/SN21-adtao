# Phase 1 Build Plan — Validator + Miner MVP

**Repo:** `tao-discovery` (https://github.com/ippcteam/tao-discovery)
**Approach:** Simplified — our own validator, HTTP data delivery, verifiable scoring
**Depends on:** Phase 0 scoring library (done), HOPE data API (done, live on Render)

---

## What We're Building

A working validator that:
1. Fetches weekly challenge packages from HOPE's API
2. Serves episodes to miners via HTTP
3. Collects miner predictions
4. Scores predictions against ground truth outcomes
5. Makes scoring verifiable (commitment before reveal)

A working miner SDK that:
1. Fetches episodes from the validator
2. Runs a prediction model (baseline included)
3. Submits predictions back
4. Can verify its own scores locally

Mining instructions adapted for v1.9 schema (launch scope).

---

## Build Order (5 steps)

### Step 1: Protocol + Data Client

**What:** Synapse definitions + HOPE data client that pulls from our live API.

Files:
- `hope/protocol/synapse.py` — EpochAnnouncement, Heartbeat, CommitmentReveal
- `hope/validator/data_client.py` — HopeDataClient (fetches `/releases/<key>/package`)

Test: Client fetches real package from HOPE API (via HOPE_API_URL env var), parses all 101 episodes.

### Step 2: Validator HTTP API

**What:** FastAPI server that miners connect to. Hotkey auth middleware.

Files:
- `hope/validator/api/server.py` — FastAPI app factory
- `hope/validator/api/episodes.py` — `GET /v1/epochs/{id}/episodes`
- `hope/validator/api/predictions.py` — `POST /v1/epochs/{id}/predictions`
- `hope/validator/api/commitments.py` — `GET /v1/epochs/{id}/commitment`
- `hope/validator/api/verification.py` — `GET /v1/epochs/{id}/verification`
- `hope/validator/api/auth.py` — Hotkey signature verification

Test: Miner can fetch episodes and submit predictions via HTTP.

### Step 3: Epoch Manager + Scoring Pipeline

**What:** Epoch lifecycle state machine + scoring integration.

Files:
- `hope/validator/epoch_manager.py` — State machine (PREPARING → COMMITTED → DISTRIBUTING → COLLECTING → SCORING → REVEALING → COMPLETE)
- `hope/validator/scoring_pipeline.py` — Wire EpochScorer to the epoch lifecycle
- `hope/validator/weight_setter.py` — Normalize scores → set_weights (placeholder for testnet)

Test: Full epoch lifecycle with synthetic predictions scored correctly.

### Step 4: Commitment Protocol (Simplified)

**What:** Pre-commit outcomes hash before distributing episodes. Reveal after scoring.

Files:
- `hope/commitment/hashing.py` — SHA256 hash of outcomes + scoring weights
- `hope/commitment/merkle.py` — Merkle tree for per-episode outcome proofs
- `hope/commitment/verification.py` — Verify revealed outcomes match commitment

Test: Commitment published, outcomes revealed, verification passes.

### Step 5: Miner SDK + Baseline Model + Mining Guide

**What:** Everything a miner needs to participate.

Files:
- `hope/miner/runner.py` — Main loop (register, fetch, predict, submit)
- `hope/miner/prediction_engine.py` — Abstract base class
- `hope/miner/episode_client.py` — HTTP client to fetch episodes
- `hope/miner/prediction_client.py` — HTTP client to submit predictions
- `hope/miner/models/baseline.py` — Reference model from miner_quickstart Section 9
- `docs/miner_quickstart.md` — Adapted for v1.9 schema, 7+14 day horizons
- `docs/validator_setup.md` — How to run the validator
- `scripts/score_predictions.py` — Offline scoring tool for miners

Test: Miner runs end-to-end against validator, baseline model produces valid predictions, scores computed.

---

## Data Delivery Flow

```
HOPE DB (campaign_daily_performance, changelog, outcomes)
    |
    v
HOPE Data API (/internal/bittensor/v1/releases/<key>/package)
    |  (authenticated, X-API-Key)
    v
HopeDataClient (in validator)
    |
    v
Validator stores episodes + outcomes locally
    |
    |── Commits outcome hash on-chain (BEFORE distributing)
    |
    v
Validator HTTP API (FastAPI)
    |  (authenticated, miner hotkey signature)
    |
    ├── GET /v1/epochs/{id}/episodes → miners fetch episodes
    ├── POST /v1/epochs/{id}/predictions → miners submit predictions
    ├── GET /v1/epochs/{id}/commitment → anyone verifies commitment
    └── GET /v1/epochs/{id}/verification → post-scoring, outcomes revealed
```

## Verification Flow (How miners verify scoring is fair)

1. **Before epoch starts:** Validator publishes `commitment_hash = SHA256(outcomes + salt + weights)` on-chain
2. **Miners submit predictions** within the deadline
3. **After deadline:** Validator scores all predictions, then reveals:
   - The actual outcomes
   - The salt
   - The scoring weights used
4. **Anyone can verify:** `SHA256(revealed_outcomes + revealed_salt + revealed_weights) == committed_hash`
5. **Per-episode verification:** Merkle proofs allow verifying individual episode outcomes without downloading all outcomes

This ensures the validator cannot change outcomes after seeing predictions.

---

## Launch Scope Constraints (v1.9)

- Horizons: **7 + 14 days only** (no 28-day)
- Campaigns: **SEARCH only**
- Actions: **Campaign-level only** (budget, bid strategy, pause, enable)
- Measurement resolution: **HIGH only**
- TRUST enrichment: **optional** (most episodes are BASELINE for launch)
- Pre-window: **60 days** (not 90)

These constraints simplify the miner's task for launch. The scoring library already supports the full spec — constraints are in the data, not the code.

---

## Acceptance Criteria

- [ ] Validator fetches live package from HOPE API (101 episodes)
- [ ] Validator serves episodes via HTTP with hotkey auth
- [ ] Miner fetches episodes, baseline model produces valid predictions
- [ ] Predictions submitted and stored per-epoch
- [ ] Scoring produces correct results (verified against Phase 0 tests)
- [ ] Commitment hash published before episode distribution
- [ ] Revealed outcomes verify against commitment
- [ ] Mining guide documents the full flow with worked examples
- [ ] `scripts/score_predictions.py` works for offline miner testing
