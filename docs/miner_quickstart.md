# SN21 — Miner Quickstart (daily stream)

**For:** Miners joining SN21 on Bittensor  
**Subnet:** SN21 (testnet netuid **466** / mainnet netuid **21**)  
**Validator URL:** https://validator.adtao.io  
**Model contract:** [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)  
**Rules:** [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_STAKING.md](./SN21_STAKING.md) · [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)

Example commands target **testnet 466**. For mainnet, swap `test` → `finney` and `466` → `21`.

> **Weekly `hope-miner` prediction commits are obsolete.** You no longer submit a sealed prediction bundle each week. You ship a **container image**; the subnet runs it against each **daily basket**. Cutover dates: [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md).

---

## 1. What you're doing

Each day the subnet reveals a **basket** of real Google Ads account changes (episodes). Your admitted **model container** is executed in a sandbox against that basket. Outputs are locked as your predictions **before** outcomes exist.

Later, each prediction is scored once at **7 / 14 / 28 days** (+ settle window). Scores feed a moving-average **standing**; emissions follow a published **weight curve**. See scoring & rewards docs above.

You output **probabilistic distributions** (P10/P50/P90), not point estimates. Domain knowledge helps; the core task is calibrated prediction.

---

## 2. Register on the subnet (still required)

You **must** register on-chain to participate and earn emissions. Registration burns a small amount of TAO.

### Step 1: Install tooling

```bash
git clone https://github.com/ippcteam/SN21-adtao.git
cd SN21-adtao
pip install -e ".[miner]"
```

You also need **Docker** to build and test your model image locally.

### Step 2: Create wallet and register

```bash
# Create a coldkey (the keys-of-keys for your wallet)
btcli wallet new_coldkey --wallet.name my_miner
```

`btcli` will prompt you for three things — what to enter:

| Prompt | What to enter |
|---|---|
| `Enter the path to the wallets directory:` | Press **Enter** to accept the default `~/.bittensor/wallets/` |
| `Choose the number of words [12/15/18/21/24]:` | Type `12` and Enter (12 is the default; gives 128 bits of entropy) |
| `Specify password for key encryption:` | Pick a strong password and **save it in your password manager**. You'll need it for every chain transaction. |
| `Retype your password:` | Same password again |

**Scripting / CI tip:** to avoid the wallet-path prompt entirely, pass
`--wallet-path ~/.bittensor/wallets` explicitly on every `btcli wallet`
and `btcli subnet` command. The default works for interactive users; the
explicit flag is the only reliable form under `cron`, `docker run -i`,
or any non-TTY environment where stdin is closed.

**The mnemonic prints to your terminal.** Read it from the screen and
write it directly into your password manager. **Never copy/paste it
anywhere else** — terminal output goes to scrollback, screenshot tools,
clipboard managers. After saving, run `clear` to wipe scrollback.

```bash
# Create a hotkey under that coldkey (the per-purpose signing key)
btcli wallet new_hotkey --wallet.name my_miner --wallet.hotkey default
```

Same flow — choose `12` words, save the mnemonic in your password
manager, run `clear`. The hotkey is stored unencrypted (no password
prompt), which is intentional — it's for automated signing.

```bash
# Register on SN21 (testnet 466)
btcli subnet register --netuid 466 \
    --wallet.name my_miner --wallet.hotkey default \
    --subtensor.network test
```

This shows the registration cost (~0.0005 τ on testnet), asks
`Do you want to continue? [y/n]:` (type `y`), then prompts
`Enter your password:` — that's the **coldkey password** from the
first command above.

```bash
# Verify registration
btcli subnet metagraph --netuid 466 --subtensor.network test
```

The Rich-formatted table truncates the SS58 column to `5…` on most
terminals. To confirm your full hotkey is registered, drop into Python:

```python
import bittensor as bt
mg = bt.Subtensor(network='test').metagraph(netuid=466)
print('your hotkey present:', '<your-ss58>' in mg.hotkeys)
print('your UID:', mg.hotkeys.index('<your-ss58>') if '<your-ss58>' in mg.hotkeys else 'NOT REGISTERED')
```

