# Validator Setup Guide

> **Current system = daily stream (from 4 August 2026).**
> **§2 is the live setup.** Anything about weekly epochs (`WR-…`),
> `hope-miner` submissions, mining deadlines, or `hope-validator --release`
> is **weekly-era history** (concluded 3 August 2026). Those sections stay
> for verifying old epochs; they are not how you run a validator today.
>
> Miner docs: [miner_quickstart.md](./miner_quickstart.md).
> Scoring / rewards: [SN21_SCORING.md](./SN21_SCORING.md),
> [SN21_REWARDS.md](./SN21_REWARDS.md).
> Verifying a day: [SN21_VERIFYING.md](./SN21_VERIFYING.md).


**For:** Running the SN21 validator on the **daily** stream
**Prerequisite:** Python 3.10+, Bittensor wallet (for testnet/mainnet)

> **Note on registration.** Validator registration on SN21 itself is open
> by Bittensor protocol — any operator meeting the chain's permit and
> stake requirements can register and submit weights.
>
> **Scoring, however, is single-operator today, and credentials are not
> what stands in the way.** In the daily stream the operator executes every
> admitted model in a sandbox and seals the predictions into a shadow
> ledger on that host; the scorer settles those predictions. A second
> validator has none, so it cannot reproduce the scoring run whatever API
> keys it holds. Asking for wider key scope will not change this, and we
> would rather say so than have you spend a day on it.
>
> What a third party can do today is **serve the daily feeds** (§2,
> Process A — no credentials at all) and **verify any published day**
> against the signed receipts
> ([SN21_VERIFYING.md](./SN21_VERIFYING.md)) — which is the check that
> actually holds our numbers to account, and it needs nothing from us. If
> a day does not reproduce, the tool names the entry that disagrees.
>
> A formal third-party validator programme — distributed scoring rather
> than distributed verification — is tracked at Review 4.

> **Architecture note (read this first).** For the **daily stream** you run
> **three processes**. Missing any one either fails to score/publish or gets
> you pruned from Yuma consensus.
>
> | Process | Command | Cadence | Who runs it | Role |
> |---|---|---|---|---|
> | **A — HTTP API** | `hope-validator-api` | long-lived | **anyone** | Serves episodes and public `/v1/daily/*` receipts. Takes `--host` / `--port`. Omit `--release` to serve the daily feeds from the ledger with no credentials. |
> | **B — Daily loop** | `python3 scripts/run_daily_loop.py` | once per day | **operator only** | Settles matured (episode × horizon) rows, updates standings, publishes receipt + accuracy, writes weight intent. Not an HTTP server. |
> | **C — Heartbeat** | `hope-validator-heartbeat` | every 3–4 hours | **operator only** | Re-asserts the weights this validator already committed, so `ActivityCutoff` (~16h on mainnet) does not drop them between daily runs. See §10. |
>
> **B and C are operator-only, and not because of credentials.** The daily
> loop settles the predictions produced when each admitted model is executed
> in the operator's sandbox (`run_daily_pipeline.py`, stage 2 — "shadow"),
> and it writes them to a shadow ledger that exists only on that host. A
> second machine has no predictions to settle, so pointing the loop at an
> empty ledger cannot produce a score however it is configured. It fails
> asking for `SN21_PLATFORM_PATH` because the outcomes provider defaults to
> a direct operator-database read; supplying that path — or the HTTP
> outcomes API instead — still leaves nothing to settle.
>
> The heartbeat re-asserts weights read from the chain
> (`SubtensorModule.Weights[netuid][validator_uid]`) — the ones this
> validator committed itself. A validator that has never committed weights
> has nothing for it to re-assert.
>
> **If you are running a validator alongside ours, A is the process you
> want**, plus `scripts/verify_day.py` to check any published day. Scoring
> in the daily stream is single-operator today; see
> [SN21_VERIFYING.md](./SN21_VERIFYING.md) for what that means and how to
> hold the numbers to account without running the scorer.
>
> **Do not confuse with weekly-era tools:**
>
> - `hope-validator --release WR-…` — one-shot **weekly** epoch scorer. Last
>   useful live run was 3 August 2026. Keep it for historical verification only.
> - `hope-validator-daemon` — supervisor whose **scoring** step still calls
>   weekly `hope-validator`. Heartbeat / reg-index ticks remain useful; do
>   **not** treat the daemon as a substitute for `run_daily_loop.py`. See
>   [validator_daemon.md](validator_daemon.md).
>
> If you see `hope-validator --port`, that is a docs mistake: use
> `hope-validator-api --port` for HTTP.

