> **Weekly-era / historical only.** The Monday scoring timer here predates the
> daily stream. Live validators use `scripts/run_daily_loop.py` — see
> [docs/validator_setup.md §2](../../docs/validator_setup.md#2-quick-start-local-testing)
> and [docs/SN21_TRANSITION_PLAN.md](../../docs/SN21_TRANSITION_PLAN.md).
> Do not schedule this weekly runner for daily-stream operation.

# SN21 Scoring Runner — Deployment (weekly era)

`hope-validator` (from `hope.validator.runner:main`) is the **one-shot per-epoch scoring process** for the weekly-era protocol. It reads each miner's on-chain commits, fetches the AES-encrypted predictions from the three-tier archive, runs the 8-check scoreability rule, scores the predictions, and submits the four Layer 9.C chain commits (`9.C.1` pre-scoring state, `9.C.3` weights, `9.C.2` post-scoring artifacts, `9.C.6` retry log if any miners were excluded for `plaintext_unavailable`).

This directory holds **reference deployment artifacts** any operator running canonical scoring can use. All operator-specific values come from environment variables — no hostnames, wallet names, or API keys are hardcoded.

## Role boundaries

| Component | Service shape | Where |
|---|---|---|
| Episode-serving HTTP API | Long-running (`hope-validator-api`) | `hope/validator/serve.py` |
| Tier-2 archive | Long-running (`hope-archive-server`) | `deploy/archive_server/` |
| **Scoring runner** | **One-shot per epoch (`hope-validator`)** | **`deploy/validator_scoring/` (this directory)** |

The three are independent processes. The scoring runner does **not** share state with the HTTP API — every output it produces is anchored on chain via timelocked commits, and downstream consumers (verifiers, leaderboards, the on-chain HTTP `/verification` endpoint when it's re-plumbed to read chain) pull from there.

## Cadence

From [`hope/constants.py`](../../hope/constants.py):

```
Mining open:   Monday 17:00 UTC → next Monday 05:00 UTC
Scoring open:  Monday 05:00 UTC → Monday 17:00 UTC  (~12 hours)
```

Schedule the runner to fire **once per week, between Monday 05:00 UTC and Monday 17:00 UTC**. The bundled systemd timer fires at Monday 12:00 UTC (mid-window).

Re-running mid-window is safe — the runner checks `validator_already_scored_epoch` on chain (`hope/validator/onchain_runner.py`) and bails with `aborted_reason="already_scored: ..."` if a `9.C.1` already landed for `(validator_hotkey, epoch_id)`. The Commitments-pallet byte budget is preserved.

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `HOPE_API_KEY` | yes | Operator data API key (release discovery + episode/outcome fetch) |
| `WALLET_NAME` | yes | Bittensor wallet name |
| `HOTKEY_NAME` | yes | Bittensor hotkey name |
| `BT_NETWORK` | yes | `finney` (mainnet) or `test` (testnet) |
| `NETUID` | yes | `21` (mainnet) or `466` (testnet) |
| `ED25519_KEY_FILE` | yes | Path to the validator's ed25519 PEM (used for `inner_sig` on chain commits) |
| `RELEASE_KEY` | no | If unset, the wrapper discovers the latest release from `HOPE_API_URL`. Set explicitly to pin a specific epoch. |
| `HOPE_API_URL` | no | Operator data backend base URL. Default placeholder lives in `run.sh`; override with the operator's real URL. |
| `ARCHIVE_TIER_1` | no | Space-separated list of Tier-1 (validator local cache) URLs |
| `ARCHIVE_TIER_2` | no | Space-separated list of Tier-2 (operator shadow) URLs |
| `BLOCKS_UNTIL_PRE_REVEAL` | no | Default `300` |
| `BLOCKS_UNTIL_POST_REVEAL` | no | Default `600` |
| `BLOCKS_UNTIL_WEIGHTS_REVEAL` | no | Default `360` |

The wrapper validates required vars and exits non-zero if anything is missing. Required-file checks (`ED25519_KEY_FILE` readable) happen before any chain or network I/O so misconfiguration fails fast.

## Persistent state

Two paths must survive between runs. Neither contains anything that should live in a public repo.

| Path | What | Notes |
|---|---|---|
| `~/.bittensor/wallets/<wallet>/` | Bittensor coldkey + hotkey | Generated once via `btcli wallet new_coldkey` / `new_hotkey`. The scoring runner only needs read access during the run + sign access on the hotkey. |
| `${ED25519_KEY_FILE}` (e.g. `/etc/sn21/keys/validator.pem`) | Validator's ed25519 inner_sig private key | Generated once via `python scripts/sn21_keys.py generate --role validator --output …`. Registered on-chain once via `scripts/sn21_keys.py register`. |

On Render: mount a Persistent Disk at `/home/sn21/.bittensor`. On bare-metal / VM: just keep the host directory between runs. On Kubernetes: a PVC.

## Deploy via systemd (bare-metal / VM)

```bash
# 1. Install the package on the host.
git clone https://github.com/ippcteam/SN21-adtao.git /opt/sn21
cd /opt/sn21
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Create the runtime user + directories.
sudo useradd -r -m -d /home/sn21 -s /usr/sbin/nologin sn21
sudo mkdir -p /etc/sn21/keys
sudo chown -R sn21:sn21 /home/sn21 /etc/sn21

# 3. Generate keys / wallet AS THE sn21 USER (one-time).
sudo -u sn21 btcli wallet new_coldkey --wallet.name <wallet_name>
sudo -u sn21 btcli wallet new_hotkey --wallet.name <wallet_name> --wallet.hotkey <hotkey_name>
sudo -u sn21 python /opt/sn21/scripts/sn21_keys.py generate \
    --role validator --output /etc/sn21/keys/validator.pem
sudo -u sn21 python /opt/sn21/scripts/sn21_keys.py register \
    --role validator \
    --network <test|finney> --netuid <466|21> \
    --wallet-name <wallet_name> --wallet-hotkey <hotkey_name> \
    --key /etc/sn21/keys/validator.pem

# 4. Drop secrets into an EnvironmentFile (the systemd unit reads this).
sudo install -m 600 -o sn21 -g sn21 /dev/null /etc/sn21/scoring.env
sudo tee /etc/sn21/scoring.env > /dev/null <<'EOF'
HOPE_API_KEY=<operator-issued>
HOPE_API_URL=https://<operator-backend>
WALLET_NAME=<wallet_name>
HOTKEY_NAME=<hotkey_name>
BT_NETWORK=<test|finney>
NETUID=<466|21>
ED25519_KEY_FILE=/etc/sn21/keys/validator.pem
ARCHIVE_TIER_2=https://<operator-shadow-archive>
EOF

# 5. Install the systemd unit + timer.
sudo cp deploy/validator_scoring/sn21-validator-scoring.service /etc/systemd/system/
sudo cp deploy/validator_scoring/sn21-validator-scoring.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sn21-validator-scoring.timer

# 6. Verify.
systemctl list-timers sn21-validator-scoring.timer
systemctl status sn21-validator-scoring.timer
journalctl -u sn21-validator-scoring.service -n 200 --no-pager
```

A test fire without waiting for Monday: `sudo systemctl start sn21-validator-scoring.service`.

## Deploy via Docker

The bundled `Dockerfile` builds a single-purpose image that runs once per invocation and exits.

```bash
# Build from the repo root.
docker build -t sn21-validator-scoring:latest \
    -f deploy/validator_scoring/Dockerfile .

# Run one scoring pass. Mount the wallet directory + ed25519 key so they
# persist across invocations.
docker run --rm \
    -e HOPE_API_KEY=<operator-issued> \
    -e HOPE_API_URL=https://<operator-backend> \
    -e WALLET_NAME=<wallet_name> \
    -e HOTKEY_NAME=<hotkey_name> \
    -e BT_NETWORK=<test|finney> \
    -e NETUID=<466|21> \
    -e ED25519_KEY_FILE=/etc/sn21/keys/validator.pem \
    -e ARCHIVE_TIER_2=https://<operator-shadow-archive> \
    -v /var/lib/sn21/bittensor:/home/sn21/.bittensor \
    -v /var/lib/sn21/keys:/etc/sn21/keys:ro \
    sn21-validator-scoring:latest
```

Schedule via host cron, Docker Compose with a sidecar scheduler, or Kubernetes `CronJob`.

## Deploy via a managed cron service (Render Cron Job, etc.)

Any managed cron-style service that can run a container or a shell command on a schedule works. Reference shape:

| Setting | Value |
|---|---|
| Schedule | `0 12 * * 1` (Monday 12:00 UTC) |
| Build | `pip install -e .` |
| Start | `bash deploy/validator_scoring/run.sh` |
| Persistent disk mount | `/home/<runtime-user>/.bittensor` (Bittensor wallet) |
| Secret file | `${ED25519_KEY_FILE}` (validator ed25519 PEM) |
| Env vars | All required vars from the table above |

The exact field names depend on the provider — the requirement is "a persistent volume for the wallet directory, a way to inject the ed25519 PEM, and the env vars."

## Operational notes

- **First run on a new wallet.** The runner's pre-flight (`hope/validator/onchain_runner.py`) verifies the validator has enough Commitments-pallet byte budget for the four 9.C commits. If the wallet is freshly minted with no prior commits, this is automatic. If the wallet has been used for other things, you may see `aborted_reason="insufficient_budget: ..."` until the pallet-epoch advances.
- **Stale RPCs.** Bittensor RPC nodes occasionally lag the chain head. If a run aborts with `no_miner_reveals_visible`, retry the timer; subsequent fires usually land on a synced backend. The runner's bail-out is byte-budget-preserving — no commits land on a stale read.
- **First-line bittensor log duplication.** A cosmetic issue (the first `Enabling default logging` line appears twice during the bittensor import). Subsequent log lines are deduped correctly. Doesn't affect correctness.
- **Logs.** `run.sh` emits the discovered release + key invocation details on stdout, then `exec`s into `hope-validator`. Capture stdout/stderr via the scheduler (`journalctl -u sn21-validator-scoring.service` for systemd, the platform's log viewer otherwise).

## Verifying a run landed correctly

After a successful run, four chain commits should be visible for `(validator_hotkey, epoch_id)`:

```bash
# Quick chain-integrity check (replace placeholders).
python scripts/verify_epoch.py \
    --epoch-id <release-key> \
    --validator-hotkey <validator-ss58> \
    --tier-2-base https://<operator-shadow-archive>
```

Expected output: `OK: True` plus per-root match details. Full score recomputation additionally needs `--truth-file` derived from the operator's 9.A.2 reveal blob.

## Security

- `run.sh` validates that required env vars are set before touching the network or chain.
- Wallets + ed25519 private keys are mounted read-only where possible.
- The systemd unit hardens the runtime (`ProtectSystem=strict`, `ReadWritePaths=/home/sn21/.bittensor`, system-call filter).
- No logs ever print the API key, wallet password, or private-key material — only the discovered `RELEASE_KEY`, network, netuid, wallet name, and archive endpoints.

## Related

- [`deploy/archive_server/`](../archive_server/) — Tier-2 / Tier-3 archive deployment (the scoring runner reads from these).
- [`hope/validator/runner.py`](../../hope/validator/runner.py) — `hope-validator` CLI source.
- [`hope/validator/onchain_runner.py`](../../hope/validator/onchain_runner.py) — Layer 9.C orchestration.
- [`scripts/verify_epoch.py`](../../scripts/verify_epoch.py) — third-party verification of any past epoch.
- [`docs/SN21_SCORING.md`](../../docs/SN21_SCORING.md) · [`docs/SN21_REWARDS.md`](../../docs/SN21_REWARDS.md) — daily-stream scoring / emissions (authoritative).
- [`docs/archive/weekly/`](../../docs/archive/weekly/) — archived weekly reward / epoch specs (historical only).
