# `hope-validator-daemon` — the consolidated validator

One long-running process that replaces the **scoring cron + heartbeat cron +
the manual registration-index step** with a single supervisor loop. Each tick
it runs three *self-idempotent* tools as isolated subprocesses:

| tick step | tool | what it does | idempotency |
|---|---|---|---|
| reg-index | `build_reg_index --once` | extends the registration index from its persisted checkpoint (archive RPC) | self-checkpointing; scans only new blocks |
| scoring | `hope-validator --release auto` | resolves the latest **closed** epoch and scores it | on-chain `already_scored` guard → scores each epoch once |
| weights | `hope-validator-heartbeat` | re-asserts the validator's last on-chain weights | self-throttles on the ≤1500-block gap |

> The **episode API** (`hope-validator-api`, the miner-facing HTTP server) is a
> separate long-running service and is **not** part of the daemon — keep running
> it. The daemon consolidates only the scoring + heartbeat + reg-index work.

## Why a supervisor (not a monolith)

Running the three proven tools as subprocesses keeps each run's
bittensor/substrate state isolated (the RSS-leak mitigation), contains failures
(one tool failing never blocks the others), and keeps the daemon itself tiny and
auditable — it holds **no scoring or weights state of its own**. This is the
"one long-running script" that lets others verify exactly what validators run.

## Run it

```bash
hope-validator-daemon \
  --network finney --netuid 21 \
  --wallet-name <validator-wallet> --wallet-hotkey <hotkey> \
  --reg-index /var/data/sn21-validator/sn21-reg-index.json \
  --reg-index-archive-url wss://archive.chain.opentensor.ai:443 \
  --reg-index-cold-start-lookback-blocks 20 \
  --interval-seconds 1800
```

Keep `--reg-index` (and its `.state.json` checkpoint sidecar) on a **persistent
disk** so the rolling scan resumes cheaply across restarts. The reg-index path
is also passed to the scorer as `--reg-index-prebuilt` automatically.

### Key flags / env

| flag | env | meaning |
|---|---|---|
| `--reg-index` | `SN21_REG_INDEX_PATH` | reg-index JSON path (persistent disk) |
| `--reg-index-archive-url` | `SN21_REG_INDEX_ARCHIVE_URL` | **archive** RPC for the reg-index scan — see the note below; **required** |
| `--reg-index-cold-start-lookback-blocks` | `SN21_REG_INDEX_COLD_START_LOOKBACK_BLOCKS` | bounds the first scan when there's no checkpoint (avoid a multi-hour cold start) |
| `--reg-index-max-blocks-per-tick` | `SN21_REG_INDEX_MAX_BLOCKS_PER_TICK` | cap each tick's scan to N blocks so a slow archive can never block the heartbeat (catches up over ticks). On the public archive `200` keeps a tick short |
| `--ed25519-key-file` | `SN21_ED25519_KEY_FILE` | the validator's ed25519 key for the scorer's 9.C inner-sig (chain hotkey is sr25519) |
| `--archive-tier-2` (repeatable) | `ARCHIVE_TIER_2_URLS` (space/comma-sep) | tier-2 ct archive(s) the scorer fetches miner AES_ct from |
| `--interval-seconds` | `SN21_DAEMON_INTERVAL_SECS` | seconds between ticks (300 recommended so the bounded reg-index keeps up + the heartbeat runs frequently) |
| `--heartbeat-dry-run` | `SN21_HEARTBEAT_DRY_RUN=1` | heartbeat logs its decision but commits nothing |
| `--skip-scoring` / `--skip-heartbeat` / `--skip-reg-index` | — | drop a tool from the tick |

> **`SN21_REG_INDEX_ARCHIVE_URL` must point at a true archive node** (full history,
> e.g. `wss://archive.chain.opentensor.ai:443` or your own archival node). A
> pruned/standard RPC keeps only ~256 blocks of state, so historical
> `CommitmentOf` reads fail with `State discarded … block is too old … use an
> archive node` and the reg-index reads **zero** blocks. The per-tick bound +
> `300s` interval keep the heartbeat safe even on the slow public archive; for
> steady-state speed, point this at your own archival node.
| `--once` | — | run one tick and exit (manual run / smoke test) |

`HOPE_API_KEY` / `HOPE_API_URL` are required (the scorer resolves `--release
auto` and fetches episodes/outcomes from the operator data backend). The
daemon shuts down cleanly on SIGTERM (finishes the current tick).

## Safety properties

- **Scoring never double-commits or re-scores**: `--release auto` resolves the
  latest *closed* epoch, and the on-chain `already_scored` guard makes repeat
  runs no-ops. The only rule: **do not run the daemon and the old scoring/
  heartbeat crons at the same time** (both would set weights).
- **Missed scoring window** → the heartbeat keeps re-asserting the last weights
  ("pull the last window"), so the validator stays above the activity cutoff.
- **Observable**: all subprocess logs are visible (the daemon restores logging
  that `import bittensor` otherwise suppresses).

## Migrating from the 3-binary setup

Replace the `hope-validator` scoring cron + the `hope-validator-heartbeat` cron
with one `hope-validator-daemon` worker (web/worker service with a persistent
disk). Roll out safely:

1. Deploy the daemon in **observe mode** (`--skip-scoring --heartbeat-dry-run`)
   *alongside* the existing crons. It only builds the reg-index + logs the
   heartbeat decision — no commits, no conflict.
2. Confirm the daemon's tick logs look right.
3. **Flip to live** (drop `--skip-scoring --heartbeat-dry-run`) **and remove the
   two crons in the same change**. Never run both committing at once.
