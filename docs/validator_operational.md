# Validator Operational Runbook

> **Pre-daily / operator historical.** Sections describing weekly scoring cadence predate the daily stream; the daily path runs `daily_loop` on the settle clock. See [SN21_TRANSITION_PLAN](./SN21_TRANSITION_PLAN.md).


Operational guide for SN21 validators — what to expect during normal
running, what failures look like, and how to act on them.

---

## Per-epoch lifecycle

Each epoch, a validator does this once, post-deadline:

1. Read all miners' Layer 9.B bundles from `RevealedCommitments`.
2. For each miner: fetch AES_ct from their archive URL, SHA-cross-check,
   decrypt with the revealed K, verify ed25519 inner_sig, run the
   eight-check scoreability rule.
3. Score accepted miners against measured outcomes from the operator's
   data API.
4. Submit four chain commits in order:
   - **9.C.1** TimelockEncrypted pre-scoring state (~600 B)
   - **9.C.3** weights via `commit_weights_v3` (different pallet — separate budget)
   - **9.C.2** TimelockEncrypted post-scoring artifacts (~600 B)
   - **9.C.6** Sha256 retry log (~32 B, only when ≥1 miner excluded for
     `plaintext_unavailable`)

Total Commitments-pallet usage per epoch: ~1232 B. Comfortably below
the 3100 B per-epoch budget (`Commitments::MaxSpace`).

---

## Commitments pallet byte budget

Substrate's `Commitments` pallet caps each `(netuid, hotkey)` to a
fixed byte budget per pallet-epoch:

```
Commitments::MaxSpace             = 3100 bytes (live runtime constant)
Commitments::UsedSpaceOf(netuid, hotkey) = { used_space, last_epoch }
```

When `used_space + new_commit_size > MaxSpace`, the chain returns
`SpaceLimitExceeded(Module)` and rejects the extrinsic.

The `last_epoch` counter is governance-set on the chain runtime and
**not** aligned with the subnet tempo. When `last_epoch` advances,
`used_space` resets to 0 on the first new commit. Empirically on
testnet, the reset cadence is on the order of hours; mainnet defaults
similarly. Plan for "one validator scoring round per pallet-epoch".

### How the validator runner protects the budget

`run_epoch_scoring` performs two pre-flight checks before any commit:

1. **Idempotency**: if the validator has a revealed 9.C.1 commit for
   the current `epoch_id` already, abort with `aborted_reason=already_scored`.
2. **Budget**: query `UsedSpaceOf`. If `MaxSpace - used_space <
   MIN_VALIDATOR_BUDGET_BYTES` (1300 B), abort with
   `aborted_reason=insufficient_budget`.

Both abort modes return cleanly with no partial commits. Re-running on
the next pallet-epoch will succeed.

---

## What the operator sees in logs

### Healthy run

```
INFO validator 5Chw... has 2 revealed commitments (audit path)
INFO miner 5G7A... bundle decrypted, scoreable=True
INFO 9.C.1 committed at block 7077511
INFO 9.C.3 weights at block 7077515
INFO 9.C.2 committed at block 7077519
On-chain epoch outcome:
  ok: True
  9.C.1 block: 7077511
  9.C.3 block: 7077515
  9.C.2 block: 7077519
```

### Budget already exhausted (re-run within same pallet-epoch)

```
On-chain epoch outcome:
  ok: False
  aborted_reason: insufficient_budget: validator 5Chw... has 202B free
                  in the Commitments pallet (need ≥1300B; last_epoch=19607).
                  Wait for the pallet-epoch to advance, or use a fresh
                  validator hotkey.
```

**Action**: do nothing. Wait for the next pallet-epoch. The next cron
fire will succeed.

### Already scored this epoch

```
On-chain epoch outcome:
  ok: False
  aborted_reason: already_scored: validator 5Chw... has a prior 9.C.1
                  commit for epoch_id=WR-2026-W18-PUB-E1.
```

**Action**: nothing. The previous run already produced the chain
record. Verify with `scripts/verify_epoch.py` if needed.

### No scoreable miners

```
INFO validator ... has 0 revealed commitments (need 2 for audit);
     continuing with empty pre/post blobs (first-scoring path).
On-chain epoch outcome:
  ok: False
  aborted_reason: weights_commit_failed: no scoreable miners; skipping weights commit
```

**Action**: usually transient — Subtensor's public RPC is load-balanced
and a stale node may return missing reveals. Simply re-run; a new
connection may land on a synced node.

If 5+ retries all show 0 scoreable miners, the cause is one of:
- The current epoch's bundle reveal hasn't fired yet (wait for the
  next subnet tempo step on netuid 466)
- All miners genuinely failed the scoreability rule (check
  `miner_reads` in the runner output for per-miner reasons)

### Subtensor returned `SpaceLimitExceeded`

This indicates the runner's pre-flight check was bypassed (e.g., race
condition, or the budget changed mid-run). The chain rejected an
extrinsic. Check `UsedSpaceOf` to confirm:

```bash
python -c "
import bittensor as bt
sub = bt.Subtensor(network='test')  # or 'finney'
v = '<validator-hotkey-ss58>'
r = sub.substrate.query('Commitments', 'UsedSpaceOf', [466, v])
print(r)
"
```

If `used_space` is at or near `MaxSpace`, wait for the next pallet-epoch.

---

## Cron configuration

Schedule the scoring run **once per epoch deadline**. On testnet 466 the
weekly schedule was `0 6 * * 1` (Mondays 06:00 UTC, about an hour after the
`WR-YYYY-WNN-PUB-E1` deadline).

**Do not** configure auto-retry on the scheduled job. If a run fails,
the runner's idempotency check will cleanly skip on the next
pallet-epoch — no manual intervention needed unless the failure is
persistent (then check logs for the `aborted_reason`).

---

## Manual recovery

If you find the validator is in a stuck state, the safe sequence is:

1. **Check logs** for the most recent `aborted_reason`.
2. **Query `UsedSpaceOf`** to see budget state.
3. **Query `RevealedCommitments`** for the validator hotkey to see what
   commits are already on chain.
4. If a commit landed but the runner crashed before completing all
   four — wait for the next pallet-epoch. The runner is idempotent and
   will pick up from where it left off (skipping commits already
   recorded for the same `epoch_id`).
5. If you genuinely need to recover from a broken validator hotkey
   (e.g., used_space stuck near MaxSpace and the pallet-epoch isn't
   rolling over fast enough) — burn a fresh validator hotkey, register
   it on the subnet, and point your deployment's `HOTKEY_NAME` at it. The
   protocol does not require a stable validator hotkey across epochs.
