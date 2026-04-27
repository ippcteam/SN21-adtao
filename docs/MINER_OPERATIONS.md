# Miner operations runbook (SN21)

**Status:** Draft — fill `TBD` / placeholder sections with the dev / ops / product team.  
**Companion:** [VALIDATOR_OPERATIONS.md](./VALIDATOR_OPERATIONS.md) · [miner_quickstart.md](./miner_quickstart.md) (task & scoring detail)

---

## 1. Purpose and audience

| Item | Value |
|------|--------|
| **Audience** | Operators running mining software against SN21 (inference, batch jobs, custom models). |
| **Goal** | Single place for **cadence**, **submit path**, **scores**, and **operational requirements**—beyond the tutorial quickstart. |
| **Out of scope (document elsewhere)** | Bittensor wallet registration details, exact emission schedule, AdTAO/Discord comms, HOPE data pipeline. |

**Related code:** `hope/miner/`, `hope/protocol/`, `hope/scoring/` (read-only for understanding scores).

---

## 2. Should I mine? (decision checklist)

Use this for **A — go/no-go** before investing in hardware and integration.

| Question | Source of truth (fill in) | Status |
|----------|---------------------------|--------|
| What is the expected **emission** or incentive per epoch? | **TBD** — *link to subnet economics / team doc* | ☐ |
| What is the **competitive** landscape (rough active miner count)? | **TBD** | ☐ |
| What are **registration** or **stake** requirements on SN21? | **TBD** — *subtensor / subnet hyperparameters* | ☐ |
| Is mining **HTTP-only** to a validator, **chain-signed**, or both at launch? | **TBD** — *this repo is HTTP reference; see §6* | ☐ |
| **Slashing** or penalty risks for bad submissions? | **TBD** | ☐ |
| **Minimum** viable hardware to be competitive (not just baseline)? | **TBD** — *see §3* | ☐ |

**Engineering sign-off:** **TBD** (name, date)

---

## 3. Compute and infrastructure

### 3.1 Baseline / smoke test (from repo)

| Resource | Baseline model (`hope/miner/models/baseline.py`) |
|----------|--------------------------------------------------|
| CPU | **TBD** — *qualify minimal vCPU* |
| RAM | **TBD** — *e.g. &lt; 512 MB for single-epoch test* |
| GPU | Not required for baseline. |
| Network | Sufficient to download N episodes and POST predictions (see §4 for N). |

### 3.2 Production mining (to be defined with ML team)

| Item | Placeholder |
|------|-------------|
| Target **inference** latency per episode (p50 / p99) | **TBD** |
| **Batch** size or parallel workers | **TBD** |
| **GPU** requirement (if any) for competitive models | **TBD** — *framework: PyTorch, XGBoost CPU, etc.* |
| **Storage** (cached episodes, model artifacts) | **TBD** |
| **Training** (if allowed off-chain, schedule vs weekly epoch) | **TBD** — *policy* |

**Capacity formula (optional, fill in):**  
`episodes_per_epoch` × `inference_time_per_episode` &lt; `window_hours` (see §4)

---

## 4. Cadence and schedule

### 4.1 Published parameters (code / docs)

| Parameter | Value in repo | Notes |
|-----------|----------------|-------|
| `EPOCH_DURATION_DAYS` | `7` | `hope/constants.py` |
| `PREDICTION_DEADLINE_HOURS` | `48` | `hope/constants.py`; env may override in validator |
| Release pattern | `WR-YYYY-Www-PUB` (+ optional epoch suffix) | **TBD** — *canonical key format for mainnet* |

### 4.2 Weekly calendar (fill with ops)

| Event | When (UTC) | Owner | Notes |
|--------|----------------|--------|--------|
| New **release** published by HOPE | **TBD** — *e.g. Monday 00:00 UTC* | **TBD** | |
| **Validator** starts epoch / miners may fetch | **TBD** | | |
| **Submission deadline** (48h after start unless changed) | **TBD** | | |
| **Scoring** and **reveal** | **TBD** | | |
| **On-chain weight** update (if applicable) | **TBD** | | *Not defined in this repo* |

**Horizon note:** Scoring uses **7d and 14d** outcomes. Document when each horizon is **measured and final** for a given action window: **TBD** — *product / HOPE*.

### 4.3 Communication to miners (fill in)

| Channel | What is announced | Link |
|---------|-------------------|------|
| Discord / blog / on-chain | **TBD** | **TBD** |
| **Release key** for next epoch | **TBD** | Must match validator `--release` and miner `--epoch`. |

