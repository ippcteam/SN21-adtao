# SN21 Operator Runbook

> **Most of this runbook describes the weekly-epoch path** (TLE commits,
> three-tier archives, per-epoch `hope-validator` scoring). That era concluded
> on 3 August 2026 and these sections are retained for verifying its history.
>
> **For daily-stream operation**, use
> [validator_setup §2](./validator_setup.md#2-quick-start-local-testing) —
> the three processes, what the daily loop needs, and when to run it — with
> [SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md) for the dates.
> Nothing in the weekly sections below is miner-facing truth for the daily
> stream.

Production cutover playbook for subnet operators, primary validators,
shadow validators, and miners. Covers deployment, key management,
registration, epoch operations, and incident response.

This document is the authoritative source for "how do I run this?" for the
**weekly-era** stack still present in the repo. Architecture context lives in
`docs/whitepaper.md` (the protocol description; weekly sections historical)
and `docs/build_journey.md` (phase-by-phase build narrative).

---

## 1. Roles

| Role | Hotkey + ed25519 Bundle | Chain Slots Used |
|---|---|---|
| Outcome signer | 1 SS58 + 1 ed25519 (role=outcome_signer) | 9.A.1, 9.A.2 |
| Primary validator | 1 SS58 + 1 ed25519 (role=validator) | 9.C.1, 9.C.2, 9.C.3, optional 9.C.6 |
| Shadow validator (operator-run) | 1 SS58 + 1 ed25519 (role=validator) | Same slots as primary, different hotkey |
| Miner | 1 SS58 + 1 ed25519 (role=miner) | 9.B (3 commits per epoch) |

Each role is a separate Bittensor wallet + a separate ed25519 PEM file.
Backup the PEM files offline (cold storage) — loss = forced re-registration.

---

## 2. First-time setup

### 2.1 Install

```bash
git clone <repo-url>
cd SN21-adtao
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 2.2 Generate ed25519 keys (per role)

```bash
mkdir -p ~/.sn21/keys
python scripts/sn21_keys.py generate \
    --role miner \
    --output ~/.sn21/keys/miner-ed25519.pem
python scripts/sn21_keys.py sign-test \
    --key ~/.sn21/keys/miner-ed25519.pem      # round-trip self-test
python scripts/sn21_keys.py show \
    --key ~/.sn21/keys/miner-ed25519.pem      # prints 32-byte pubkey hex
```

PEM files are written with mode 0600. Back up the PEM file to offline
storage (encrypted USB / hardware wallet vault). Production hotkey
operations do not need the PEM unless you re-register the binding.

### 2.3 Register the hotkey ↔ ed25519 binding on chain

```bash
python scripts/sn21_keys.py register \
    --role miner \
    --netuid 21 --network finney \
    --wallet-name my-miner --wallet-hotkey default \
    --key ~/.sn21/keys/miner-ed25519.pem
```

Cost: 1 `Raw{109}` extrinsic against MaxSpace (~609 bytes including
overhead per §18.2 of the architecture doc).

Verify from any third party:

```bash
python scripts/sn21_keys.py verify-reg \
    --netuid 21 --network finney \
    --hotkey-ss58 5...miner...ss58
```

Expected output: `sig valid: True`. If False, the binding is malformed
or the SS58 is bound to a different ed25519 key than the one you have.

---

## 3. Outcome signer — daily epoch routine

For each scheduled epoch (recommended cadence ≥ 4.5 hours):

The 9.A.1 / 9.A.2 modules expose Python APIs (no `__main__` CLI).
Operators wrap them in their own production scheduling. Reference
implementations:

### 3.1 At T=0 (epoch open) — submit 9.A.1 release_commit

```python
import bittensor as bt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives import serialization
from hope.hope_outcomes.release_commit import (
    build_release_commit,
    submit_release_commit_layer_9a1,
)

with open("~/.sn21/keys/outcome-signer-ed25519.pem", "rb") as f:
    signing_key = serialization.load_pem_private_key(
        f.read(), password=None
    )

# 1. Build the release_commit CBOR (rules + episode hashes + drand round).
release_blob = build_release_commit(
    epoch_id="EPOCH-2026-W18-MON",
    episodes=episodes,                # list of Episode objects
    drand_round_at_open=current_round,
    signer_key=signing_key,
)

# 2. Submit the BLAKE2b-256 digest as 9.A.1 chain commit.
subtensor = bt.Subtensor(network="finney")
wallet = bt.Wallet(name="outcome-signer")
result = submit_release_commit_layer_9a1(
    subtensor=subtensor,
    wallet=wallet,
    netuid=21,
    release_commit_blob=release_blob,
)
print(f"9.A.1 chain block: {result.block_number}")
print(f"plaintext digest:  {result.message}")  # BLAKE2b-256 hex
```

Serve `release_blob` at `https://outcomes.example.io/release/{epoch_id}`
**only after** `result.block_number` is finalized (CL-9 commit-then-
serve gate).

### 3.2 At T=deadline (miner deadline + δ) — submit 9.A.2 reveal blob

```python
from hope.hope_outcomes.reveal_blob import (
    build_reveal_blob,
    submit_outcome_reveal_hash_layer_9a2,
)

# 1. Build the reveal blob (signed measured outcomes).
reveal_blob = build_reveal_blob(
    epoch_id="EPOCH-2026-W18-MON",
    release_commit_sha256=release_commit_sha256,  # from §3.1
    outcomes=outcomes,                            # list of Outcome objects
    signer_key=signing_key,
)

# 2. Submit SHA-256(reveal_blob) on chain.
result = submit_outcome_reveal_hash_layer_9a2(
    subtensor=subtensor,
    wallet=wallet,
    netuid=21,
    reveal_blob=reveal_blob,
)
print(f"9.A.2 chain block: {result.block_number}")
```

Serve `reveal_blob` at `https://outcomes.example.io/reveal/{epoch_id}`
**only after** the 9.A.2 commit finalizes.

> If you want a single-command CLI for these two operations, the
> simplest path is to wrap the snippets above in your own
> `scripts/sign_epoch.py`. The library functions are the contract;
> the CLI is operator-specific.

---

## 4. Primary validator — daily epoch routine

### 4.1 Continuous mode

```bash
python -m hope.validator.runner \
    --mode onchain \
    --network finney --netuid 21 \
    --wallet-name primary-validator --wallet-hotkey default \
    --ed25519-key-file ~/.sn21/keys/validator-ed25519.pem \
    --archive-tier-2 https://archive.example.io \
    --archive-tier-2 https://archive-eu.example.io \
    --release ${RELEASE_KEY}
```

Per epoch, the runner emits:
- 9.C.1 pre-scoring state (TLE, ~980 B chain cost)
- 9.C.3 weights via `commit_timelocked_weights` (separate pallet)
- 9.C.2 post-scoring artifacts (TLE, ~980 B)
- 9.C.6 retry log (only if any miner excluded for `plaintext_unavailable`; ~532 B)

Per-epoch validator MaxSpace usage: ~1,960 B without retry log, ~2,492 B
with. Comfortably within 3,100 B per 4.2-hour window.

### 4.2 Single epoch (manual)

```bash
python -m hope.validator.runner \
    --mode onchain \
    --score-now \
    --release EPOCH-2026-W18-MON \
    --network finney --netuid 21 \
    --wallet-name primary-validator --wallet-hotkey default \
    --ed25519-key-file ~/.sn21/keys/validator-ed25519.pem \
    --archive-tier-2 https://archive.example.io
```

Returns an `EpochScoringOutcome`. Check `outcome.ok` and the four chain
commit blocks/hashes.

---

## 5. Shadow validator (operator-run)

Identical to §4 but with the shadow's hotkey + ed25519 key:

```bash
python -m hope.hope_shadow_validator.runner \
    --network finney --netuid 21 \
    --wallet-name shadow-validator --wallet-hotkey default \
    --ed25519-key-file ~/.sn21/keys/shadow-ed25519.pem \
    --archive-tier-2 https://archive.example.io \
    --release ${RELEASE_KEY}
```

The shadow MUST run independent code from the primary at the operational
level (different hotkey, different host, ideally different cloud
provider). The architecture's SH-5 defense relies on the shadow signing
its own inner_sig.

---

## 6. Miner — daily epoch routine

```bash
python -m hope.miner.runner \
    --mode onchain \
    --epoch ${EPOCH_ID} \
    --validator-url <validator-url> \
    --network finney --netuid 21 \
    --wallet-name my-miner --wallet-hotkey default \
    --ed25519-key-file ~/.sn21/keys/miner-ed25519.pem \
    --archive-tier-2 https://archive.example.io \
    --archive-tier-3 https://my-miner.example/archive \
    --blocks-until-reveal 300
```

Per epoch, the miner emits:
- TimelockEncrypted K (~1,110 B)
- Sha256(AES_ct) (~532 B)
- Raw{N} URL (~532 B)

Per-epoch miner MaxSpace usage: ~2,174 B. Fits one 4.2-hour window per
epoch.

The Tier-3 self-archive at `--archive-tier-3` MUST be running and
reachable for AT LEAST the next epoch (validators fetch AES_ct after the
miner's K reveals). Use `deploy/archive_server/` to spin one up.

---

## 7. Archive server — deployment

See `deploy/archive_server/README.md` for full instructions. Quick path:

```bash
cd deploy/archive_server
docker compose up -d                                 # Tier-2/Tier-3
curl -s http://localhost:8080/healthz                # liveness
```

For Tier-2 (operator shadow), set `SN21_ARCHIVE_REQUIRE_SIGNED=true` so
miners must sign uploads with their hotkey (`X-Miner-Hotkey` +
`X-Miner-Signature` headers). For Tier-3 self, leave it `false`.

Retention sweep (cron):

```bash
# Tier-2: 90 days
find /var/lib/sn21-archive -mindepth 1 -maxdepth 1 -type d -mtime +90 -exec rm -rf {} +
```

---

## 8. Public verification

Anyone (auditor, advertiser, third party) verifies an epoch.

### 8.1 Archive node requirement

The Bittensor `Commitments::CommitmentOf` storage holds ONE entry per
`(netuid, hotkey)` and is overwritten by every new commit (Phase H
finding §19.3). To audit a PAST epoch the verifier MUST use an archive
node + a block-pinned read:

```bash
python scripts/verify_epoch.py \
    --epoch-id EPOCH-2026-W18-MON \
    --validator-hotkey 5...primary...ss58 \
    --netuid 21 --network finney \
    --block-hash 0x<block hash where validator's 9.C.2 commit landed> \
    --tier-2-base https://archive.example.io \
    --output json
```

The validator publishes the `block_hash` for each of its 9.C.1, 9.C.3,
9.C.2 commits in its post-scoring artifacts off-chain manifest at
`https://validator.example.io/epoch/{epoch_id}.json`. Auditors retrieve
that manifest, then call verify_epoch with the right block_hash.

### 8.2 Real-time verification (chain head)

To verify the LATEST epoch (the one that just finalized), omit
`--block-hash` and the verifier reads chain head:

```bash
python scripts/verify_epoch.py \
    --epoch-id EPOCH-2026-W18-MON \
    --validator-hotkey 5...primary...ss58 \
    --netuid 21 --network finney \
    --tier-2-base https://archive.example.io \
    --output json
```

This works only if NO new commit from the same hotkey has landed
between the target commit and now. Tighter audit windows (e.g.,
"verify within 30 min of weights commit") may use chain head; longer
windows MUST use --block-hash.

### 8.3 Archive node operator setup

Run a Bittensor mainnet archive node:

```bash
# subtensor archive mode
docker run -d --name subtensor-archive \
    -p 9944:9944 \
    opentensor/subtensor:latest \
    --base-path /data/subtensor \
    --chain finney \
    --pruning archive \
    --rpc-cors all
```

Point the verifier at it via `--network ws://localhost:9944` (or
configure Bittensor to use the local archive endpoint).

### 8.4 Verdict

Expected output (JSON): `"ok": true`. A `false` outcome implies the
validator and the verifier diverge on either the IMT roots or the
inner_sig — open an incident ticket immediately.

---

## 9. Incident response

### 9.1 `SpaceLimitExceeded`

Symptom: a chain extrinsic returns
`Subtensor returned: SpaceLimitExceeded(Module)`.

Diagnosis: the (netuid, hotkey) burned its 3,100 B MaxSpace within a
4.2-hour rolling window.

Action:
1. Stop submitting from that hotkey for ≥ 4.5 hours.
2. Inspect recent commits — if a 9.C.6 retry log was emitted unnecessarily,
   tune the `require_tier_2` flag in the miner runner.
3. If you operate multiple hotkeys, route the next epoch to one with
   headroom.

### 9.2 Archive fetch fails for a miner

Symptom: validator emits 9.C.6 retry log; miner is excluded with
`plaintext_unavailable`.

Diagnosis: AES_ct not available at any tier. Check:
- Tier-2 (operator) — `curl -fI https://archive.example.io/healthz`
- Tier-3 (miner self) — URL listed in chain commit

Action:
1. Page the miner if Tier-3 is down.
2. Confirm Tier-2 retention hasn't aged out the bytes.
3. If neither tier has the bytes, the miner missed the upload — they
   are excluded for this epoch by design.

### 9.3 drand pulse outage

Symptom: `bittensor_drand.encrypt(...)` fails or weights commits hang.

Diagnosis: drand quicknet feed is down. The chain auto-decrypt path
depends on it.

Action:
1. Check `https://api.drand.sh/52db9ba.../public/latest`.
2. If outage > 5 min, halt epoch submissions; resume when drand recovers.
3. If outage > 24h, governance issues a `Sha256` commit with payload
   `b"v1.0:emergency-fallback-instructions"` pointing to a manual
   recovery procedure (whitepaper §12.1).

### 9.4 Hotkey ↔ ed25519 binding lost

Symptom: `verify_reg` returns `sig valid: False` or the ed25519 PEM is
lost.

Action:
1. If PEM still exists but on-chain binding is wrong — re-run
   `sn21_keys.py register`. Old binding is overwritten by the latest
   `Raw{N}` commit.
2. If PEM is lost — generate a NEW key, register the new binding. The
   old binding is invalidated as soon as the new commit lands. Existing
   in-flight commits using the old key remain verifiable until their
   reveal_round; new commits MUST use the new key.

### 9.5 Validator and shadow disagree

Symptom: `verify_epoch.py` returns `miner_commits_match: false` OR
`final_score_match: false` for one validator vs the other.

Diagnosis: deterministic divergence. One of the validators has a bug,
ran stale code, or is malicious.

Action:
1. Re-run `verify_epoch.py` against BOTH validator hotkeys.
2. If primary mismatches, shadow's view is canonical (architectural
   defense — Yuma stake-weighted median clips the dishonest actor).
3. Open an incident; escalate to the dispute path if the divergence
   persists across multiple epochs.

---

## 10. Key rotation

Quarterly rotation recommended for the outcome signer key (highest
trust). Procedure:

```bash
# Generate new key
python scripts/sn21_keys.py generate \
    --role outcome_signer \
    --output ~/.sn21/keys/outcome-signer-ed25519-2026Q3.pem

# Register binding (overwrites old binding for this hotkey)
python scripts/sn21_keys.py register \
    --role outcome_signer --netuid 21 --network finney \
    --wallet-name outcome-signer \
    --key ~/.sn21/keys/outcome-signer-ed25519-2026Q3.pem
```

The old key remains verifiable for any in-flight commits until their
reveal_round. New commits MUST use the new key. Archive the old PEM
offline; do NOT delete (needed for retroactive auditing).

Validator + miner key rotation: same procedure, but coordinate timing
with operations to avoid mid-epoch rotation.

---

## 11. Daily on-call checklist

- [ ] All hotkey balances > 1 testnet/mainnet TAO (registration burn buffer).
- [ ] All MaxSpace headroom > 1,000 B (no recent SpaceLimitExceeded).
- [ ] Tier-2 archive `/healthz` returns 200.
- [ ] `verify_epoch.py` against last finalized epoch returns `ok: true`
  for primary AND shadow.
- [ ] Outcome signer commits both 9.A.1 AND 9.A.2 within 1 tempo
  of the deadline.
- [ ] Validator runner logs show `EpochScoringOutcome.ok=True`.
- [ ] Shadow validator `miner_commits_root` MATCHES primary's (paste
  both into a side-by-side check; structurally identical).

---

## 12. Mainnet pre-launch checklist

Before flipping `--network test` → `--network finney`:

- [ ] All probes from §18 of the architecture doc rerun on mainnet
  (Q11 + Q13 specifically — testnet hyperparams may differ).
- [ ] Mainnet hotkey registration on netuid 21 (burn cost paid).
- [ ] All 4 ed25519 keys (outcome signer / primary / shadow / first miner) bound
  on-chain via `sn21_keys.py register --network finney`.
- [ ] Tier-2 archive deployed at production URL with TLS, retention,
  monitoring, on-call paging.
- [ ] One full mainnet epoch dry-run: submit, score, weights commit,
  verify. NO LIVE EMISSIONS until this returns `ok: true`.
- [ ] Operator runbook reviewed by the on-call team.
- [ ] Dispute path tested end-to-end.
