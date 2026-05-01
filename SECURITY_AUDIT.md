# SN21 Security Audit & Remediation Tracker

**Audit date:** 2026-05-01
**Source:** Tensora code review + internal deep audit
**Principle:** Can we PROVE this data hasn't been manipulated? HOPE's data, validator scores, miner answers — every link in the chain must be cryptographically verifiable.

---

## Phase 1: CRITICAL — Must fix before any public launch

### 1.1 Cryptographic Proof Chain (Data Integrity)

- [ ] **A1 — HOPE must sign episode data and outcomes**
  - File: `data_client.py`
  - Problem: HOPE provides episodes AND outcomes as plain JSON. No signature. HOPE could change outcomes after seeing miner predictions to favor specific miners.
  - Fix: HOPE signs every API response with an ed25519 keypair. Validator verifies signature before accepting data. HOPE's public key published in repo / on-chain.

- [ ] **A2 — Package hash is self-referential (useless)**
  - File: `data_client.py:222`
  - Problem: Hash computed by HOPE over its own response. Proves nothing — HOPE always makes the hash match.
  - Fix: Replace with HOPE's cryptographic signature. Validator verifies against HOPE's public key, not a self-computed hash.

- [ ] **A3 — Commitment hash computed AFTER seeing predictions + outcomes**
  - File: `epoch_manager.py:204`
  - Problem: Commitment = SHA256(outcomes + salt + weights), computed after validator sees everything. Not published before scoring. Proves zero.
  - Fix: Two-phase commitment:
    1. **Episode commitment** — hash of all episodes published on-chain or broadcast BEFORE miners receive data
    2. **Prediction commitment** — Merkle root of all collected predictions published BEFORE outcomes are fetched
    3. **Outcome commitment** — HOPE signs outcomes BEFORE validator sees predictions (HOPE commits to outcomes at epoch creation, sealed until deadline)

- [ ] **A4 — Validator can edit miner predictions before scoring**
  - File: `epoch_manager.py:61`
  - Problem: Predictions stored as mutable Python dict. No integrity seal. Validator can modify any prediction between collection and scoring.
  - Fix: Each prediction hashed on receipt. Merkle tree built over all predictions. Root published before scoring. Any modification invalidates the tree.

