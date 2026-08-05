# Build your own SN21 registration index (self-serve)

To score an SN21 miner, a validator must verify each prediction's inner
signature against the **ed25519 key that miner registered** (its `sn21-reg-v1`
commitment). This page shows you how to **build that registration index
yourself, directly from chain** — no file from the operator, nothing to trust.
Every binding is re-derived from chain events and signature-checked before it's
indexed (*build-by-construction*).

> **You do not need a prebuilt JSON from anyone.** Point the builder at your own
> archival node and you get the same index every other validator gets.

---

## Why an index is needed at all

A miner registers by writing a `sn21-reg-v1` record to the Commitments pallet:

```
b"sn21-reg-v1:" + role_byte(M/V/O) + ed25519_pubkey(32) + ed25519_signature(64)
```

This binds the **chain hotkey (SS58) ↔ ed25519 signing key**, and the embedded
signature proves the hotkey controls that ed25519 key.

The catch: a miner usually submits its prediction bundle **in the very next
block**, which **overwrites** the `sn21-reg-v1` slot in current chain state. So
a validator reading only HEAD sees the bundle, not the registration, and wrongly
drops the miner as "unregistered." The registration is therefore only readable
from **historical state at its commit block** — which is why this needs an
**archive node**, and why you build an index once and keep it updated.

## Why build it **daily** (the easy way)

Instead of reconstructing weeks-old registrations from deep history, **scan once
a day**. The builder reads the `Commitments.Commitment` **event**, which fires
at the commit block regardless of what overwrites the slot afterwards — so a
daily run catches every registration within ~24h of commit, from recent blocks
that are cheap to read (instant on your local archival node). Run it daily and
by the time a submission window closes your index already holds every registered
miner.

---

## The command

```bash
# point at YOUR archival node (historical state required)
SN21_SUBTENSOR_URL=ws://localhost:9944 \
python scripts/build_reg_index.py \
    --network finney --netuid 21 --role miner \
    --index /path/on/persistent/disk/sn21-reg-index.json \
    --reconnect
```

- **First run (cold start):** with no prior checkpoint it scans a safety window
  back from head. To backfill a specific range once, add
  `--backfill-start <block>`.
- **Every run after:** it resumes from the saved checkpoint and only scans the
  new blocks since last time (a daily tick is ~7,200 blocks — seconds on a local
  node).
- **Long-running:** add `--loop --interval-hours 24` to run it as a daemon.

### What it writes

| File | Contents |
|---|---|
| `--index` path | the reg-index — a **bare JSON list** of `{hotkey_ss58, hotkey_pk_hex, ed25519_pk_hex, role, block_number}` (one per hotkey; newest registration wins) |
| `<index>.state.json` | a sidecar checkpoint `{last_scanned_block, ...}` so the next run resumes cheaply |

Keep both on a **persistent disk** — the checkpoint is what makes each run
cheap. If you host on a managed platform, use a service type that offers
persistent storage rather than an ephemeral scheduled job. Co-locating with
your archive node is ideal.

---

## Verify it (don't trust — check)

1. **Build-by-construction:** every entry was already signature-verified during
   the scan (`verify_registration`, role-checked). A binding only enters the
   index if its on-chain ed25519 signature is valid.
2. **Independent re-verify:** a self-contained verifier (only
   `substrate-interface` + `cryptography`, no subnet code) re-reads each
   `sn21-reg-v1` at its block and re-checks the signature:
   ```bash
   python verify_reg_index.py --index sn21-reg-index.json \
       --rpc wss://archive.chain.opentensor.ai:443
   ```
3. **Cross-check (optional):** the operator publishes its per-run index/output
   purely so you can `diff` your independently-built result against it. It's a
   sanity check, never a dependency — if they ever diverge, your chain-derived
   index is authoritative.

## Feed it to scoring

```bash
hope-validator ... --reg-index-prebuilt /path/.../sn21-reg-index.json
```

…or load the file into your own pipeline. The result: every registered miner
that submitted a valid prediction is recognized and scored.

---

## Requirements & notes

- **Archive node** (historical state). Validators running their own archival
  nodes are already set; a pruned/standard RPC will miss the overwritten
  registrations. The only public mainnet archive
  (`wss://archive.chain.opentensor.ai:443`) works but is slow/flaky — your own
  node is far better.
- **Key rotation is handled:** if a miner re-registers a new ed25519 key, the
  index keeps the newest (highest-block) binding automatically.
- **Genuinely unregistered keys can't be fixed index-side:** a miner that signs
  with a key it never registered must run `sn21_keys.py register` for the key it
  actually signs with.
