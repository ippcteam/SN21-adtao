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

> **Architecture note (read this first).** SN21's validator code ships as
> **three separate binaries** that run independently. **You need to run
> all three for a healthy validator** — running only one or two will
> either fail to score or get pruned from Yuma consensus.
>
> - **`hope-validator-api`** — long-lived **HTTP daemon** that serves
>   episodes to miners and accepts their public-facing health checks.
>   This is the binary that takes `--port` and `--host`.
> - **`hope-validator`** — one-shot **scoring pass** invoked once per
>   epoch (weekly) after the miner deadline (typically from cron). It
>   reads on-chain miner submissions, evaluates scoreability, commits
>   weights, and exits. **No `--port` flag** — it is not an HTTP server.
> - **`hope-validator-heartbeat`** — short-lived **activity-floor cron**
>   invoked every 3-4 hours. It re-asserts the latest weights commit so
>   Bittensor's `ActivityCutoff` (~16h on mainnet) does not drop your
>   validator from consensus between weekly scoring runs. Without this,
>   your validator gets pruned from emission every Tuesday-ish. See
>   §10.4 for full details.
>
> If you see a reference to `hope-validator --port`, treat it as a
> documentation drift and use `hope-validator-api --port` for HTTP and
> plain `hope-validator` for scoring.

---

## 1. Installation

```bash
git clone <repo-url>
cd SN21-adtao
pip install -e .
```

---

## 2. Quick Start (Local Testing)

**Three** commands run as **three** independent processes — typically
one long-lived HTTP service, one weekly cron, and one frequent
(3-4 hour) cron:

```bash
# Process A — episode-serving HTTP daemon (long-lived)
hope-validator-api \
    --release WR-2026-W19-PUB-E1 \
    --host 0.0.0.0 --port 8080 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default
```

```bash
# Process B — one-shot weekly scoring pass (run AFTER the miner deadline)
hope-validator \
    --release WR-2026-W19-PUB-E1 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/.sn21/keys/validator-ed25519.pem
```

```bash
# Process C — activity-floor heartbeat (run every 3-4 hours from cron)
hope-validator-heartbeat \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default
```

Roles in one line each:

- **A** (`hope-validator-api`) serves episodes to miners, runs forever.
- **B** (`hope-validator`) scores after each weekly mining deadline,
  produces the on-chain `9.C.1 → 9.C.3 → 9.C.2 → 9.C.6` artifact
  sequence, and exits.
- **C** (`hope-validator-heartbeat`) re-asserts your latest weights
  every few hours so Bittensor's `ActivityCutoff` (~16h on mainnet)
  does not drop you from consensus between weekly scoring runs.
  Self-throttles via `LastUpdate` check — safe to run every 3-4h on
  cron. See §10.4 for the full mechanism.

**All three are required for sustained operation.** Skipping the
heartbeat means your validator gets pruned from emission a day or
two after each scoring run — even if your scoring is otherwise
flawless.

For testnet, swap `--network test --netuid 466`; for mainnet use
`--network finney --netuid 21` (the defaults).

---

## 3. Running with Miners

### Start the episode API daemon

```bash
hope-validator-api \
    --release WR-2026-W19-PUB-E1 \
    --host 0.0.0.0 --port 8080 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default
```

This starts the FastAPI server and waits for miners to connect. The daemon:
- Fetches episodes from the data API at startup
- Serves episodes at `<validator-url>/v1/epochs/{epoch_id}/episodes`
- Authenticates miner requests against the on-chain metagraph
- Does **not** accept HTTP-posted predictions — production miners submit
  via on-chain commits (see [Layer 9.B](whitepaper.md)). The
  `POST /v1/epochs/{id}/predictions` endpoint still exists for dev/local
  workflows but is not part of the canonical scoring path.

### Tell miners your endpoint

Miners connect with:

```bash
hope-miner --validator-url <validator-url> --epoch WR-2026-W19-PUB-E1 ...
```

See `docs/miner_quickstart.md` for the full miner-side command.

### Score after deadline

Run the one-shot scorer once per epoch, after the miner deadline AND
after drand auto-reveal has fired (~60 minutes past the deadline at
default reveal-block settings):

```bash
hope-validator \
    --release WR-2026-W19-PUB-E1 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/.sn21/keys/validator-ed25519.pem
```