- [ ] **A5 — No miner-signed prediction receipts**
  - File: `predictions.py:135-142`
  - Problem: Miner submits raw JSON, validator constructs objects server-side. Miner has no proof their prediction wasn't altered.
  - Fix: Miner signs SHA256(prediction_payload) with hotkey. Validator stores signature alongside prediction. Returns signed receipt (validator-signed hash of miner's submission + timestamp). Miner can later prove what they submitted.

- [ ] **A6 — No on-chain episode commitment at distribution time**
  - File: `runner.py`
  - Problem: Episodes served to miners but never anchored anywhere immutable. HOPE could serve different data to different validators.
  - Fix: Validator computes hash of episode bundle, publishes to chain or IPFS before serving to miners.

- [ ] **A7 — Phase separation is unverifiable**
  - File: `epoch_manager.py`
  - Problem: All timestamps are validator-local. Modified binary could fetch outcomes anytime.
  - Fix: Key phase transitions (epoch start, deadline, outcome fetch) should have on-chain timestamps or be verifiable against block heights.

### 1.2 Authentication & Signatures

- [ ] **B1 — Signature not bound to HTTP method/URL/body**
  - File: `auth.py:46`
  - Problem: Signed message is `SHA256(hotkey:nonce)`. Does NOT cover method, path, or body. Captured signature replays to any endpoint with any payload.
  - Fix: Signed message must be `SHA256(hotkey:nonce:method:path:body_hash)`. Verifier reconstructs and checks.

- [ ] **B2 — No nonce replay protection**
  - File: `auth.py:99-105`
  - Problem: No seen-nonce tracking. Same (hotkey, nonce, signature) replayable unlimited times within 5-minute window.
  - Fix: Track seen nonces in a set/dict with TTL. Reject any nonce already consumed.

- [ ] **B3 — Non-numeric nonce bypasses expiry entirely**
  - File: `auth.py:106-107`
  - Problem: `float(nonce)` ValueError caught with bare `pass`. Nonce like "abc" never expires.
  - Fix: Reject non-numeric nonces immediately with 401.

- [ ] **B4 — REQUIRE_SIGNATURES defaults to false**
  - File: `auth.py:93`
  - Problem: Default = unauthenticated. Anyone impersonates any miner by setting X-Miner-Hotkey header.
  - Fix: Default to `true`. Only allow `false` in explicit test/dev mode.

- [ ] **B5 — Failed signatures accepted when REQUIRE_SIGNATURES=false**
  - File: `auth.py:111-113`
  - Problem: Bad signature → warning → proceeds. Request accepted.
  - Fix: If signature is provided, it MUST validate regardless of REQUIRE_SIGNATURES. Fail closed.

- [ ] **B6 — Empty registered_miners bypasses metagraph check**
  - File: `auth.py:84-88`
  - Problem: Empty set = check skipped. Any hotkey accepted during startup.
  - Fix: If metagraph not synced, reject ALL requests (503 Service Unavailable).

- [ ] **B7 — Missing substrateinterface silently disables verification**
  - File: `auth.py:53-54`
  - Problem: ImportError → returns False silently. Zero auth without the package.
  - Fix: Fail at startup if substrateinterface not importable. Log CRITICAL, refuse to start.

### 1.3 Prediction Submission Exploits

- [ ] **C1 — Duplicate episode predictions scored independently**
  - File: `predictions.py:142`, `scorer.py:74`
  - Problem: Same episode_id submitted N times → scored N times → miner amplifies favorable episodes.
  - Fix: Deduplicate — last prediction per episode per miner wins. Dict keyed by (hotkey, episode_id), not list.

- [ ] **C2 — Cherry-picking: skipped episodes cost nothing**
  - File: `scorer.py:74-88`
  - Problem: Score = average over ONLY submitted episodes. Skip hard ones → inflated average.
  - Fix: Coverage penalty. Score = raw_average * coverage_factor. If miner covers 10/50 episodes, coverage_factor penalizes heavily. Minimum coverage threshold below which miner scores 0.

- [ ] **C3 — No payload size limit**
  - File: `predictions.py:53`
  - Problem: `predictions: list[PredictionSubmission]` has no max_length. Millions of entries → OOM.
  - Fix: Add `max_length` to Pydantic model. Cap at episode_count (e.g., 200). Also add FastAPI request body size limit middleware.

- [ ] **C4 — Rate limit counts requests, not predictions**
  - File: `predictions.py:29-43`
  - Problem: 5 requests/epoch, each with unlimited predictions. Effective = 5 * infinity.
  - Fix: Rate limit by total predictions submitted. Max = episode_count * N (e.g., 2x for resubmission headroom).

- [ ] **C5 — Null penalty blind to skipped episodes**
  - File: `null_penalty.py:38-43`
  - Problem: Near-zero fraction computed over submitted predictions only. Submit 2 genuine, skip 48 → 0% penalty.
  - Fix: Compute near-zero fraction over ALL episodes in the epoch, not just submitted ones. Skipped = near-zero.

- [ ] **C6 — Null penalty threshold gaming at boundary**
  - File: `null_penalty.py:30`
  - Problem: `p50=1.0` is `>= threshold(1.0)` → evades penalty. Functionally near-zero.
  - Fix: Raise threshold to 2.0-3.0 OR use strict greater-than (`>`) instead of `>=`.

- [ ] **C7 — Only p50 checked for null penalty**
  - File: `null_penalty.py:30-35`
  - Problem: p10/p90 not checked. Set p50=1.01, p10=0, p90=2 → interval centered on zero, evades check.
  - Fix: Check interval width (p90-p10) as well. Narrow interval centered near zero = near-zero prediction.

- [ ] **C8 — submission_open defaults to True**
  - File: `predictions.py:81`
  - Problem: Missing key = permanently open. Submissions accepted after outcomes known.
  - Fix: Default to `False`. Require explicit `True` to accept submissions.

- [ ] **C9 — In-memory rate limit not shared across workers**
  - File: `predictions.py:25-26`
  - Problem: Each uvicorn worker has own counter. Rate limit * worker_count.
  - Fix: Use shared state (Redis, file lock, or single-worker mode for validator).

- [ ] **C10 — No quantile prediction value bounds**
  - File: `predictions.py:119-128`
  - Problem: `p10=-1e18, p90=1e18` accepted. Extreme width.
  - Fix: Clamp quantile values to [-100, 100] or similar reasonable range for percentage deltas.

### 1.4 DDoS & Infrastructure

- [ ] **D1 — No IP-level rate limiting**
  - File: `server.py`
  - Problem: Zero middleware. Miner submits own predictions, then floods server to block others before deadline.
  - Fix: Add slowapi or custom middleware. Global IP rate limit (e.g., 60 req/min per IP). Stricter on POST endpoints.

- [ ] **D2 — No request body size limit**
  - File: `server.py`
  - Problem: No limit. Multi-GB payload crashes server.
  - Fix: Add middleware limiting request body to 1MB (predictions are ~50KB max for 200 episodes).

- [ ] **D3 — Unauthenticated endpoints exposed to flooding**
  - File: `server.py`
  - Problem: /health, /commitment, /verification, /scores, /training/* — no auth, no throttle.
  - Fix: Rate limit all endpoints. Public endpoints get stricter limits. Training endpoints require auth or pagination.

- [ ] **D4 — /training/episodes serves entire dataset per request**
  - File: `training.py`
  - Problem: Returns full JSON (MBs) per request. No pagination, no auth, no cache.
  - Fix: Add auth. Add pagination. Add response caching.

- [ ] **D5 — Uvicorn runs with no connection limits**
  - File: `runner.py:211`
  - Problem: No limit_concurrency, limit_max_requests, timeout_keep_alive.
  - Fix: Set `limit_concurrency=100`, `limit_max_requests=10000`, `timeout_keep_alive=5`.

- [ ] **D6 — Render starter plan, minimal DDoS protection**
  - File: `render.yaml`
  - Problem: No CDN, no WAF.
  - Fix: Consider Cloudflare proxy or Render Pro plan. At minimum, IP rate limiting in app layer (D1).

---

## Phase 2: Cleanup — Must complete before repo goes public

### 2.1 Remove all person/company references

- [ ] **E1-E11 — Remove all "Tensora" references** (11 occurrences in 7 files)
  - `hope/miner/prediction_client.py:3`
  - `hope/miner/runner.py:8`
  - `hope/constants.py:80`
  - `hope/validator/weight_setter.py:12,25`
  - `hope/validator/epoch_manager.py:6`
  - `hope/validator/runner.py:13,110`
  - `hope/validator/api/auth.py:9`
  - `hope/validator/api/predictions.py:3,23`

- [ ] **E12-E14 — Remove all "Rob" references** (3 occurrences)
  - `hope/constants.py:66`
  - `hope/validator/epoch_manager.py:110,132`

- [ ] **E15 — Review PPCRebel/ippcteam references**
  - `LICENSE:3`, `README.md:201`
  - `docs/validator_setup.md:11`, `docs/PHASE1_BUILD_PLAN.md:3`
  - `docs/SN21_REWARD_MECHANISM.md:49`, `docs/miner_quickstart.md:25`, `README.md:53`

### 2.2 Delete internal development documents

- [ ] **F1** — Delete `docs/GAP_CHECKLIST_HOPE_BACKEND.md`
- [ ] **F2** — Delete `docs/GAP_CHECKLIST_TAO_DISCOVERY.md`
- [ ] **F3** — Delete `docs/PHASE1_BUILD_PLAN.md`
- [ ] **F4** — Delete `docs/VALIDATOR_OPERATIONS.md` (or resolve all 30+ TBDs)
- [ ] **F5** — Delete `docs/MINER_OPERATIONS.md` (or resolve all 40+ TBDs)

### 2.3 Remove hardcoded values

- [ ] **G1** — Move `WR-2026-W18-PUB-E1` defaults to env vars (runner.py, render.yaml, start_validator.sh, scripts)
- [ ] **G2** — Move testnet wallet name `sn21-testnet-1` to env var only
- [ ] **G3** — Move testnet netuid `466` to env var only
- [ ] **G4** — Remove `https://hope.ppcrebel.com` from README.md

---

## Scoring Integrity (Additional findings)

- [ ] **A8 — Scoring weights never validated against published ranges**
  - File: `weights.py:21`
  - Problem: `validate_ranges()` exists but is never called in the scoring pipeline.
  - Fix: Call `validate_ranges()` at scorer initialization. Reject out-of-range weights.

- [ ] **A9 — `verified` flag has no downstream effect**
  - File: `auth.py:33`
  - Problem: MinerIdentity.verified set but never checked. Unverified miners scored same as verified.
  - Fix: Reject unverified miners from prediction submission. Or flag in scoring.

- [ ] **C calibration bias** — Large actuals make wide intervals nearly free due to normalization by `max(abs(actual), 1.0)`. Known scoring design tradeoff, not a code bug. Monitor post-launch.

---

## Execution Order

Work in this order — each phase builds on the previous:

1. **Auth hardening** (B1-B7) — foundational, everything depends on knowing who submitted what
2. **Prediction integrity** (C1-C10) — stop exploit vectors in submission pipeline
3. **DDoS protection** (D1-D6) — keep the server alive under attack
4. **Data integrity chain** (A1-A7) — cryptographic proof chain (largest effort)
5. **Scoring integrity** (A8-A9) — weight validation, verified flag
6. **Cleanup** (E, F, G) — references, docs, hardcoded values (do last, quick)

---

## Verification

After all fixes, every link in the chain must be provable:

```
HOPE signs episodes (ed25519) ──────────────────────────────────┐
                                                                 │
Validator verifies HOPE signature                                │
Validator publishes episode_hash on-chain ───────────────────────┤
                                                                 │
Miners receive episodes, verify against on-chain hash            │
Miners sign predictions with hotkey (covers full payload)        │
Validator returns signed receipt to miner                        │
                                                                 │
Deadline passes                                                  │
Validator publishes prediction Merkle root ──────────────────────┤
                                                                 │
HOPE reveals signed outcomes (pre-committed at epoch creation)   │
Validator verifies HOPE outcome signature                        │
                                                                 │
Validator scores (weights validated against ranges)              │
Validator publishes scores + proofs                              │
                                                                 │
Miners verify:                                                   │
  - Episode hash matches on-chain commitment                     │
  - Their prediction is in the Merkle tree (inclusion proof)     │
  - Outcomes signature valid from HOPE                           │
  - Scoring weights within published ranges                      │
  - Re-run scoring locally to verify their score                 │
```

**The test:** At every arrow above, an external observer with no trust in HOPE, the validator, or any miner can independently verify correctness.