---

## 5. How to mine (reference flow)

Detailed steps, episode schema, and model API: **[miner_quickstart.md](./miner_quickstart.md)**.

**CLI (reference):**

```bash
pip install -e ".[miner]"
hope-miner --validator-url http://VALIDATOR_IP:PORT --hotkey YOUR_HOTKEY --epoch RELEASE_KEY
```

| Field | Description |
|-------|-------------|
| `VALIDATOR_IP:PORT` | **TBD** — *public endpoint of chosen validator* |
| `RELEASE_KEY` | Must match the active epoch on that validator. |

**Custom model:** Subclass `PredictionEngine` and use `MinerRunner` — see quickstart.

---

## 6. How to submit predictions

### 6.1 Reference HTTP API (this repository)

| Step | Details |
|------|---------|
| Auth | `X-Miner-Hotkey` header (simplified; **TBD** for production Ed25519 verification). |
| Fetch | `GET /epochs/{epoch_id}/episodes` or `.../episodes_batch` |
| Submit | `POST /epochs/{epoch_id}/predictions` |

**Interactive docs** on a running validator: `/docs` (FastAPI).

### 6.2 Production / mainnet (TBD with subnet team)

| Item | Status |
|------|--------|
| Signed payloads or metagraph check | **TBD** |
| Required **hotkey** / coldkey / subnet UID steps | **TBD** |
| Sybil or rate limits | **TBD** |

---

## 7. How scores are calculated and communicated

### 7.1 Component scores (documented in repo)

Full formulas: **[miner_quickstart.md](./miner_quickstart.md)** §5.

| Component | Default weight | Notes |
|-----------|----------------|--------|
| Quantile accuracy | 50% | P10/P50/P90 |
| Calibration | 20% | |
| Directional | 15% | |
| Goal accuracy | 15% | |
| **Horizon** split (high res) | t7: 40%, t14: 60% | `hope/constants.py` `HORIZON_WEIGHTS` |
| **Null** penalty | See quickstart | Near-zero predictions |
| **Skill** vs predict-zero baseline | **TBD in prose** — *see* `hope/scoring/skill_score.py` | |
| **Episode weight** (e.g. `trust_enriched` vs `baseline`) | **TBD** — *confirm multiplier in* `hope/scoring/scorer.py` *vs reward paper* | |

### 7.2 How you **receive** scores

| Channel | When available | TBD / detail |
|---------|----------------|--------------|
| `GET /epochs/{epoch_id}/scores` (validator API) | After scoring | **TBD** — *exposure policy (public or miner-only)* |
| `GET /epochs/{epoch_id}/verification` | After reveal | Commitment + outcomes + weights for self-verify |
| **Local** recompute | Anytime with saved JSON | `hope-score` — see quickstart §9 |
| **On-chain** weight / reward | | **TBD** — *not specified in this repo* |

**Verify independently:** [miner_quickstart.md](./miner_quickstart.md) §9 (commitment check).

### 7.3 What miners must not rely on (until specified)

- Exact **final** `trust_enriched` **multiplier** (must match published reward spec).
- **Tie-breaking** or minimum score for inclusion in weights.
- **Per-horizon** reward timing (7d vs 14d) if they score on different **weeks**: **TBD** — *product*.

---

## 8. Operational checklists

### 8.1 Pre-epoch

- [ ] Confirm **release key** and **validator URL** (same epoch on both).
- [ ] Time sync (NTP) if using deadline validation.
- [ ] **TBD** — *model version tag in prediction metadata* (if required later).
- [ ] Disk space for episode cache **TBD** — *N × ~15 KB*.

### 8.2 During epoch

- [ ] Fetch + infer + submit before **deadline** (`PREDICTION_DEADLINE_HOURS` on validator).
- [ ] **TBD** — *retry policy on 4xx/5xx*.

### 8.3 Post-epoch

- [ ] Pull **scores** / **verification** from validator.
- [ ] Run **hope-score** on saved files to audit (optional).
- [ ] **TBD** — *log retention and PII policy* (hashed IDs only in payloads).

---

## 9. Escalation and support

| Issue type | Contact / tracker |
|------------|-------------------|
| API or validator down | **TBD** |
| Scoring disagrees with local `hope-score` | Open issue with `epoch_id`, `episode_id`, inputs redacted. |
| HOPE / release package | **TBD** — *HOPE team* |

---

## 10. Document control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 0.1 | **TBD** | **TBD** | Initial placeholders |

**Review cadence:** **TBD** (e.g. before each testnet / mainnet phase)
