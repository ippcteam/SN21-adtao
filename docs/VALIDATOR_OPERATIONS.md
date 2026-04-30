# Validator operations runbook (SN21)

**Status:** Draft — fill `TBD` / placeholder sections with the dev / ops team.  
**Companion:** [MINER_OPERATIONS.md](./MINER_OPERATIONS.md) · [validator_setup.md](./validator_setup.md) (install & API)

---

## 1. Purpose and audience

| Item | Value |
|------|--------|
| **Audience** | Operators running the **reference** `hope-validator` stack and (later) any production validator beside HOPE. |
| **Goal** | **Cadence**, **workload**, **HOPE API dependency**, and **handoff** steps—aligned with miners in [MINER_OPERATIONS.md](./MINER_OPERATIONS.md). |
| **Out of scope** | HOPE platform internals (migrations, payload builder) — see [GAP_CHECKLIST_HOPE_BACKEND.md](./GAP_CHECKLIST_HOPE_BACKEND.md). **Bittensor** binary and **weight-setting** on chain — add in §11 when ready. |

**Related code:** `hope/validator/`, `hope/validator/data_client.py`, `hope/validator/epoch_manager.py`, `hope/validator/api/`.

---

## 2. Workload and commitment (SLO)

Answer: **“What am I signing up to run 24/7 (or not)?”**

| Question | Placeholder / default in repo |
|----------|------------------------------|
| Must the validator be **up** for the full **48h** miner collection window? | **TBD** — *expectation: yes for active participation*; `PREDICTION_DEADLINE_HOURS` in `hope/constants.py` |
| Uptime **target** (e.g. 99% during window) | **TBD** |
| **RTO / RPO** if the process crashes mid-epoch | **TBD** |
| Who **announces** the validator URL to miners? | **TBD** |

**Rough sizing (from [validator_setup.md](./validator_setup.md) §10):**

| Resource | Minimum (reference) | Production **TBD** |
|----------|---------------------|--------------------|
| Python | 3.10–3.12 | |
| RAM | 2 GB (300 × ~15 KB episodes in memory is small; allow headroom for API + Python) | **TBD** |
| CPU | **TBD** | |
| Egress | HOPE API fetch + miner HTTP | **TBD** — *bandwidth cap* |
| Open port | Miner-facing HTTP (default `8080`) | + **TBD** — *TLS, reverse proxy* |

---

## 3. Cadence and weekly runbook

### 3.1 Parameters

| Parameter | Value | Config |
|-----------|--------|--------|
| Epoch length | 7 days | `EPOCH_DURATION_DAYS` |
| Miner submission window | 48 h (default) | `PREDICTION_DEADLINE_HOURS`, env in deployment |
| HOPE **release** cadence | Weekly (narrative) | **TBD** — *exact day/time UTC* |

### 3.2 Weekly checklist (template — adjust with HOPE)

| # | Step | Owner | TBD / notes |
|---|------|--------|-------------|
| 1 | HOPE publishes new **release key** and package available | HOPE / **TBD** | Confirm `GET .../package` returns `schema_version: v1.9` when live |
| 2 | Obtain **API key** and confirm access | **TBD** | `HOPE_API_KEY`, `HOPE_API_URL` |
| 3 | **Restart** (or reconfigure) validator with new `--release KEY` | Validator op | Downtime plan: **TBD** |
| 4 | Publish **epoch_id** and **endpoint** to miners (same as `RELEASE_KEY` if 1:1) | **TBD** | |
| 5 | Keep API up until **deadline** | | |
| 6 | **Score** and **reveal** after deadline (or `--score-now` for tests only) | | **TBD** — *automation vs manual* |
| 7 | **TBD** — *on-chain weight update, if out-of-band* | | See §11 |

**Timezone:** All operational times in **TBD** — *recommend UTC in final doc*.

### 3.3 Handoff: HOPE platform ↔ this validator

| Deliverable | Owner | TBD |
|-------------|--------|-----|
| **Package** JSON matches subnet **HopeDataClient** | HOPE + SN21 | `integrity.package_hash` algorithm documented — [GAP_CHECKLIST_TAO_DISCOVERY.md](./GAP_CHECKLIST_TAO_DISCOVERY.md) |
| Staging **release key** for joint test | **TBD** | |
| **Incident** contact for bad package | **TBD** | |

---

## 4. Install and run (reference)

Full install, CLI, and API tables: **[validator_setup.md](./validator_setup.md)**.

```bash
pip install -e .
hope-validator --release WR-2026-W18-PUB-E1 --port 8080
```

| Flag / env | Purpose |
|------------|---------|
| `--release` | **TBD** — *must match publicized miner epoch id* |
| `--api-key` / `HOPE_API_KEY` | HOPE data API |
| `--port` / `VALIDATOR_PORT` | Miner HTTP |
| `--score-now` | Development: skip waiting for miners |