---

## 1. Installation

```bash
git clone <repo-url>
cd SN21-adtao
pip install -e .
```

---

## 2. Quick Start (Local Testing)

**Three** processes: one long-lived HTTP service, one daily run, and one
frequent (3-4 hour) cron.

```bash
# Process A — HTTP service (long-lived)
# Serves episodes to miners, and the daily verifiability endpoints
# (/v1/daily/*) that let anyone reproduce a day's scores.
hope-validator-api \
    --host 0.0.0.0 --port 8080 \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default
```

```bash
# Process B — the daily loop (run once a day from cron)
# Settles the day's matured predictions, folds them into standings,
# publishes the day's receipt + accuracy document, and writes the
# intended weight vector.
python3 scripts/run_daily_loop.py \
    --shadow-root /var/lib/sn21_ledger \
    --ledger-root /var/lib/sn21_ledger
```

```bash
# Process C — activity-floor heartbeat (every 3-4 hours from cron)
hope-validator-heartbeat \
    --network test --netuid 466 \
    --wallet-name my_validator --wallet-hotkey default
```

Roles in one line each:

- **A** (`hope-validator-api`) serves episodes and the daily receipts;
  runs forever.
- **B** (the daily loop) settles, scores, publishes and produces the
  weight intent for one day, then exits. Run it after the day's basket
  has been delivered.
- **C** (`hope-validator-heartbeat`) re-asserts your latest weights every
  few hours so Bittensor's `ActivityCutoff` (~16h on mainnet) does not
  drop you from consensus between runs. Self-throttles via `LastUpdate`
  — safe on a 3-4 hour cron. See §10.4.

**All three are required for sustained operation.** Skipping the heartbeat
means your validator is pruned from emission a day or two after each run,
even if your scoring is flawless.

### What the daily loop needs

| Setting | Purpose |
| :---- | :---- |
| `SN21_ED25519_KEY_FILE` | Signs each day's published receipt and accuracy document. Without it, publication is skipped. |
| `SN21_LEDGER_ROOT` | Where the feeds are written — set it on **Process A** too, so the API serves the same files the loop writes. |
| `SN21_ANCHOR_COMMITS` | When set, the loop commits the feed's rolling Merkle root on chain. Off by default: chain spend is deliberate, never incidental. |

#### Anchoring the feed on chain

With `SN21_ANCHOR_COMMITS` on, the daily loop commits the feed's rolling
Merkle root — 32 bytes, once per published day — from the validator's hotkey.
That root is what `verify_day --expect-anchor` compares against, so a miner
reading it from chain can check any published day, however old.

All of these are read only when the flag is on, and all are required
together. A missing one refuses to anchor and says which: committing the
right root from the wrong identity looks anchored and verifies against
nothing.

| Setting | Value |
| :---- | :---- |
| `SN21_ANCHOR_COMMITS` | `1` / `true` / `yes` / `on` |
| `SN21_WALLET_NAME` | Bittensor wallet holding the validator hotkey |
| `SN21_WALLET_HOTKEY` | Hotkey name (default `default`) |
| `SN21_BT_NETWORK` | `finney` (mainnet) or `test` (testnet) |
| `SN21_NETUID` | `21` mainnet / `466` testnet |

The same root is never committed twice: the loop is idempotent and re-running
it on a day already published would otherwise spend a second write to say the
identical thing. A failed commit is not recorded, so the next run retries.
Test on `test` / `466` before mainnet.

### Timing

Run the daily loop after the day's basket has been delivered. A day's
predictions do not settle immediately — the 7-day horizon finalises 15 days
after the basket's own date, 14-day at 22 days, 28-day at 36 — so the loop is
idempotent by design and simply finds nothing new on a day when nothing has
matured. Re-running it is safe.