The scorer reads on-chain miner submissions, fetches AES ciphertext
from the archive, decrypts with the chain-revealed key, runs the
8-check scoreability rule, computes scores, and writes the
`9.C.1 → 9.C.3 → 9.C.2 → 9.C.6` artifact sequence on chain. It is
idempotent against chain state: a re-run for an already-scored
(validator, epoch) pair exits cleanly without re-committing.

Typical operator deployment runs `hope-validator` from a weekly cron;
see `deploy/validator_scoring/README.md` for the canonical Render
configuration.

---

## 4. API Endpoints

Once `hope-validator-api` is running, the daemon exposes:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | None | Daemon status / current epoch |
| `GET` | `/` | None | Service banner |
| `GET` | `/v1/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/v1/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/v1/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `GET` | `/v1/epochs/{id}/episode-commitment` | None | Per-episode commitment hash |
| `GET` | `/v1/epochs/{id}/commitment` | None | Epoch commitment proof |
| `POST` | `/v1/epochs/{id}/predictions` | Hotkey | Submit predictions (dev/local; production miners submit on chain via Layer 9.B) |
| `GET` | `/v1/epochs/{id}/my-predictions` | Hotkey | Inspect predictions previously POSTed by the caller |
| `GET` | `/v1/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |
| `GET` | `/v1/epochs/{id}/scores` | None | Per-miner scores (post-scoring) |
| `GET` | `/v1/training/episodes` | None | Historical training episodes |
| `GET` | `/v1/training/summary` | None | Training-set composition stats |

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

The protocol's epoch lifecycle is **chain-anchored** rather than driven
by an in-process state machine. The validator side splits naturally
across two phases that map to the two binaries:

```
Phase 1 (open):  EPISODE_API_LIVE → MINER_DEADLINE
Phase 2 (close): DRAND_REVEAL    → SCORING_RUN → ON_CHAIN_ARTIFACTS
```

| Stage | Driven by | What happens |
|-------|-----------|--------------|
| **Episode API live** | `hope-validator-api` | Daemon serves the current epoch's episodes to authenticated miners. Lifetime = ~6.5 days per epoch (`PREDICTION_DEADLINE_HOURS = 156` in `hope/constants.py`). |
| **Miner deadline** | drand schedule | Miners stop accepting their bundles into chain TLE commits at the configured cutoff. |
| **Drand reveal** | drand quicknet | ~60 minutes after each miner's submission, the chain auto-decrypts the TLE'd K. Bundles become parseable in `Commitments::RevealedCommitments`. |
| **Scoring run** | `hope-validator` (one-shot, cron) | Reads chain, fetches archive, runs scoreability, computes scores. Commits `9.C.1` pre-scoring state, `9.C.3` weights, `9.C.2` post-scoring artifacts, and `9.C.6` retry log (when miners are excluded). Idempotent per (validator, epoch). |
| **Consensus** | Yuma | At the next subnet tempo step, the validator's weights influence the network's consensus output and emissions. |

The older in-process state machine
(`IDLE → PREPARING → COMMITTED → ...`) is no longer load-bearing —
state lives on chain, not in validator memory.

---

## 6. Commitment Verification

In the chain-anchored model, commitment verification is performed
**against on-chain artifacts**, not against the validator's
`/verification` HTTP endpoint. The canonical reproducible verifier is
[`scripts/verify_epoch.py`](../scripts/verify_epoch.py); it reads the
validator's `9.C.1`, `9.C.3`, and `9.C.2` commits at chain head (or
at a pinned block hash, against an archive node), re-fetches each
miner's AES_ct from the archive, re-runs the scoring code, and either
confirms or contradicts the validator's claim.

The HTTP `/v1/epochs/{id}/verification` endpoint remains available for
operator-published outcome + salt material and is unchanged from
earlier revisions; it is now a convenience layer over what's already
provable from chain state alone.

See the [Whitepaper](whitepaper.md) §"Trust model" for the full
threat model and what each commit binds.

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
          f"null_pen={score.null_penalty:.4f} "
          f"cov_pen={score.coverage_penalty:.4f} "
          f"covered={score.episodes_scored}/{score.episodes_total} "
          f"final={score.final_score:.4f}")