**Default HOPE base URL:** `https://hope-bittensor-api.onrender.com` — `hope/constants.py` (**TBD** if production URL differs).

---

## 5. Environment and secrets

| Variable | Default (repo) | Production |
|----------|----------------|--------------|
| `HOPE_API_KEY` | See validator_setup (example only) | **TBD** — *secret store* |
| `HOPE_API_URL` / override | `HOPE_API_BASE_URL` in constants | **TBD** |
| `PREDICTION_DEADLINE_HOURS` | `48` | **TBD** if subnet changes |
| `VALIDATOR_PORT` | `8080` | **TBD** |

**Secrets management:** **TBD** — *Vault, K8s secret, .env on host, etc.*

---

## 6. HOPE data API (validator dependency)

| Endpoint | Role |
|----------|------|
| `GET /internal/bittensor/v1/releases` | List releases |
| `GET /internal/bittensor/v1/releases/{key}/package` | Episodes + outcomes + integrity |
| `GET /internal/bittensor/v1/governance/summary` | Stats |

**Failure modes:**

| Symptom | Action |
|---------|--------|
| Package **hash** mismatch | **TBD** — *retry, escalate to HOPE if algorithm drift* (see gap checklist) |
| **401/403** | Rotate / check API key |
| **Empty** `payload` in episodes | Escalate — HOPE builder issue |

**Rate limits / SLO from HOPE:** **TBD**

---

## 7. Epoch lifecycle (operational view)

State machine: **IDLE → PREPARING → … → COMPLETE** — [validator_setup.md](./validator_setup.md) §5.

| State | Operator concern |
|-------|------------------|
| PREPARING | HOPE fetch + hash verification |
| COMMITTED | Miners can trust commitment **TBD** — *comms* |
| COLLECTING | Miners must finish before deadline |
| SCORING / REVEALING | Expose `scores` and `verification` per API |

**Automation:** **TBD** — *Cron / workflow to `score_epoch()` after deadline; today may be script/manual.*

---

## 8. Miner-facing API (contract)

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | None |
| GET | `/v1/epochs/{id}/episodes` | `X-Miner-Hotkey` |
| GET | `/v1/epochs/{id}/episodes/{ep_id}` | same |
| GET | `/v1/epochs/{id}/episodes_batch` | same |
| POST | `/v1/epochs/{id}/predictions` | same |
| GET | `/v1/epochs/{id}/commitment` | **TBD** public? |
| GET | `/v1/epochs/{id}/verification` | post-reveal |
| GET | `/v1/epochs/{id}/scores` | post-scoring **TBD** access policy |

**TLS / DDoS / `allowed_hosts`:** **TBD**

---

## 9. Scoring and integrity

- Library: `hope/scoring/`, invoked from epoch manager after collection.
- **Weight ranges** and defaults: [validator_setup.md](./validator_setup.md) §8.
- **TBD:** Ensure deployed validator uses **same** `ScoringWeights` as committed in epoch (if weights change per epoch, document in verification payload).

---

## 10. Production deployment (extend when ready)

| Topic | TBD |
|------|-----|
| Process manager (systemd, K8s, etc.) | **TBD** |
| **Logging** (structured JSON, log level) | **TBD** |
| **Metrics** (Prometheus: fetch latency, active epochs, request count) | **TBD** |
| **Alerts** (API down, HOPE 5xx) | **TBD** |
| **Backups** (epoch state) | **TBD** — *if state is local* |
| **Multi-region** or multiple validators | **TBD** |

**Rollback:** If bad package, **TBD** — *HOPE feature flag; see developer spec §13*.

---

## 11. Bittensor chain integration (out of this repo)

This repository implements **HTTP** serving and **local** scoring. The following are **not** specified here; add subsections when the team has answers:

| Item | TBD content |
|------|-------------|
| **Subtensor** endpoint (mainnet / testnet) | |
| **Validator** process (`btcli` / neuron) and relationship to `hope-validator` | *Same process vs sidecar* |
| **Weight** setting, tempo, and **hyperparameters** (SN21) | |
| **Reg** key / hotkey / stake requirements for validators | |
| **Synapse** wire format if not HTTP: | `hope/protocol/synapse.py` exists — **TBD** mapping |

**Owner for §11:** **TBD**

---

## 12. Checklists

### 12.1 New epoch (weekly)

- [ ] Confirm **release** exists on HOPE with expected **episode count** and **schema**.
- [ ] Set `--release`, secrets, and port.
- [ ] **TBD** — *smoke: `GET /health`, one episode fetch*.
- [ ] Publish miner instructions (URL + key).
- [ ] After deadline: run scoring, verify `/verification`, **TBD** chain step.

### 12.2 Incident: miners cannot submit

- [ ] Validator reachable? TLS? Firewall?
- [ ] Epoch id match?
- [ ] **TBD** — *rate limit*.

---

## 13. Document control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 0.1 | **TBD** | **TBD** | Initial placeholders |

**Review cadence:** **TBD**
