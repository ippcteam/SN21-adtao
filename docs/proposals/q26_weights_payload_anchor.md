# Proposal: Add a 32-byte anchor field to `WeightsTlockPayload`

**Authors:** HOPE Foundation (SN21)
**Status:** Draft for Bittensor Subtensor RFC
**Target:** `pallets/subtensor` runtime — `WeightsTlockPayload` struct
**Issue tracking:** SN21 architecture Q26 / T-20b residual gap

---

## 1. Summary

Add a 32-byte `external_anchor: H256` field to the
`WeightsTlockPayload` struct used by the timelocked-weights commit-reveal
path. The anchor is opaque to the runtime — it travels through TLE
encryption alongside `(uids, values, version_key)` and is exposed
unchanged in the `WeightsRevealed` event.

This closes a single residual gap in subnets that pair off-chain scoring
artifacts with on-chain weights commits: today there is no chain-side
binding between a weights commit and the validator's claimed scoring
inputs, so a malicious validator can swap scoring artifacts post-hoc.
The field this proposal adds is the cheapest possible fix — 32 bytes, no
runtime logic — and is purely opt-in for subnets that don't need it.

---

## 2. Problem

A subnet protocol like SN21 (HOPE verifiable-scoring architecture) wants
the following property:

> The weights a validator commits MUST be the deterministic output of a
> publicly-verifiable scoring function applied to publicly-verifiable
> inputs.

The architecture handles "publicly-verifiable inputs" via off-chain
artifacts (per-episode prediction bundles) plus on-chain Merkle roots in
the validator's Layer 9.C.2 post-scoring artifacts commit. The Merkle
root is bound to the validator's hotkey via inner_sig.

What the architecture CANNOT achieve at the chain level today is:

> Bind THIS weights commit to THAT scoring artifact.

A malicious validator could:

1. Commit scoring artifact A at block X (TLE'd, reveals at round X+R).
2. Compute weights from artifact A.
3. Commit those weights via `commit_timelocked_weights` at block Y.
4. AFTER A reveals, construct a different artifact A' that justifies the
   same weights, and serve A' instead of A from off-chain storage.

The chain has no way to distinguish A from A' because the weights
payload doesn't reference the artifact at all.

The architecture currently mitigates this operationally — a HOPE-run
shadow validator independently computes scoring and submits a parallel
9.C.2/9.C.3 pair, and Yuma's stake-weighted median clips the dishonest
actor when stake is balanced. This works but is operational not
cryptographic, and degrades gracefully (not catastrophically) only when
shadow operators are themselves trustworthy.

A 32-byte chain-side anchor would close this gap.

---

## 3. Proposed change

### 3.1 Struct change

In `pallets/subtensor/src/migrations/v3_weights_commit/types.rs` (or
wherever `WeightsTlockPayload` is defined):

```rust
#[derive(Encode, Decode, ...)]
pub struct WeightsTlockPayload {
    pub uids: BoundedVec<u16, MaxValidators>,
    pub values: BoundedVec<u16, MaxValidators>,
    pub version_key: u64,
    /// Opaque 32-byte anchor for off-chain artifact binding.
    /// Subnets that don't need it can pass `[0u8; 32]`.
    pub external_anchor: H256,
}
```

### 3.2 Extrinsic change

`commit_timelocked_weights` gains an `external_anchor: H256` parameter.
The runtime stores it inside `WeightsTlockPayload` before TLE
encryption. No validation logic — the runtime treats it as opaque bytes.

### 3.3 Event change

`WeightsRevealed { netuid, hotkey, ... }` gains an `external_anchor`
field. After auto-decrypt, off-chain consumers read the anchor from the
event log.

### 3.4 Backward compatibility

Existing subnets calling the old `commit_timelocked_weights` extrinsic
get `external_anchor = H256::zero()`. Their behaviour is unchanged.

The struct change is a runtime-storage migration — existing committed
payloads need re-encoding. A migration helper that pads old entries
with zeros is straightforward.

---

## 4. How SN21 would use it

The validator's Layer 9.C.3 weights commit would call:

```rust
commit_timelocked_weights(
    netuid: 21,
    payload: WeightsTlockPayload {
        uids:           ...,
        values:         ...,
        version_key:    10002001,
        external_anchor: H256::from(<32-byte hash of canonical CBOR
                                      encoding of the validator's 9.C.2
                                      scoring inputs hash + epoch_idx>),
    },
    reveal_round:    R,
)
```

After auto-decrypt, the verifier reads the anchor from the event,
recomputes its own anchor from the chain-anchored scoring artifact, and
asserts equality. Any mismatch = malicious validator, publicly auditable.

---

## 5. Alternatives considered

### 5.1 Use `version_key`

`version_key: u64` is 8 bytes — too small for a cryptographic hash. It's
also intended for protocol versioning, not arbitrary opaque data.
Repurposing it would conflate two concerns and limit future protocol
evolution.

### 5.2 Off-chain anchoring only

Today's mitigation: shadow validators + Yuma stake-weighted median. This
works as an operational defense but doesn't cryptographically prevent
the attack — it only makes it economically expensive when stake is
balanced. A 32-byte anchor is strictly cheaper and stronger.

### 5.3 Separate `commit_weights_anchor` extrinsic

Could be used to anchor weights to off-chain artifacts via a separate
extrinsic. But: (a) it costs an extra MaxSpace slot per epoch
(~532 B, see SN21 architecture §18.2), (b) it's ordering-sensitive (anchor
extrinsic must finalize before weights extrinsic, or attackers can pivot),
(c) it doubles the extrinsic count for a chain-anchoring goal that fits
in 32 bytes. Inline field is simpler.