```

`MinerScore` carries the additional `coverage_penalty`,
`coverage_fraction`, `episodes_scored`, `episodes_total`, and
`episode_scores` fields that the older example omitted.

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

| Variable | Default | Used by | Description |
|----------|---------|---------|-------------|
| `HOPE_API_KEY` | *(required)* | both | Data API key — provided on validator registration |
| `HOPE_API_URL` | *(required)* | both | Data API base URL — provided on validator registration |
| `RELEASE_KEY` | `--release` | both | Epoch ID to serve/score (CLI flag wins if both set) |
| `REQUIRE_SIGNATURES` | `true` | `-api` | Require signed miner requests (set to `false` only for dev) |
| `SN21_SUBTENSOR_URL` | *(unset)* | all | Pin every validator-side chain connection to a wss:// URL (e.g. an archive node you operate). When set, takes precedence over `--network` and applies uniformly across `hope-validator`, `hope-validator-api`, `hope-validator-heartbeat`, the registration-index module, and the diag dump scripts. |
| `SN21_LEADERBOARD_REPORTER` | `0` | scorer | When `1`, POSTs the post-scoring artifact to the CMS after a successful run |
| `SN21_LEADERBOARD_API_KEY` | *(unset)* | scorer | API key for the leaderboard reporter (only used when reporter is enabled) |
| `SN21_EPOCH_ARTIFACT_DIR` | `~/.sn21/epoch_artifacts` | scorer | Where the per-epoch artifact JSON is written before the optional POST |

The miner submission deadline is **156 hours** (~6.5 days), pinned in
`hope/constants.py:PREDICTION_DEADLINE_HOURS` to match the weekly
mining window in `docs/SN21_EPOCH_STRUCTURE.md`. It is not configured
via env var.

The episode-API HTTP port is set via `--port` (default `8080`) on
`hope-validator-api`, not via an env var. `hope-validator` (the scorer)
has no HTTP surface.

### CLI arguments

```
hope-validator-api  (long-running episode HTTP daemon)
  --release KEY              Release key to serve (e.g., WR-2026-W19-PUB-E1).
                             Pass 'auto' (or set RELEASE_KEY=auto in env) to
                             discover the latest published release from the
                             operator data backend at startup — useful for
                             third-party validators who want set-and-forget
                             weekly rotation without manual env-var edits.
  --port PORT                HTTP port (default: 8080)
  --host HOST                Bind host (default: 0.0.0.0)
  --network NETWORK          Bittensor network: 'test', 'finney', 'local',
                             or a wss:// URL (default: finney mainnet)
  --netuid NETUID            Subnet netuid (default: 21 mainnet; 466 testnet)
  --wallet-name NAME
  --wallet-hotkey HOTKEY
  --no-chain                 Skip metagraph load (no auth — dev/local only)