### Weekly-era scoring (historical)

`hope-validator --release WR-...` scored a completed weekly epoch and produced
the on-chain `9.C.1 → 9.C.3 → 9.C.2 → 9.C.6` artifact sequence. The last
weekly epoch was scored on 3 August 2026. The command remains for verifying
weekly history; it is not part of daily operation.

For testnet, swap `--network test --netuid 466`; for mainnet use
`--network finney --netuid 21` (the defaults).

---

## 3. Weekly-era: running with miners (historical)

> **Weekly era only** (concluded 3 August 2026). Daily-stream miners ship a
> container; they do **not** call `hope-miner` or POST predictions to your
> API. For live operation see **§2**. Miner onboarding:
> [miner_quickstart.md](./miner_quickstart.md).

### Start the episode API daemon (weekly release key)

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
- Does **not** accept HTTP-posted predictions — production weekly miners
  submitted via on-chain commits (see [Layer 9.B](whitepaper.md)). The
  `POST /v1/epochs/{id}/predictions` endpoint still exists for dev/local
  workflows but is not part of the canonical scoring path.

### Tell miners your endpoint (weekly — obsolete for live miners)

```bash
hope-miner --validator-url <validator-url> --epoch WR-2026-W19-PUB-E1 ...
```

### Score after deadline (weekly `hope-validator`)

Run the one-shot scorer once per weekly epoch, after the miner deadline AND
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

Historical operator cron for this path:
`deploy/validator_scoring/README.md`.

---

## 4. API Endpoints

Once `hope-validator-api` is running, the daemon exposes:

### Daily stream (current) — all public, all reproducible

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/daily/{day}/receipt` | The day's full scoring record: outcomes used, every prediction verbatim, score components, and the formula that ran |
| `GET` | `/v1/daily/{day}/accuracy` | The day's aggregate document; names the receipt it belongs to |
| `GET` | `/v1/daily/{day}/scores` | Aggregate score summary for the day |
| `GET` | `/v1/daily/{day}/miner/{hotkey}` | One miner's entries, components and scores |
| `GET` | `/v1/daily/{day}/proof` | Proof that the day sits inside the anchored root |
| `GET` | `/v1/daily/root` | The current rolling root — what is committed on chain |
| `GET` | `/v1/daily/index` | The feed walk: every day, its hash, and its predecessor |
| `GET` | `/health` | Daemon status |
| `GET` | `/` | Service banner |
| `GET` | `/v1/training/episodes` | Historical training episodes |
| `GET` | `/v1/training/summary` | Training-set composition stats |

Anyone can recompute a day's scores from these; see
[SN21_VERIFYING](./SN21_VERIFYING.md). These endpoints read from
`SN21_LEDGER_ROOT`, so the API process must point at the same directory the
daily loop writes to.

### Weekly-era endpoints (historical)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/v1/epochs/{id}/episodes` | Hotkey | List episode metadata |
| `GET` | `/v1/epochs/{id}/episodes/{ep_id}` | Hotkey | Single episode payload |
| `GET` | `/v1/epochs/{id}/episodes_batch` | Hotkey | All episodes in one request |
| `GET` | `/v1/epochs/{id}/episode-commitment` | None | Per-episode commitment hash |
| `GET` | `/v1/epochs/{id}/commitment` | None | Epoch commitment proof |
| `POST` | `/v1/epochs/{id}/predictions` | Hotkey | Dev/local only (weekly production used on-chain Layer 9.B) |
| `GET` | `/v1/epochs/{id}/verification` | None | Revealed outcomes (post-scoring) |

> `/v1/epochs/{id}/scores` and `/v1/epochs/{id}/my-predictions` no longer
> serve data. They return a pointer to the daily equivalents above.

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

## 5. Daily lifecycle (current)

| Stage | Driven by | What happens |
|-------|-----------|--------------|
| **Basket delivered** | Operator | Morning basket `BD-*` of the previous day's qualifying account changes |
| **Container runs** | Operator sandbox | Admitted miner images run against the basket; predictions recorded |
| **Settle / publish** | `scripts/run_daily_loop.py` (daily cron) | Matured (episode × horizon) rows fold into standings; receipt + accuracy published; weight intent written |
| **Optional chain anchor** | Daily loop + `SN21_ANCHOR_COMMITS` | Rolling Merkle root of the receipt feed committed on chain |
| **Activity floor** | `hope-validator-heartbeat` | Re-asserts last weights so `ActivityCutoff` does not prune the validator |
| **Consensus** | Yuma | Weights influence emissions at the next subnet tempo step |