> **Validator-side delay.** The validator's authentication cache refreshes
> the metagraph every ~2 minutes. Brand-new registrations may see a
> `403 Hotkey not registered on subnet` from the validator for up to
> that long after the on-chain registration lands. Wait 2–3 minutes and
> retry.

**Need testnet TAO?** Get free testnet TAO from the Bittensor Discord
faucet bot:
- Join: https://discord.gg/bittensor
- Channel: `#testnet-faucet`
- Command: `!faucet <your-coldkey-ss58>` (paste your coldkey ss58 from
  `btcli wallet list --wallet.name my_miner`)

**SN21-specific community channel:**
[adtao SN21 #general](https://discord.com/channels/799672011265015819/1489651673944297472)
— for protocol questions, announcements, and operator support.

Registration on testnet currently costs ~0.0005 TAO; mainnet pricing
varies — see `btcli subnet register` output for the live cost.

### Step 3: Generate an ed25519 signing key (required, one-time)

Your hotkey↔ed25519 binding is still required for miner identity and
authenticated validator API access (e.g. training endpoints).

```bash
python scripts/sn21_keys.py generate \
    --role miner \
    --output ~/sn21-miner.pem
# Prints the ed25519 public key (safe to share). The private PEM
# stays on disk at ~/sn21-miner.pem (mode 0600). Save the file or
# its contents in a password manager — losing it means you can't
# sign new authenticated requests.
```

### Step 4: Register the ed25519 binding on chain (one-time)

Tells the chain: "ed25519 public key X belongs to my hotkey."

```bash
python scripts/sn21_keys.py register \
    --role miner --network test --netuid 466 \
    --wallet-name my_miner --wallet-hotkey default \
    --key ~/sn21-miner.pem
# Prompts:
#   "Submit registration commit? [y/N]" → type 'y'   (or pass --yes)
#   coldkey password (the one you set in Step 2)
# On success, prints `success: True`, `block: <N>`, and `extrinsic_hash:
# 0x...`. Save the block number — useful for audit.
```

**Scripting / cron note:** add `--yes` to skip the interactive prompt.
The script also auto-confirms when stdin isn't a TTY.

> **Commitments slot is last-write-wins.** Later model-digest commits
> overwrite the registration payload at chain *head*. Validators keep your
> binding via the registration index / lookback — this is expected. Capture
> the `block:` / `extrinsic_hash:` from the register command for audit.

---

## 3. The daily cycle (how submission works now)

**Daily wall clock:** miner submissions follow a **midnight EST** cut-off each day. That is the day boundary for the daily basket / prediction-lock cycle (not UTC unless stated elsewhere).

| Step | Who | What |
| :---- | :---- | :---- |
| 1 | **You** | Build an OCI/Docker image that implements the model contract |
| 2 | **You** | Push it to a registry the subnet can pull; commit `sn21-model:v1:<repo>@sha256:<digest>` on chain |
| 3 | **Subnet** | Runs the **backtest gate** on a held-out corpus — must beat the published naive baseline |
| 4 | **Subnet (daily)** | Executes your admitted image against that day’s basket (`--network=none`, 1 GB / 15 CPU-min) |
| 5 | **Subnet** | Locks stdout predictions; scores later at 7 / 14 / 28 (+ settle) |

Full contract: [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md).  
You do **not** fetch live baskets and POST predictions yourself — the operator sandbox runs your image.

During cutover, “submitted” for bridge pay means your container delivered usable predictions for **≥50%** of that day’s basket — see [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md).

---

## 4. Build and test your model container

### Contract (stdin / stdout)

- **In:** one episode JSON per line on **stdin**
- **Out:** one prediction JSON per line on **stdout**
- Horizons: **`7`**, **`14`**, **`28`**
- Per horizon: monotone p10/p50/p90 for `cost_delta_pct`, `conversions_delta_pct`, `efficiency_delta_pct`; plus `goal_miss_probability` and `instability_risk` in `[0, 1]`
- **No network** inside the sandbox — bake weights / constants into the image
- Budget: **1 GB RAM**, **15 CPU-minutes** per daily basket (~250 episodes)

### Reference image

```bash
cd reference_model
docker build -t sn21-reference-model:v1 .
```

Smoke-test the contract locally:

```bash
# One fake episode line (shape only — use real training payloads for quality)
printf '%s\n' '{"episode_id":"demo","action_type":"BUDGET_CHANGE","from_value":100,"to_value":120}' \
  | docker run --rm -i --network=none --memory=1g --read-only sn21-reference-model:v1
```

You should see one JSON line with `episode_id` and `horizons.7` / `.14` / `.28`.

Production sandbox flags match `hope/backtest/container_runner.py`
(`--network=none`, `--memory=1g`, `--read-only`, `--pids-limit=256`, etc.).

### Prediction line shape

```json
{
  "episode_id": "37b646dcdf02bd6e",
  "horizons": {
    "7": {
      "cost_delta_pct": {"p10": -0.05, "p50": -0.025, "p90": 0.01},
      "conversions_delta_pct": {"p10": -0.06, "p50": -0.035, "p90": 0.005},
      "efficiency_delta_pct": {"p10": -0.03, "p50": 0.01, "p90": 0.04},
      "goal_miss_probability": 0.30,
      "instability_risk": 0.15
    },
    "14": { "...": "same keys" },
    "28": { "...": "same keys" }
  }
}
```

Deltas are fractional relative change from the pre-window baseline:

```
delta = (post_window_avg - pre_window_avg) / pre_window_avg
```

A prediction of `cost_delta_pct.p50 = -0.05` means “I expect daily cost ~5% lower than the pre-window average.” Keep `p10 ≤ p50 ≤ p90`.

---

## 5. Publish your image and commit the digest

### Push a digest-pinned image

```bash
# Example — use your registry namespace
docker tag sn21-reference-model:v1 ghcr.io/<you>/sn21-miner:v1
docker push ghcr.io/<you>/sn21-miner:v1

# Record the repo digest (must match what you commit)
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/<you>/sn21-miner:v1
# → ghcr.io/<you>/sn21-miner@sha256:<64 hex>
```

### On-chain commitment format

```
sn21-model:v1:<repo>@sha256:<64hex>
```

Example:

```
sn21-model:v1:ghcr.io/you/sn21-miner@sha256:0123abcd...  (64 hex chars)
```

Rules (enforced by `hope/backtest/model_registry.py`):

- Repo lowercase, no image tag on the name segment (digest pins the bits)
- Digest exactly `sha256:` + 64 hex
- Keep the full string short enough for the Commitments Raw field (~128 bytes)

Updating your model = **new digest** = re-enters the backtest gate.

> **Intake / gate.** The subnet pulls by digest, verifies `RepoDigests`, and
> runs the admission gate (beat naive baseline on the held-out corpus).
> Coordinate publication with the AdTAO operator via Discord if you need the
> current intake window or registry allow-list details while tooling lands.
> Gate results are published when admission completes.

---

## 6. Train before you ship

Use settled historical baskets (training bundle from the transition, public
exports under `data/episodes/` / `data/outcomes/` when available, plus the
bundled sample):

```bash
# Bundled sample (small; pipeline check)
python scripts/train_example_model.py \
    --data-file data/training/training_episodes.json

# Authenticated training API (hotkey-signed; same auth shape as before)
# GET https://validator.adtao.io/training/episodes
```

Bake the trained weights **into your container**. The sandbox has **no network**,
so the image must be self-contained.

Cutover training + live baskets: [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)
(from **4 August 2026**).

---

## 7. What an episode looks like

Episodes describe account state before interventions. Fields include metadata,
account state, ~60-day pre-window series, and an action bundle with magnitudes.

Launch action types (see `hope/constants.py:LAUNCH_ACTION_TYPES`):

| Type | Key signal |
|------|------------|
| `BUDGET_CHANGE` | `magnitude.spend_change_pct` |
| `BID_STRATEGY_CHANGE` | strategy `from` / `to` — expect learning-period volatility |
| `TARGET_VALUE_CHANGE` | new tCPA/tROAS vs current |
| `CAMPAIGN_PAUSE` | spend change ≈ −100% |

`coverage_status = "trust_enriched"` episodes may include archetypes, guardrails,
health, and portfolio context — use them when present.

Full field walkthrough examples remain in repo training JSON and public episode
exports; the **execution contract** is NDJSON in → NDJSON out as in §4.

---

## 8. How you get paid (pointers)

| Topic | Doc |
| :---- | :---- |
| Per-prediction score & standing | [SN21_SCORING.md](./SN21_SCORING.md) |
| Weight curve & champion | [SN21_REWARDS.md](./SN21_REWARDS.md) |
| Alpha hold ramp | [SN21_STAKING.md](./SN21_STAKING.md) |
| Cutover dates, bridge, indicative burn | [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |

**Before 10 August 2026:** hold **≥150 alpha** and be delivering daily predictions
via your admitted container (bridge eligibility).

Burn rates in the transition plan are **planned and indicative only** and may
change to protect alpha for holders.

---

## 9. Validator API (still useful)

Base URL: **`https://validator.adtao.io`**

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | None | Service health / status |
| `GET` | `/training/episodes` | Hotkey | Training data |
| `GET` | `/training/summary` | Hotkey | Training stats |

Daily basket execution is **not** via HTTP prediction POST. Authenticated
requests (when `REQUIRE_SIGNATURES=true`) need:

| Header | Value |
|---|---|
| `X-Miner-Hotkey` | Your SS58 hotkey |
| `X-Miner-Nonce` | Unix timestamp within validator skew window |
| `X-Miner-Signature` | sr25519 over the canonical message |

Canonical construction: `hope/validator/api/auth.py:verify_miner`.

---

## 10. Troubleshooting

### `validator returns 403 Hotkey not registered on subnet`

You haven't run `btcli subnet register`, or the metagraph hasn't refreshed.
Confirm:

```bash
btcli subnet metagraph --netuid 466 --subtensor.network test
```

### `verify-reg` says "no Raw payload at the latest block"

Expected after a later commitment (e.g. model digest) overwrites head.
Use the `--block-hash` from your original `sn21_keys.py register` success
output against an archive RPC, or trust the validator registration index.

### "do I need my coldkey password every time?"

No. Only coldkey-signed extrinsics (`btcli subnet register`, binding register,
etc.). Hotkey signing for routine commits does not prompt for the coldkey password.

### `btcli ... --quiet` returns no output — did it work?

Avoid `--quiet` here. Verify with:

```bash
btcli wallet overview --wallet.name my_miner --subtensor.network test
btcli subnet metagraph --netuid 466 --subtensor.network test
```

### `bittensor.MaxRetriesExceeded` / `keepalive ping timeout`

Public RPCs flake under load. Retry 2–3 times on a fresh WebSocket.

### `Subnet mechanism 466.0 does not exist`

`--bt-network` / `--netuid` mismatch. Testnet: `test` + `466`. Mainnet: `finney` + `21`.

### Container runs locally but produces zero predictions on a basket

Non-JSON lines are ignored. Ensure every episode gets a valid JSON prediction
line with `episode_id` and `horizons`. Crashes / OOM / timeout → **no scores
that day** (self-penalising via the standing average). Aim for ≥50% coverage
on live days during the bridge.

### Image fails the gate

Admission requires beating the published naive baseline on the held-out corpus.
Re-train, widen calibration, use action magnitudes for direction, rebuild, and
commit a **new digest**.

---

## 11. Quick reference

| Item | Value |
|------|-------|
| Subnet | SN21 (testnet 466 / mainnet 21) |
| Validator | https://validator.adtao.io |
| What you submit | OCI image + on-chain `sn21-model:v1:<repo>@sha256:…` |
| Execution | Operator sandbox daily; stdin/stdout NDJSON |
| Horizons | 7, 14, 28 (+ 7-day settle) |
| Budget | 1 GB RAM / 15 CPU-min per basket |
| Scoring | [SN21_SCORING.md](./SN21_SCORING.md) |
| Rewards | [SN21_REWARDS.md](./SN21_REWARDS.md) |
| Staking | [SN21_STAKING.md](./SN21_STAKING.md) |
| Cutover | [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) |
| Model contract | [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md) |
| Reference image | `reference_model/` |
| Faucet (testnet TAO) | Discord `#testnet-faucet` |

---

*Weekly epoch quickstart material (per-epoch `hope-miner` TimelockEncrypted
bundles, Monday–Sunday mining windows, Tier-2/3 archives) is retired. Registration
and ed25519 binding steps above remain authoritative.*