hope-validator  (one-shot post-deadline scoring pass)
  --release KEY              Epoch ID to score
  --api-key KEY              Data API key (or HOPE_API_KEY env var)
  --network NETWORK          (same as above — named or wss:// URL)
  --netuid NETUID
  --wallet-name NAME
  --wallet-hotkey HOTKEY
  --archive-tier-1 URL       Tier-1 archive base URL (repeatable)
  --archive-tier-2 URL       Tier-2 archive base URL (repeatable)
  --ed25519-key-file PATH    PEM private key for inner_sig
  --reg-index-lookback-blocks N   Blocks scanned for miner registrations
                                  (default: 600 ≈ 2h testnet activity)
  --reg-index-prebuilt PATH       Optional prebuilt registration index JSON
  --blocks-until-pre-reveal N     9.C.1 reveal delay (default 300 ≈ 1h)
  --blocks-until-post-reveal N    9.C.2 reveal delay (default 600 ≈ 2h)
  --blocks-until-weights-reveal N  9.C.3 reveal delay (default 360)
```

---

## 10. Production Deployment

### Requirements

- Python 3.10-3.12
- 2GB RAM minimum (episodes are ~15KB each, 300 episodes = ~4.5MB)
- Stable internet for data API access
- Open port for miner HTTP connections

### Recommended setup

Run the episode daemon as a long-lived service, and the scorer as a
post-deadline cron job:

```bash
pip install -e .

# Long-lived episode daemon
nohup hope-validator-api \
    --release WR-2026-W19-PUB-E1 \
    --host 0.0.0.0 --port 8080 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default \
    > validator-api.log 2>&1 &

# One-shot scorer (run from cron Monday morning, after deadline + reveal)
hope-validator \
    --release WR-2026-W19-PUB-E1 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default \
    --archive-tier-2 https://adtao-deploy.onrender.com \
    --ed25519-key-file ~/.sn21/keys/validator-ed25519.pem

tail -f validator-api.log
```

The operator's canonical Render deployment of the cron lives at
`deploy/validator_scoring/` and pulls the latest source from this
repo on every trigger, so production stays in lockstep with `main`.

### Weekly epoch cycle

Each Monday:
1. A new release is available from the operator (e.g., `WR-2026-W19-PUB-E1`)
2. Restart `hope-validator-api` with the new release key
3. Miners have until the weekly deadline (~6.5 days) to submit predictions
4. Drand auto-reveal fires ~60 minutes after each miner's submission
5. Run `hope-validator` (one-shot) after the deadline + reveal window
6. Scoring artifacts (`9.C.1 → 9.C.3 → 9.C.2 → 9.C.6`) land on chain;
   weights take effect at the next Yuma consensus step

### Activity-floor heartbeat (`hope-validator-heartbeat`)

Bittensor's per-subnet `ActivityCutoff` hyperparameter caps how long a
validator can go without submitting `set_weights` before its weights
drop out of consensus computation. SN21's authoritative scoring runs
weekly — well over the cutoff — so a third short-lived binary fills
the gap by **re-asserting whatever weights the latest scoring run
already committed**:

```bash
hope-validator-heartbeat \
    --network finney --netuid 21 \
    --wallet-name my_validator --wallet-hotkey default
```

Run it from a cron at ~3-4h cadence. The binary self-throttles:

- Reads `SubtensorModule.LastUpdate[netuid][validator_uid]`.
- If `current_block - LastUpdate < --threshold-blocks` (default 1500),
  exits with action `skipped_recent_activity` — no submission.
- Otherwise reads `SubtensorModule.Weights[netuid][validator_uid]` and
  re-submits the same `(uids, weights)` tuple via the same
  `set_weights(commit_reveal_version=4)` extrinsic the scoring cron uses.

The heartbeat **cannot** score, cannot fabricate weights, and does not
produce 9.C.* audit records. It can only re-emit what the chain itself
reports for the validator's last revealed weights commit. Auditors
checking that every published epoch has matching 9.C.* records continue
to see exactly one set per week, tied to the Monday scoring run.

`--dry-run` logs what would be submitted without calling `set_weights`
— recommended for the first week of cron operation. Threshold is also
configurable via `SN21_HEARTBEAT_THRESHOLD_BLOCKS` env var.

---

## 11. Troubleshooting

| Issue | Solution |
|-------|----------|
| `--port` rejected by `hope-validator` | You want `hope-validator-api` instead. See §2 — `--port` lives on the HTTP daemon, not the scorer. |
| `no_miner_reveals_visible` (scorer aborts) | Drand auto-reveal hasn't fired yet. Wait ~60 min past each miner's submission and rerun. |
| `already_scored` (scorer aborts) | This (validator, epoch) pair already has a 9.C.1 commit on chain. Use a fresh validator hotkey to retry. |
| `insufficient_budget` (scorer aborts) | The validator hotkey hit the Commitments-pallet byte budget for the current pallet-epoch. Wait for the pallet-epoch to roll (~72 min) or rotate to a fresh hotkey. |
| Miners are excluded as `inner_sig.hotkey_mismatch` | Miner published their ed25519 binding outside the validator's `--reg-index-lookback-blocks` window. Increase the lookback, or supply `--reg-index-prebuilt` from an offline backfill against an archive RPC. |
| `block_hash` lookups failing with "block out of reach" / "State discarded" / pruned-state errors | Your subtensor node is not an archive node (default finney peers retain only the most recent ~256 blocks of state). The registration-index 600-block default lookback will hit this on a non-archive node. Two fixes, pick one: (a) run the diag probe against an archive RPC out of band and pass the resulting JSON via `--reg-index-prebuilt`, while setting `--reg-index-lookback-blocks 0` in the scorer to skip the in-process scan; (b) point the validator at your own archive node by setting `SN21_SUBTENSOR_URL=wss://<your-archive-host>:443` — this now applies to every validator binary uniformly. |
| Miners are excluded as `plaintext_unavailable` | The archive served 404 for their bundle. Either they didn't submit for this epoch, or their self-archive URL is unreachable from the scorer's vantage. |
| Network errors fetching from the data API | Check `HOPE_API_KEY` and `HOPE_API_URL`; verify connectivity from the validator host. |
| Low miner scores | Expected for the baseline model — miners should train their own. |