Horizon settle lag (from basket date): 7-day → +15 days, 14-day → +22 days,
28-day → +36 days. The daily loop is idempotent — safe to re-run when nothing
has matured.

Authoritative scoring detail: [SN21_SCORING.md](./SN21_SCORING.md).
Public verification: [SN21_VERIFYING.md](./SN21_VERIFYING.md) /
[`scripts/verify_day.py`](../scripts/verify_day.py).

### 5.1 Weekly-era epoch lifecycle (historical)

> Concluded 3 August 2026. Retained for verifying weekly history with
> [`scripts/verify_epoch.py`](../scripts/verify_epoch.py).

The weekly protocol's epoch lifecycle was **chain-anchored** rather than driven
by an in-process state machine:

```
Phase 1 (open):  EPISODE_API_LIVE → MINER_DEADLINE
Phase 2 (close): DRAND_REVEAL    → SCORING_RUN → ON_CHAIN_ARTIFACTS
```

| Stage | Driven by | What happens |
|-------|-----------|--------------|
| **Episode API live** | `hope-validator-api` | Served the current weekly epoch's episodes (~6.5 days; `PREDICTION_DEADLINE_HOURS = 156`). |
| **Miner deadline** | drand schedule | Miners stopped submitting TLE commits at the cutoff. |
| **Drand reveal** | drand quicknet | ~60 minutes after each submission, chain auto-decrypted TLE'd K. |
| **Scoring run** | `hope-validator` (one-shot, cron) | Scoreability + `9.C.1 → 9.C.3 → 9.C.2 → 9.C.6` commits. Idempotent per (validator, epoch). |
| **Consensus** | Yuma | Weights at the next tempo step. |

---

## 6. Commitment Verification

### Daily stream (current)

Each scored day is published as a signed receipt (hash-chained, covered by a
rolling Merkle root). Anyone recomputes scores with
[`scripts/verify_day.py`](../scripts/verify_day.py). When
`SN21_ANCHOR_COMMITS` is on, `--expect-anchor` checks the root against chain.
Full walkthrough: [SN21_VERIFYING.md](./SN21_VERIFYING.md).

### Weekly era (historical)

Weekly commitment verification is performed **against on-chain artifacts**,
not against the validator's `/verification` HTTP endpoint. The canonical
reproducible verifier is
[`scripts/verify_epoch.py`](../scripts/verify_epoch.py); it reads the
validator's `9.C.1`, `9.C.3`, and `9.C.2` commits at chain head (or
at a pinned block hash, against an archive node), re-fetches each
miner's AES_ct from the archive, re-runs the scoring code, and either
confirms or contradicts the validator's claim.

The HTTP `/v1/epochs/{id}/verification` endpoint remains available for
operator-published outcome + salt material; it is a convenience layer over
what's already provable from chain state alone.

See the [Whitepaper](whitepaper.md) §"Trust model" for the full
threat model and what each commit binds.

---

## 7. Data API — **operator only**

> **You do not need this.** This is how the operator's own pipeline collects
> the day's basket. It is not part of running a second validator, and a key
> for it is not something to ask for: nothing a second validator does reads
> these endpoints.
>
> `--release auto` is the one place this leaks. It resolves against the
> WEEKLY release listing below, skipping `BD-YYYY-MM-DD` daily entries
> entirely, so it cannot resolve at all now the weekly stream has wound
> down. Omit `--release` (§2) and the daily feeds serve from the ledger with
> no credentials.
>
> Everything a second validator needs is public and signed — the receipt,
> accuracy and proof feeds under `/v1/daily/*`. See
> [SN21_VERIFYING.md](./SN21_VERIFYING.md).

The operator's pipeline fetches releases / packages from this API. The
weekly `WR-…` example below is historical packaging shape.