### 5.4 Use `Sha256` commitment field separately

Subnets could commit a separate `Sha256` to the Commitments pallet. But
the Commitments pallet stores ONE commit per (netuid, account) per
extrinsic, and the new commit OVERWRITES the previous one. Pairing it
with the weights commit at the same block requires either atomicity (a
batch extrinsic) or careful timing. Inline field avoids both issues.

---

## 6. Implementation sketch

The change touches three files in `pallets/subtensor/`:

```
pallets/subtensor/src/migrations/v3_weights_commit/
  types.rs       (add `external_anchor: H256` to WeightsTlockPayload)
  mod.rs         (migration: pad existing entries with zero)

pallets/subtensor/src/lib.rs
  pub fn commit_timelocked_weights(..., external_anchor: H256)
  emit WeightsRevealed event with external_anchor
```

LOC estimate: ~80 lines of Rust + ~50 lines of migration + tests.

---

## 7. Testing

### 7.1 Unit tests (Rust)

- `commit_timelocked_weights` with zero anchor matches pre-change
  behaviour byte-for-byte.
- Two distinct anchors produce distinct stored payloads.
- The `WeightsRevealed` event carries the anchor verbatim.

### 7.2 Migration tests (Rust)

- Existing storage entries from before the change get
  `external_anchor = H256::zero()` after migration.
- Total storage cost increase: 32 bytes per entry. For a typical 256
  validators × 4 active subnets × 1 active reveal = ~32 KB increase
  globally — negligible.

### 7.3 Integration test (Python, SN21-side)

After the runtime change ships, SN21's `verify_epoch.py` adds an extra
check:

```python
# Read the WeightsRevealed event for this validator's commit.
event = subtensor.find_weights_revealed(netuid, validator_hotkey, block)
expected_anchor = blake2b_256(canonical_cbor({
    "epoch_id": epoch_id,
    "scoring_inputs_hash": post_9c2.scoring_hash,
}))
assert event.external_anchor == expected_anchor, "T-20b: anchor mismatch"
```

A passing test asserts the chain enforced the binding.

---

## 8. Costs

- Per weights commit: +32 bytes storage (negligible).
- Per `commit_timelocked_weights` extrinsic: +32 bytes encoded → ~32 bytes
  more transaction bytes. Fee delta: nominal.
- Migration: one-time, no operator action required.
- Subnet adoption: opt-in. Subnets passing zeros are unchanged.

---

## 9. Adoption path

1. Open subtensor PR with the runtime change + migration + tests.
2. Land in a runtime-upgrade window.
3. SN21 (and any interested subnet) gates its weights commits on the new
   field, dropping the operational shadow-validator burden once Yuma's
   defense is no longer the only mitigation.

---

## 10. References

- SN21 architecture §15 "Self-Audit: Against the Original Auditor's Concerns" — describes T-20b as the residual gap.
- SN21 architecture §17 — Phase D/E summary; §17.5 lists Q26 as the only remaining cryptographic gap.
- This proposal: `docs/proposals/q26_weights_payload_anchor.md`.
