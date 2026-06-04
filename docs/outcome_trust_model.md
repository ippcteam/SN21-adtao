# SN21 outcome trust model — verify scoring independently

This explains exactly what a validator must trust, what it can **verify**, and
how to do that verification yourself. Short version: **you can already fetch the
outcomes, verify they are the operator's signed values, and re-derive every
score independently against on-chain anchors.** The one irreducible trust is the
*honesty* of the measured ad-impact data (which only the operator can produce) —
and below is how we minimize even that.

---

## What you can verify today (no trust beyond the measurement itself)

1. **Outcomes are signed.** The operator's `/releases/{key}/package?include_outcomes=true`
   response is ed25519-signed with the operator key; the public key is published
   (tao-discovery repo). The validator client verifies it before accepting
   (`REQUIRE_HOPE_SIGNATURE=true`). → outcomes are **attributable + tamper-evident**.

2. **Scoring is anchored on-chain.** Each epoch the validator commits
   `scoring_inputs_hash` (9.C.1) and `final_score_root` (9.C.2). These bind the
   exact inputs (incl. outcomes) and the resulting score table to chain.

3. **Scoring is independently reproducible.** `scripts/verify_epoch.py` re-reads
   the on-chain commits, re-derives every miner's scoreability + score from the
   raw chain bundles + the signed outcomes, and **asserts equality** with the
   chain-anchored roots:
   ```bash
   python scripts/verify_epoch.py --epoch-id <EPOCH> \
       --validator-hotkey <VALIDATOR_SS58> --netuid 21 --network finney \
       --tier-2-base <archive-url> --truth-file <signed-outcomes.json>
   ```
   A pass means: *given these (signed) outcomes, the scores on chain are exactly
   what the rules produce.* Any divergence is attributable to one party.

4. **Registrations are self-built.** You build the reg-index from your own
   archival node (see `reg_index_self_serve.md`) — no operator hand-off.

So the scoring math, the inputs binding, and the registration data are all
**verifiable without trusting the operator**.

## On-chain outcome commitment (P4b — pins ONE canonical outcome set)

To remove "could the operator serve different / post-hoc-altered outcomes," the
operator commits the **canonical hash of each epoch's outcome set on chain** at
reveal, under the outcome-signer identity:

```
b"sn21-outcomes-v1:" + epoch_id + sha256(canonical_outcomes)
```

A validator fetching outcomes checks `sha256(fetched_outcomes)` against the
on-chain committed hash and **rejects on mismatch**. This guarantees every
validator scores against the *same* immutably-anchored outcome set — the
operator cannot quietly change outcomes after the fact or serve a different set
to different validators. (See `verify_outcome_commitment` in the validator
client.)

## The one thing you still trust — and the roadmap to shrink it

The measured outcomes derive from **private Google Ads account data only the
operator holds**, so a validator cannot *recompute* them — it trusts that the
operator's measurement is **honest**. The signature + on-chain commitment make
the outcomes attributable, immutable, and identical for everyone, but they do
not by themselves prove the measurement is truthful.

We are explicit about this. Mitigations, in order of what's shipped vs planned:
- **Shipped:** signed outcomes + on-chain `scoring_inputs_hash`/`final_score_root`
  + independent re-derivation (`verify_epoch.py`).
- **Shipped (P4b):** on-chain canonical outcome-set commitment (anti-tamper /
  anti-cherry-pick / one-set-for-all).
- **Roadmap:** multiple independent outcome attestors and/or attested
  (TEE/ZK) outcome computation, so the measurement itself becomes verifiable
  without revealing private advertiser data.

If you want to audit a specific epoch end-to-end, reach out — we'll walk through
`verify_epoch.py` against that epoch's chain commits with you.