| Endpoint | Purpose |
|----------|---------|
| `GET {base}/internal/bittensor/v1/releases` | List available releases (weekly `WR-`; daily `BD-` entries appear here but nothing submits to them) |
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

**Daily stream (current):** settle logic, standings, and the weight curve are
documented in [SN21_SCORING.md](./SN21_SCORING.md) and
[SN21_REWARDS.md](./SN21_REWARDS.md). The live process is
`scripts/run_daily_loop.py` (§2), not the weekly `EpochScorer` path below.

### Weekly-era library example (historical)

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

### Scoring component weights (shared formula)

| Component | Weight | Range |
|-----------|--------|-------|
| Quantile Accuracy | 0.50 | 0.45-0.55 |
| Calibration | 0.20 | 0.15-0.25 |
| Directional | 0.15 | 0.10-0.20 |
| Goal Accuracy | 0.15 | 0.10-0.20 |

Weights must sum to 1.0 and stay within published ranges.

---

## 9. Configuration

### Environment variables — daily stream (current)

| Variable | Default | Used by | Description |
|----------|---------|---------|-------------|
| `HOPE_API_KEY` | *(required)* | API / data client | Data API key — provided on validator registration |
| `HOPE_API_URL` | *(required)* | API / data client | Data API base URL — provided on validator registration |
| `SN21_LEDGER_ROOT` | *(required for receipts)* | API + daily loop | Directory for published daily feeds — **same path on Process A and B** |
| `SN21_ED25519_KEY_FILE` | *(required to publish)* | daily loop | Signs each day's receipt and accuracy document |
| `SN21_ANCHOR_COMMITS` | off | daily loop | When set, commit the feed's rolling Merkle root on chain |
| `SN21_WALLET_NAME` | — | daily loop (anchor) | Wallet for chain anchor commits |
| `SN21_WALLET_HOTKEY` | `default` | daily loop (anchor) | Hotkey name |
| `SN21_BT_NETWORK` | — | daily loop (anchor) | `finney` or `test` |
| `SN21_NETUID` | — | daily loop (anchor) | `21` mainnet / `466` testnet |
| `REQUIRE_SIGNATURES` | `true` | `-api` | Require signed miner requests (set to `false` only for dev) |
| `SN21_SUBTENSOR_URL` | *(unset)* | all | Pin chain RPC to a wss:// URL (e.g. archive node). Overrides `--network` for validator binaries and related scripts. |
| `SN21_HEARTBEAT_THRESHOLD_BLOCKS` | `1500` | heartbeat | Skip re-assert if last update is fresher than this |

The episode-API HTTP port is set via `--port` (default `8080`) on
`hope-validator-api`, not via an env var. The daily loop is not an HTTP
server.

### Environment variables — weekly-era scorer (historical)

| Variable | Default | Used by | Description |
|----------|---------|---------|-------------|
| `RELEASE_KEY` | `--release` | weekly API/scorer | Epoch ID to serve/score (CLI flag wins if both set) |
| `SN21_LEADERBOARD_REPORTER` | `0` | weekly scorer | When `1`, POSTs the post-scoring artifact to the CMS |
| `SN21_LEADERBOARD_API_KEY` | *(unset)* | weekly scorer | API key for the leaderboard reporter |
| `SN21_EPOCH_ARTIFACT_DIR` | `~/.sn21/epoch_artifacts` | weekly scorer | Where the per-epoch artifact JSON is written |

The weekly miner submission deadline was **156 hours** (~6.5 days), pinned in
`hope/constants.py:PREDICTION_DEADLINE_HOURS` (see
`docs/archive/weekly/SN21_EPOCH_STRUCTURE.md`). Not used by the daily loop.
`hope-validator` (weekly scorer) has no HTTP surface.

### CLI arguments

```
hope-validator-api  (long-running HTTP daemon — daily + weekly surfaces)
  --port PORT                HTTP port (default: 8080)
  --host HOST                Bind host (default: 0.0.0.0)
  --network NETWORK          Bittensor network: 'test', 'finney', 'local',
                             or a wss:// URL (default: finney mainnet)
  --netuid NETUID            Subnet netuid (default: 21 mainnet; 466 testnet)
  --wallet-name NAME
  --wallet-hotkey HOTKEY
  --no-chain                 Skip metagraph load (no auth — dev/local only)
  --release KEY              Weekly-era: release key to serve (WR-…).
                             OMIT IT for the daily stream: with no release the
                             API serves the /v1/daily feeds straight from
                             SN21_LEDGER_ROOT, and needs no data-API key, no
                             release and no chain read.
                             'auto' discovers the latest published WEEKLY
                             release; it cannot resolve now that the weekly
                             stream has wound down, and is not fatal — the API
                             logs the failure and serves the daily feeds.

scripts/run_daily_loop.py  (daily stream scorer — current, OPERATOR ONLY)
  --shadow-root PATH         Shadow / settle state root. Holds the predictions
                             sealed when each admitted model was executed in
                             the operator's sandbox; only that host has one.
  --ledger-root PATH         Where receipt + accuracy feeds are written
                             (must match SN21_LEDGER_ROOT on the API)
  Outcomes come from the operator database by default (needs
  SN21_PLATFORM_PATH), or over HTTP with SN21_OUTCOMES_API_URL +
  SN21_OUTCOMES_API_KEY. Neither makes this runnable elsewhere: without the
  shadow ledger above there is nothing to settle.

hope-validator  (weekly-era one-shot scorer — historical only)
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
- 2GB RAM minimum (episodes are ~15KB each; daily baskets are smaller than weekly packs)
- Stable internet. Data API access is needed only by the operator's own
  pipeline (§7); serving the daily feeds needs no credentials
- Open port if you expose `hope-validator-api` publicly
- Persistent disk for `SN21_LEDGER_ROOT` (receipt feeds)

### Recommended setup (daily stream — current)

```bash
pip install -e .

export SN21_LEDGER_ROOT=/var/lib/sn21_ledger
export SN21_ED25519_KEY_FILE=~/.sn21/keys/validator.pem

# Process A — long-lived HTTP (episodes + /v1/daily/*)
nohup hope-validator-api \
    --host 0.0.0.0 --port 8080 \
    --network finney --netuid 21 \
    --wallet-name my_validator --wallet-hotkey default \
    > validator-api.log 2>&1 &

# Process B — daily loop (cron once per day after basket delivery)
python3 scripts/run_daily_loop.py \
    --shadow-root /var/lib/sn21_ledger \
    --ledger-root /var/lib/sn21_ledger

# Process C — heartbeat (cron every 3–4 hours)
hope-validator-heartbeat \
    --network finney --netuid 21 \
    --wallet-name my_validator --wallet-hotkey default
```

See §2 for ledger/anchor env details and settle timing.

### Daily cycle

Every day:
1. A basket is delivered in the morning, named for the previous day's changes
   (`BD-2026-08-03` holds Monday 3 August's changes, delivered Tuesday 4th)
2. Admitted miner containers are run against it; their predictions are recorded
3. The daily loop settles whatever matured that day — 7-day results finalise
   15 days after the basket's own date, 14-day at 22, 28-day at 36
4. The day's receipt and accuracy document are published and hash-chained to
   the day before
5. The feed's rolling Merkle root is committed on chain when
   `SN21_ANCHOR_COMMITS` is set, so any published day stays verifiable
6. Standings update; the weight vector follows at the next consensus step

### Serving the daily feeds

No release, no data-API key, no chain read. The `/v1/daily/*` routes are read
straight from the ledger the daily loop writes to, so the only thing this needs
is that path:

```bash
export SN21_LEDGER_ROOT=/var/data/sn21/ledger
hope-validator-api --port 8080
```

Passing `--release auto` here asks for a WEEKLY release. That lookup cannot
resolve now the weekly stream has wound down; the API logs it and serves the
daily feeds anyway, so the flag is unnecessary rather than harmful.

**Verifying does not need this server at all.** The published feeds are
mirrored and public — see [SN21_VERIFYING.md](./SN21_VERIFYING.md), which is
authoritative for the daily stream:

```bash
python scripts/verify_day.py --url https://hope-bittensor-api.onrender.com --day 2026-08-29
```

### Weekly-era production cron (historical — concluded 3 August 2026)

```bash
# Long-lived episode daemon (weekly release key)
hope-validator-api --release WR-2026-W19-PUB-E1 ...

# One-shot scorer (Monday morning after deadline + reveal)
hope-validator --release WR-2026-W19-PUB-E1 ...
```

Reference deployment for that historical cron:
`deploy/validator_scoring/`. Each Monday a release was published, miners had
~6.5 days to submit, drand auto-reveal fired ~60 minutes after each
submission, and a one-shot scoring pass produced the
`9.C.1 → 9.C.3 → 9.C.2 → 9.C.6` artifacts on chain.

### Activity-floor heartbeat (`hope-validator-heartbeat`) — **operator only**

Bittensor's per-subnet `ActivityCutoff` hyperparameter caps how long a
validator can go without submitting `set_weights` before its weights
drop out of consensus computation. Even with a **daily** settle loop, gaps
between weight updates can exceed the cutoff — so a third short-lived binary
fills the gap by **re-asserting whatever weights the latest scoring run
already committed**.

It reads those weights back from the chain
(`SubtensorModule.Weights[netuid][validator_uid]`) — the ones *this*
validator committed. A validator that has never committed weights has
nothing for it to re-assert, so this is only useful on a host that scores.

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
  re-submits the same `(uids, weights)` tuple via
  `set_weights(commit_reveal_version=4)`.

The heartbeat **cannot** score, cannot fabricate weights, and does not
produce daily receipts or weekly 9.C.* audit records. It can only re-emit
what the chain itself reports for the validator's last revealed weights
commit.

`--dry-run` logs what would be submitted without calling `set_weights`
— recommended for the first days of cron operation. Threshold is also
configurable via `SN21_HEARTBEAT_THRESHOLD_BLOCKS` env var.

---

## 11. Troubleshooting

### Daily stream (current)

| Issue | Solution |
|-------|----------|
| `/v1/daily/*` empty or 404 | Set `SN21_LEDGER_ROOT` on **both** the API and the daily loop to the same directory; confirm the loop has published at least one day. |
| Daily loop skips publication | Set `SN21_ED25519_KEY_FILE`; without it, signing (and publication) is skipped. |
| Anchor commit refused | Enable `SN21_ANCHOR_COMMITS` and set `SN21_WALLET_*`, `SN21_BT_NETWORK`, `SN21_NETUID` together — a missing one refuses rather than anchoring from the wrong identity. |
| `--port` rejected by `hope-validator` | You want `hope-validator-api`. The daily scorer is `scripts/run_daily_loop.py`, not `hope-validator`. |
| Validator pruned from consensus | Run `hope-validator-heartbeat` every 3–4 hours (§10). |
| Network errors fetching from the data API | Check `HOPE_API_KEY` and `HOPE_API_URL`; verify connectivity from the validator host. |
| Low miner scores | Expected for the baseline model — miners should train their own. |

### Weekly-era scorer (historical)

| Issue | Solution |
|-------|----------|
| `no_miner_reveals_visible` (scorer aborts) | Drand auto-reveal hasn't fired yet. Wait ~60 min past each miner's submission and rerun. |
| `already_scored` (scorer aborts) | This (validator, epoch) pair already has a 9.C.1 commit on chain. Use a fresh validator hotkey to retry. |
| `insufficient_budget` (scorer aborts) | The validator hotkey hit the Commitments-pallet byte budget for the current pallet-epoch. Wait for the pallet-epoch to roll (~72 min) or rotate to a fresh hotkey. |
| Miners are excluded as `inner_sig.hotkey_mismatch` | Miner published their ed25519 binding outside the validator's `--reg-index-lookback-blocks` window. Increase the lookback, or supply `--reg-index-prebuilt` from an offline backfill against an archive RPC. |
| `block_hash` lookups failing with "block out of reach" / "State discarded" / pruned-state errors | Your subtensor node is not an archive node. Fix: (a) `--reg-index-prebuilt` + `--reg-index-lookback-blocks 0`, or (b) `SN21_SUBTENSOR_URL=wss://<archive>:443`. |
| Miners are excluded as `plaintext_unavailable` | The archive served 404 for their bundle — missing submission or unreachable self-archive URL. |
