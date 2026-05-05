# SN21 Whitepaper

**Verifiable prediction markets for Google Ads campaign outcomes.**

> Anyone can submit a prediction. Anyone can verify it was scored
> honestly. The chain is the source of truth.

---

## Launch status (read first)

| Item | Status | Where |
|---|---|---|
| Mainnet netuid | **21** (Bittensor `finney`) | Subnet registration |
| Testnet netuid | **466** (Bittensor `test`) | Phase 0 + integration probes |
| Current phase | Pre-mainnet — testnet validation complete | §14.3 |
| Validator registration | **Closed at launch.** Operator runs primary + shadow. Opening is on the Review 4 agenda | `docs/SN21_REWARD_MECHANISM.md` |
| Miner registration | Open on testnet 466 today; mainnet 21 opens when launch announces | `docs/miner_quickstart.md` |
| Public verifier | **Live at launch.** `scripts/verify_epoch.py` runs chain reads, `inner_sig` checks, IMT root recomputation, AND end-to-end score recomputation via the production `score_one_miner` adapter. Pass `--truth-file` (derived from the 9.A.2 reveal blob) for full score reproduction. Recorded-epoch fixture under `tests/fixtures/recorded_epoch/` proves `ok=true` round-trip | `tests/unit/scripts/test_verify_epoch_live_scorer.py` |
| Weights ↔ scoring binding | **Operational at launch + verifier-side cross-check live.** Verifier compares chain weights at `weights_commit_block_hash` against weights re-derived from the score table; mismatched UIDs are surfaced. The chain-side anchor (32-byte field in `WeightsTlockPayload`) is upstream Bittensor RFC, tracked in `docs/proposals/q26_weights_payload_anchor.md` | §13.1 + adversarial test |
| Per-episode artifacts | **Live at launch.** Miners that pass `per_episode_entries` to `submit_miner_epoch` ship the bundle to archives, bind its IMT root via `episodes_root` and SHA via `episodes_bundle_sha256`. Aggregate-per-horizon path is preserved for miners that have not yet adopted Phase E | §13.2 |
| Reward mechanism | **Tiered allocator live at launch.** `TieredAllocator` enforces participation gate, four-epoch EMA tier placement, Elite/Competitive/Participating pool shares, Elite-floor redistribution, and the <15-miner single-pool fallback. Wire it via `WeightSetter(tiered_allocator=TieredAllocator())`; the legacy score-normalization path remains as the documented fallback | `docs/SN21_REWARD_MECHANISM.md`, `hope/validator/tiered_weights.py` |
| Conditional-prior baseline | **Live at launch.** The release artifact's `scoring_metadata.conditional_prior` per episode plumbs through `ScoringMetadata` and `SkillScoreCalculator.compute_baseline_prediction(...)`. Episodes with no published prior fall through to predict-zero — no crash, no silent gate-zeroing | §11 |

When in doubt about a claim in the rest of this paper, this table is the
authoritative read on what is shipping at launch versus what is
described as the target architecture.

**Conventions used below:**
- *Launch* = behavior available the day mainnet opens.
- *Migration* = behavior currently scaffolded; will land before launch.
- *Roadmap* = explicitly future, marked as such.

---

## 0. How this was built

This document describes a protocol built between 2026-04-30 and
2026-05-04 — five days of intense work — with significant authoring
assistance from an AI coding agent. We are disclosing this up front
for two reasons.

First, anyone reading the codebase will see the agent's fingerprints
(the agent wrote large amounts of the test scaffolding and the
docstrings verbatim). Pretending otherwise would be insulting to
readers who can just open the commit history and see.

Second, the work that mattered most was NOT the code the agent
generated. It was:

- **Choosing what to build.** The original SN21 design had validators
  grading their own homework. The architecture in this paper is what
  came out of choosing, in successive iterations, which trust
  assumptions to remove and what cryptographic primitives to use to
  remove them. The agent did not pick those primitives; it composed
  them once we'd chosen.

- **Hitting the chain and finding the wall.** Phase 0 surfaced four
  empirical surprises that would have invalidated a paper-only
  architecture: drand library wheel mismatch (Q34), commit fee on
  testnet (Q13), MaxSpace per-window byte cap (Q11), and most
  importantly the chain auto-decrypt format bug (H-3 → H-6). Each one
  required reading the chain runtime source, hypothesizing why our code
  failed, and testing the hypothesis on real testnet. This is the work
  that's hardest to outsource and easiest to spot in retrospect.

- **Deciding what to NOT build.** The architecture is sophisticated.
  We used that sophistication budget on the bindings that matter
  (inner_sig, AAD, IMT roots, three-tier archives) and rejected
  complexity that didn't pay (multi-field commit, decoy submissions,
  zk-proofs of scoring). The "what we deliberately defer" section
  (§13) is the artifact of that judgment.

- **Running the probes.** Every claim in this paper backed by a
  number — "auto-decrypt fires in 105 seconds," "MaxSpace is 1,259
  blocks," "9.C.1 plaintext fits in 380 bytes" — comes from a probe
  somebody manually ran against testnet 466. The phase-by-phase
  narrative is in `docs/build_journey.md`; the JSON probe outputs
  are signed implicitly by the chain extrinsic hashes they reference.

The whitepaper itself was iterated by the agent given the design we'd
locked in. We've reviewed every paragraph; the receipts (commits,
probe outputs, test files) are linked at every claim.

If the question is "do you understand this solution?" — Section 4
walks through an epoch end-to-end, Section 7's worked example shows
which defense fires for which attack, Section 12 documents the
edge cases we built around, and Appendix B records the empirical
checks.

If the question is "could you rebuild this without the agent?" —
slower, yes. The agent's role was the keyboard; the architecture
decisions came from human judgment, and the empirical validation
came from running real chain extrinsics that no agent could
autonomously authorize.

---

## 1. The problem we're solving

Advertisers spend trillions of dollars on Google Ads each year. The
algorithms that decide which auction to enter, which bid to place, and
which audience to target are run inside a black box. When an advertiser
sees a bad week of performance, they cannot tell whether the cause was a
genuine market shift or a quietly-changed Google policy.

Existing prediction services try to fill this gap. They train models on
performance data and forecast what will happen next quarter. The
problem with these services is that **they grade their own homework**:

- The vendor predicts a number.
- The vendor measures the outcome.
- The vendor reports how accurate they were.

There is no way for the advertiser to verify that the prediction sold to
them last month was the prediction the vendor actually made. The
prediction lives in a private database. The vendor can quietly improve
yesterday's record before today's customer asks about it.

**The fundamental issue is that prediction and grading are bundled in
the same trust domain.** The vendor is judge, jury, and historian.

SN21 unbundles them. Predictions are committed on a public blockchain
before outcomes are measured. Outcomes are committed too. Scoring is a
deterministic function of public chain state. Anyone can recompute it.
The vendor cannot retroactively improve their record because the record
isn't theirs to edit.

### 1.1 What this gives you

- **Cryptographic accountability.** Every prediction is signed by the
  miner that produced it and recorded on chain before the outcome is
  known. A miner cannot deny submitting a prediction, and no one can
  fabricate a prediction in their name.
- **Independent re-scoring.** Anyone running the verifier can reproduce
  the validator's scoring decisions from chain state and archived
  ciphertexts. Disagreement is publicly auditable.
- **Open competition.** Miners are anonymous Bittensor neurons. Whoever
  predicts best earns the most weight. There is no vendor lock-in
  because there is no vendor.

### 1.2 What this is NOT

It is not a sportsbook. It is not a synthetic prediction market with
play-money tokens. It is a real-money mechanism (Bittensor's TAO
emissions) that pays for accurate forecasts of real Google Ads
performance.

It is also not a product you buy access to. It is a public protocol.
Validators run software that reads the chain. Anyone who wants
predictions can read the chain too. The economics are wholesale; the
audit surface is retail.

### 1.3 Outcomes are released in arrears (important)

A reasonable reading of "predict the next 7 days" is that miners predict
some live future and the operator measures it as it happens. **That is
not how SN21 works.**

Each epoch is built from a fixed historical window of Google Ads data
that has already finished playing out at the moment the epoch is
released. Concretely:

- The pre-window (account state, time series, action context) covers
  days `[T-60, T-1]` for some past anchor date `T`.
- The action being predicted occurred on day `T`.
- The 7-day and 14-day outcomes — what we ask miners to predict —
  cover `[T+1, T+7]` and `[T+1, T+14]`. Those windows are also in the
  past. The operator already knows what happened.

The miner is predicting a withheld historical outcome, not a live future
one. From the protocol's point of view this is identical (the miner
sees only the pre-window and action; outcomes are sealed until reveal),
but it has three concrete consequences:

1. **Outcomes do not change between release and reveal.** The release
   package, the reveal blob, the salts, and the baseline values are
   content-addressed and committed on chain. Nothing is re-measured.
2. **There is no "live data" trust assumption.** A skeptical
   participant does not need to trust that the operator is measuring
   live Google Ads honestly between T+1 and T+14. By the time an epoch
   opens, T+14 has already happened; the outcome blob exists on the
   operator's side; what the chain commits is the SHA-256 of bytes
   that already exist.
3. **The "commit-then-serve" gate is what enforces fairness.** The
   reveal blob is published on the operator's HTTPS endpoint **only
   after** the chain commit (9.A.2) is finalized. Until that block is
   final, no participant — including the operator — can serve a
   different blob to anyone without contradicting the on-chain hash.

What stops the operator from re-measuring the outcomes after seeing
miner predictions and choosing different ground truth? The chain
commit at T=0 (9.A.1, `release_commit_digest`) pins the EPISODE SET
and its query hashes. The chain commit at deadline (9.A.2) pins the
OUTCOME BYTES. Between those two commits the operator publishes
predictions that are themselves chain-anchored (9.B). Re-measuring
outcomes after seeing predictions would force the operator to break
the 9.A.2 hash that has already been committed before the predictions
were even revealed by the chain auto-decrypt.

The protocol does not prevent the operator from picking arbitrarily
favorable historical episodes for an epoch. It does prevent the
operator from rewriting outcomes after seeing predictions, which is
the trust gap the protocol was built to close.

---

## 2. Two core guarantees

The protocol gives two cryptographic guarantees, plain English first:

### Guarantee A — Predictions are bound to the miner that produced them

When you read a miner's prediction off the chain, you can prove three
things mechanically:

1. The bytes were committed by THAT miner's hotkey at THAT block.
2. Nobody altered them after the fact, including the validator.
3. The prediction was made BEFORE the outcome was knowable.

These are not policy claims; they are checks anyone can run.

### Guarantee B — Scoring is a deterministic function of public state

When you read a validator's score for a miner, you can prove four
things mechanically:

1. The validator used the on-chain prediction we already verified.
2. The validator used the on-chain outcome the operator published.
3. The scoring algorithm is the published one — its hash is committed.
4. Two independent verifiers will reach the same score.

If a verifier reaches a different score, exactly one of {validator,
verifier} is wrong, and the disagreement is publicly auditable.

These two guarantees are the entire trust model. Every other piece of
the architecture — the timelock encryption, the Merkle trees, the
shadow validator, the three-tier archive — exists to enforce them.

---

## 3. Vocabulary

You can read this paper without the glossary, but the glossary makes
later sections shorter.

| Term | Meaning |
|---|---|
| **Operator** | The party that operates the protocol. Publishes episodes (questions to predict) and outcomes (measured ground truth). |
| **Episode** | A single prediction challenge. "What will the cost-per-conversion of this campaign be over the next 7 days?" |
| **Epoch** | A batch of episodes released together. Typically 4-24 hours from open to deadline. |
| **Miner** | A Bittensor neuron that predicts outcomes. Anonymous, registered by hotkey. |
| **Validator** | A Bittensor neuron that scores predictions and submits weights. The operator runs the primary; a shadow runs in parallel. |
| **Prediction** | The miner's answer for one episode and one horizon: P10/P50/P90 quantiles + goal-miss probability + instability risk. |
| **Outcome** | What actually happened: cost delta, conversion delta, efficiency delta, did the goal miss. |
| **Commit-reveal** | A two-step protocol: post a hash now, post the bytes that hash to it later. Standard cryptographic pattern. |
| **TLE (timelock encryption)** | Encryption that can only be decrypted after a specific drand round. The chain auto-decrypts on schedule. |
| **drand quicknet** | The League of Entropy's randomness beacon. Emits a signature every 3 seconds; signatures are the keys for TLE decryption. |
| **IMT (indexed Merkle tree)** | A sorted Merkle tree with low/high pointers. Supports both inclusion AND non-inclusion proofs cheaply. |
| **AAD (associated data)** | Extra bytes mixed into AES-GCM encryption. Binds ciphertext to a context (here: epoch_id) so it can't be replayed elsewhere. |
| **Shadow validator** | A second validator the operator runs that scores independently. Its commits are diffed against the primary's; mismatches are publicly auditable. |
| **MaxSpace** | A subtensor rate limit: ~3,100 bytes of commits per (netuid, hotkey) per ~4-hour window. |
| **Tempo** | A subnet's epoch length in chain blocks. Testnet 466 = 360 blocks ≈ 72 minutes. |

We will introduce one more term — `inner_sig` — when we get to it.

---

## 4. How an epoch runs (narrative)

Three characters: **Hannah** the outcome signer, **Miner Mike** the
prediction model operator, **Validator Vera** the scoring honest party.
A fourth, **Adversary Adam**, will join in §10.

### T = 0 — Episodes go out

Hannah builds the epoch's episodes from real Google Ads data. She picks
182 campaigns. For each, she records a question — "what will the
cost-per-conversion be 7 days from now?" — and the inputs the miners
will see.

She does NOT yet measure the outcomes. Outcomes don't exist yet; the
7-day window starts when the epoch opens.

She then constructs a `release_commit`: a small CBOR map containing the
epoch ID, the round of drand at which the epoch opens, the hash of each
episode's query, and her own ed25519 public key. She signs it (the
`inner_sig`), then publishes its BLAKE2b-256 hash on chain as a
`Sha256` commit. The full plaintext goes to a public HTTPS URL —
**only after** the chain commit is finalized.

The chain commit pins the rules. After T = 0, Hannah cannot change
which campaigns are in scope or what their queries are. She cannot
even quietly add an extra campaign. The hash on chain is the witness.

### T = 0 → deadline — Miners predict

Miner Mike runs his prediction model. He generates P10/P50/P90 quantile
forecasts for each (campaign × horizon) — say, P50 cost goes down 3.7%
over 7 days, with the P10/P90 band at -8.5% to +1.2%.

To submit, Mike does five things:

1. Builds a CBOR map of his predictions, plus his ed25519 public key
   and the epoch ID.
2. Signs the CBOR with his ed25519 private key (the `inner_sig`).
3. Generates a fresh 32-byte AES key K. Encrypts the CBOR with K
   under AES-GCM, mixing the epoch ID into the AAD so the ciphertext
   only decrypts under THAT epoch.
4. Uploads the AES ciphertext to two archive endpoints — the
   operator's long-retention server and his own.
5. Submits THREE commits on chain from his miner hotkey:
   - **TimelockEncrypted(K)** — the AES key, encrypted with drand TLE
     to a future round (typically deadline + 100 rounds = ~5 minutes).
   - **Sha256(AES_ct)** — the SHA-256 of the AES ciphertext.
   - **Raw(self_archive_url)** — the URL where his archive serves AES_ct.

Step 3 binds the ciphertext to the epoch. Step 5 binds the SHA, the
key, and the URL all to Mike's hotkey on chain. Steps 2 and 5 together
mean: nobody can produce a different prediction with valid signatures
except Mike. Even if Vera is malicious, even if she controls the
archives, she can't forge Mike's signed CBOR.

### T = deadline + δ — Outcomes measured

The 7-day window has now passed. Hannah measures the actual outcomes —
cost change, conversion change, did the goal miss — and packages them
into a `reveal_blob` JSON. She signs the blob, then submits a
`Sha256(reveal_blob)` commit on chain. After that commit finalizes,
the blob goes to her HTTPS endpoint.

The order matters. If Hannah served the blob before the chain commit,
she could serve different blobs to different validators. The chain
commit ensures that whatever blob anyone sees, it is the SAME blob.

### T = deadline + δ + ε — Validator scores

Vera now has everything she needs:
- The on-chain `release_commit` digest (rules of the epoch).
- The on-chain `reveal_blob_hash` (with the blob fetchable from
  Hannah's HTTPS).
- For each miner, three on-chain commits and an off-chain ciphertext.

The drand round of each miner's K commit has by now passed. The chain
has auto-decrypted K and stored it in `Commitments::RevealedCommitments`.

Vera reads each miner's K from chain. She fetches their AES_ct from
the archive (any tier; she trusts none of them and verifies the
SHA-256 against what's on chain). She decrypts AES_ct with K, with
the epoch ID as AAD. She gets the miner's CBOR plaintext.

She runs **the eight-check scoreability rule** (§7) on each miner.
Failed checks → that miner is excluded from this epoch. Passed checks
→ Vera scores the prediction against the outcomes.

Then Vera does her OWN three-step commit:

1. **Pre-scoring state (TimelockEncrypted)** — the IMT root over all
   miners' (block, K-round, sha256_ct) tuples; the hash of excluded
   miners; the round at which she fetched the outcomes. Signed by Vera.
2. **Weights commit** — through the standard subtensor
   `commit_timelocked_weights` extrinsic. This is what Yuma consensus
   reads to award TAO emissions.
3. **Post-scoring artifacts (TimelockEncrypted)** — the IMT root over
   each miner's final score; a hash of the scoring inputs; and the
   block hash where her weights commit landed. Signed by Vera.

Vera's signatures (`inner_sig`) bind the artifacts to her hotkey.
If a malicious second validator later copies Vera's chain plaintext
into its own storage slot, the inner_sig won't verify against the
second validator's hotkey — the rubber-stamp attack falls flat.

### Anyone can verify

Now a third party — an advertiser, an auditor — runs:

```bash
python scripts/verify_epoch.py \
    --epoch-id WR-2026-W18-PUB-E1 \
    --validator-hotkey 5GutpW22DLSvG9uM3vUEobGyGqck8ioctbGry8m3Wm2nkHKj \
    --tier-2-base https://archive.example.io \
    --block-hash 0x<the block where Vera's 9.C.2 landed>
```

The verifier:
1. Reads Vera's pre/post-scoring CBOR via block-pinned chain query.
2. Verifies Vera's `inner_sig` against her chain hotkey.
3. For each miner, repeats Vera's read + decrypt + scoreability + score.
4. Builds its OWN IMT root over the resulting per-miner scores.
5. Compares its root to the chain-anchored root in Vera's 9.C.2.

Match → the validator is honest, full stop. Mismatch → exactly one of
{Vera, the verifier} has a bug or is malicious, and the divergence is
publicly auditable.

> **Status (launch).** All five steps are live. Step 3 calls
> `score_one_miner` from `hope.scoring.onchain_adapter` — the same
> adapter the production validator runs. Pass `--truth-file <path>`
> built from the 9.A.2 reveal blob and the verifier reproduces miner
> scores end-to-end. The recorded-epoch fixture in
> `tests/fixtures/recorded_epoch/` is proved `ok=true` by
> `tests/unit/scripts/test_verify_epoch_live_scorer.py`, and a
> regression guard test fails the build if a placeholder scorer is
> ever reintroduced into `scripts/verify_epoch.py`. The verifier also
> cross-checks `actual_weights_at_commit_block` against weights
> re-derived from the score table — see §13.1.

---

## 5. Seven chain anchors

Every binding moment in the protocol is a chain commit. Here is the
exhaustive list.

| # | Layer | Who | What | Variant | Purpose |
|---|---|---|---|---|---|
| 1 | 9.A.1 | Operator | release_commit_digest | Sha256 | Pin epoch rules at T=0 |
| 2 | 9.A.2 | Operator | reveal_blob_sha256 | Sha256 | Pin measured outcomes at T=deadline+δ |
| 3 | 9.B.K | Miner | TimelockEncrypted(K) | TimelockEncrypted | The miner's AES key, auto-decrypted by chain at reveal_round |
| 4 | 9.B.s | Miner | Sha256(AES_ct) | Sha256 | Bind off-chain ciphertext to chain |
| 5 | 9.B.u | Miner | self_archive_url | Raw{N} | Tier-3 archive location |
| 6 | 9.C.1 | Validator | pre_scoring_state | TimelockEncrypted | IMT root over miner commits + outcome-fetch round |
| 7 | 9.C.3 | Validator | weights | (subtensor) | Yuma weights via `commit_timelocked_weights` |
| 8 | 9.C.2 | Validator | post_scoring_artifacts | TimelockEncrypted | IMT root over scores + scoring-inputs hash + weights block hash |
| 9 | 9.C.6 | Validator | retry_log_blob_sha256 | Sha256 | Only when ≥1 miner excluded for plaintext_unavailable |

(Yes that's nine. We say "seven" colloquially because 9.B is naturally
counted as one layer. Pedants and protocol implementers should count nine.)

The chain footprint is tight. A miner's epoch costs ~2,174 bytes against
a 3,100-byte MaxSpace; a validator's costs ~1,960 bytes (2,492 with
the optional retry log). Both fit in one rate-limit window.

---

## 6. Cryptographic primitives, intuitive depth

This section is the protocol's load-bearing crypto, explained at the
depth a curious reader can follow. The implementation lives in
`hope/commitment/`; every claim in this section maps to a function in
that package and a unit test in `tests/unit/commitment/`.

### 6.1 Canonical CBOR

We use [RFC 8949 §4.2.1] canonical CBOR for every byte that gets
hashed or signed. Canonical means: definite-length items, sorted
keys, smallest-form integers, no indefinite-length anything. The
`cbor2.dumps(..., canonical=True)` library call already does this.

The point: two implementations encoding the same map produce
byte-identical output. So when we hash the encoded bytes, both
implementations get the same hash. Without canonicalization, two
implementations could produce different hashes for "the same" map and
the protocol would silently break.

### 6.2 AES-GCM with epoch-bound AAD

Each miner generates a fresh 32-byte K per epoch. They encrypt their
prediction CBOR with K under AES-256-GCM. The AAD is the literal
string `b"sn21-prediction-v1:" + epoch_id`.

Why AAD? Without it, AES-GCM gives confidentiality and integrity of the
ciphertext but doesn't bind the ciphertext to a context. A malicious
validator with a copy of K could try to play it under a different
epoch_id. With AAD, the chain enforces "this ciphertext only decrypts
under THAT epoch."

### 6.3 ed25519 inner_sig

Both miners and validators carry an ed25519 keypair, separate from
their Bittensor SS58 hotkey. The public key is registered on chain
once (a 109-byte `Raw{N}` commit binding hotkey ↔ ed25519_pk).
Subsequent prediction / scoring commits sign their content with the
ed25519 key.

The inner_sig is computed over `blake2b_256(canonical_cbor(plaintext
sans inner_sig))`. The trick: the plaintext carries the public key
inside it AND the chain storage account is the writer's hotkey.
Verifying inner_sig means checking BOTH that the signature is valid
for the public key AND that the public key matches the chain-anchored
binding.

This kills the rubber-stamp attack. A second validator can copy a
primary's plaintext bytes into its own storage slot, but the
inner_sig in those bytes verifies against the PRIMARY's hotkey, not
the second's. A verifier comparing the second's chain account against
the inner_sig's hotkey field finds the inconsistency immediately.

### 6.4 drand TLE

Drand quicknet is a randomness beacon operated by the League of
Entropy. Every 3 seconds it publishes a BLS signature for round N. The
signature for any future round is unknowable until that round.

Drand TLE encrypts a message such that it can only be decrypted with
the round-N signature. Submit a TLE'd ciphertext at time T; the
ciphertext is opaque until round-N's signature drops; then anyone can
decrypt it.

Subtensor has a built-in feature: when you submit a `TimelockEncrypted`
commit with a `reveal_round` field, the chain automatically decrypts
and stores the plaintext after that round fires. We use this for the
miner's K (which auto-reveals after the miner deadline) and for the
validator's 9.C.1 / 9.C.2 (which auto-reveal a configurable delay
later, typically 1-2 hours).

A subtle point we learned the hard way, narrated because it's instructive:

We initially used `bittensor_drand.encrypt(bytes, n_blocks, block_time)`.
It returns `(ciphertext_bytes, reveal_round)`, takes binary input, looks
like the obvious choice. The chain accepted our `TimelockEncrypted`
extrinsics with `success=True` returned. We assumed it worked.

It didn't. The H-3 probe (2026-05-04) submitted a known 32-byte
plaintext, polled `RevealedCommitments` for 30 minutes past the
reveal_round, observed nothing. We had assumed `success=True` meant
auto-decrypt would fire; in fact the chain runtime silently drops
ciphertexts whose format it doesn't recognize.

H-4 was diagnosis: read the SDK source, find that `Subtensor.set_reveal_commitment(...)`
calls a DIFFERENT C function — `bittensor_drand.get_encrypted_commitment(data: str, ...)`
— and that's what the chain runtime decodes. The two functions look
identical at the Python signature level; their underlying ciphertext
formats are not.

H-6 was the fix: hex-encode binary plaintext to a string, call the
right C function, halve the budget for plaintext (since hex doubles
the byte count). We reran the round-trip on testnet:

```
plaintext (hex):    0xc0ffee64284082626c6ebdf1b074c9afdeadbeef
submit:             success=True reveal_round=28367904
auto-decrypt fired: block 7049220, 105 seconds after submission
decode round-trip:  byte-exact match
```

That was the proof. Until we'd run it on real chain, the protocol was
running on an assumption that turned out to be false. The build
journey (`docs/build_journey.md`, Phase H) records the H-3 → H-6
arc; the test suite encodes the fix in
`tests/unit/commitment/test_on_chain.py`.

The lesson generalizes: *"the chain accepted the extrinsic"* is not the
same as *"the chain processed the extrinsic correctly."* Substrate
storage is permissive about what gets written; the runtime's
interpretation is where the work actually happens. Every claim in this
paper that depends on the chain processing something correctly has
a probe behind it.

### 6.5 Indexed Merkle trees

A standard Merkle tree lets you prove "X is a leaf of this tree." An
indexed Merkle tree (IMT) goes further: every leaf carries a
`(key, value, next_key)` triple, where `next_key` points to the
next-larger key in sorted order. With this structure you can also prove
"X is NOT a leaf of this tree" — exhibit a leaf whose key is less than
X and whose next_key is greater than X.

We use IMT roots for two things:

- **Miner commits root** (in 9.C.1): leaves are
  `(miner_hotkey, blake2b(k_block || k_round || sha256_ct))`. Any
  verifier can check whether a given miner's chain commit was
  considered by the validator at scoring time.
- **Final score root** (in 9.C.2): leaves are
  `(miner_hotkey, score_micro)`. Same check, but for the score the
  validator awarded.

The IMT root is 32 bytes. Inclusion + non-inclusion proofs are
~16 hashes (2,048 bits) per query. Cheap to compute, cheap to verify.

---

## 7. The eight-check scoreability rule

When validator Vera reads a miner's three on-chain commits and the
archived AES_ct, she runs eight checks before scoring. Failing any
check means that miner is excluded from the epoch with a discrete
reason. The checks are:

| # | Name | What it catches |
|---|---|---|
| 1 | `ON_CHAIN_PRESENT` | Miner missing one of the three commits → exclude |
| 2 | `CIPHERTEXT_MATCH` | SHA-256(AES_ct) doesn't match the chain commit → archive served wrong bytes |
| 3 | `AAD_BIND` | AES-GCM decrypt fails under epoch AAD → cross-epoch replay or wrong K |
| 4 | `CANONICAL_ENCODING` | Decrypted CBOR doesn't round-trip canonically → malformed input |
| 5 | `INNER_SIG` | inner_sig invalid OR doesn't match chain hotkey → forgery / wrong miner |
| 6 | `EPOCH_MATCH` | Plaintext epoch_id ≠ epoch being scored → splicing different epoch |
| 7 | `HORIZON_SHAPE` | Quantiles non-monotone, probabilities outside [0,1] → malformed prediction |
| 8 | `TIMING_BOUND` | submitted_round outside [open, deadline] OR k_block outside chain window → late or early |

The order is deliberate. ON_CHAIN_PRESENT short-circuits without
fetching the archive. CIPHERTEXT_MATCH short-circuits the decrypt.
AAD_BIND short-circuits the canonical check. The cheap, deterministic
checks run first; the more expensive ones only on what's left.

Every reason is a discrete enum. The validator publishes the list of
excluded miners (with reasons) off-chain; only its SHA-256 goes in
9.C.1. Verifier replays each check and confirms exclusion-vs-scored
state matches.

### 7.1 Worked example — Adversary Adam tries to rewrite Mike's prediction

Adam controls the validator. Mike has submitted his epoch as in §4.

**Attempt 1:** Adam decrypts AES_ct using K from chain, edits the CBOR
to replace Mike's P50 cost forecast with a much worse one, re-encrypts
under K, and serves the new bytes from his archive.

Result: check 2 (`CIPHERTEXT_MATCH`) fails. The on-chain
`Sha256(AES_ct)` was committed by Mike's hotkey BEFORE Adam saw the
plaintext. Adam can't retroactively replace it. The new ciphertext's
SHA-256 disagrees. Mike is excluded with `ciphertext_match.fail`.

**Attempt 2:** Adam re-uses Mike's ORIGINAL ciphertext but submits a
different K under his own hotkey, hoping verifiers will use his K.

Result: chain storage at (netuid, miner_hotkey) belongs to Mike, not
Adam. Adam's K is at (netuid, Adam_hotkey). Verifier reads Mike's slot,
gets Mike's K, decrypts to Mike's original prediction. No effect.

**Attempt 3:** Adam picks a different epoch's old AES_ct from Mike that
Adam knows the K for, and serves that under Mike's new sha256_ct slot.

Result: check 2 fails because the SHA-256 doesn't match. If Adam
ALSO somehow rewrote the chain commit (impossible by Substrate's
storage authorization), check 3 (`AAD_BIND`) would fail because the
AAD includes the current epoch_id; an older ciphertext was bound to a
DIFFERENT epoch_id and won't decrypt under the new one.

The eight checks are not ad-hoc. They form a chain of bindings:
`(miner hotkey) ⇒ (chain commits) ⇒ (archive bytes) ⇒ (decrypted CBOR)
⇒ (signed prediction)`. Break any link and the chain falls apart, and
verifiers see exactly which link broke.

---

## 8. Three-tier archive durability

The chain stores ~32 bytes per AES ciphertext (its SHA-256). The
ciphertext itself — typically 350-400 bytes per miner per epoch — lives
off chain, in a three-tier archive system.

| Tier | Operator | Path | Retention | Purpose |
|---|---|---|---|---|
| 1 | Each validator | `archive.{validator_url}` | Until 9.C.2 reveals + 7 days | Fast local cache for scoring |
| 2 | Subnet operator | `archive.example.io` | ≥ 90 days | Long retention; geographically replicated |
| 3 | Each miner | URL declared in chain commit | Best-effort | Fallback if Tier-1/2 lose bytes |

A validator looking up Mike's AES_ct at scoring time tries Tier-1
first (fastest), then Tier-2, then Tier-3. Each fetch verifies the
returned bytes' SHA-256 against the chain commit before accepting them.
A malicious archive returning wrong bytes is detected immediately and
the validator falls through to the next tier.

If ALL three tiers fail to serve a SHA-matching ciphertext, the miner
is excluded with reason `plaintext_unavailable`. The validator then
emits a Layer 9.C.6 retry log — a JSON blob recording each archive
attempt (URL, status code, elapsed time, SHA match) — and submits the
blob's SHA on chain. A verifier rerunning the audit can reproduce the
same fall-through and confirm the exclusion was honest.

The point of three tiers is fault tolerance under any 1-of-3 outage,
not theatrical redundancy. If the operator's archive server goes down,
miners who self-archive still get scored. If a miner fails to
self-archive, the operator's tier covers them. If both fail
simultaneously for the SAME miner in the SAME epoch, that miner's
prediction can't be retrieved and the protocol records the failure
publicly.

---

## 9. Validator architecture: primary + shadow

A single validator scoring all miners is a weakness, no matter how
honest. Validator Vera could be subtly wrong, or compromised, or
strategically dishonest in a way that's invisible to a single
verifier checking only her work.

The architecture defends against this via a **shadow validator**, a
parallel operator-run process running on a separate hotkey, separate
host, ideally separate cloud provider. The shadow:

1. Reads the same chain state Vera reads.
2. Runs the same scoreability rule.
3. Runs the same scoring algorithm.
4. Submits its OWN 9.C.1 / 9.C.2 / 9.C.3 / 9.C.6 commits — under the
   shadow's hotkey, signed with the shadow's ed25519 key.

Now there are two independent records on chain. A verifier checks
both. If primary and shadow agree, both are honest (or both are
identically wrong, which is a much harder feat for an adversary).
If they disagree, the verifier can attribute the divergence: one of
them got which fact wrong is recorded byte-for-byte.

### 9.1 The rubber-stamp attack and why it doesn't work

A naive shadow could just copy primary's chain bytes into its own
storage slot. Substrate authorizes the WRITER, not the CONTENT, so
the shadow could write whatever bytes it wants. A surface-level
auditor sees "primary committed X; shadow committed X; consensus."

The protocol blocks this in two ways:

- The plaintext carries the validator's ed25519 public key.
- The inner_sig is over `blake2b(canonical_cbor(plaintext sans inner_sig))`,
  using that ed25519 key.

If a shadow copies primary's bytes verbatim, the inner_sig still
verifies — but it verifies against the PRIMARY's hotkey, not the
shadow's. The verifier's check is: "does plaintext.validator_hotkey
match the chain account that wrote this slot?" For a rubber-stamping
shadow, NO — primary's hotkey is in the plaintext but the writer is
shadow.

The shadow has only one way to produce a chain commit that passes
inner_sig verification under its own hotkey: actually run the scoring
itself and sign the result. Which means the shadow's commit IS an
independent computation, by construction.

This is the SH-5 defense, proven by a unit test in
`tests/adversarial/test_attack_surface.py`:

```python
def test_shadow_using_primary_plaintext_fails_under_shadow_slot():
    # Shadow blindly copies primary's plaintext into its own storage slot.
    # Verifier checks inner_sig against shadow's chain account → must fail.
    assert not verify_inner_sig(
        primary_plain, shadow_pk, hotkey_field="validator_hotkey"
    )
    # ...while it still verifies under primary's hotkey.
    assert verify_inner_sig(
        primary_plain, primary_pk, hotkey_field="validator_hotkey"
    )
```

### 9.2 What the shadow doesn't fix

The shadow is an operational defense, not a cryptographic one. If a
single operator controls both primary and shadow, that operator can
collude with itself. The architecture mitigates this with three layers:

1. **Phase 1 (current)**: operator primary + operator shadow, hosted
   separately, ideally with audit-trail separation.
2. **Phase 2**: operator primary + INDEPENDENT third-party shadow (a
   contracted audit firm or a validator-as-a-service operator) with a
   different operator key.
3. **Phase 3+**: External validators register on netuid 21
   organically. Yuma stake-weighted median consensus naturally clips
   any 1-of-N malicious actor.

The shadow buys us cryptographic-level defenses against
single-validator dishonesty. It does NOT buy us protection against
the operator-as-an-organization being dishonest. That requires Phase
3's external validators or, ultimately, an upstream chain runtime
change (see §13).

---

## 10. Adversarial defense matrix

Here is the explicit list of attacks the protocol claims to defeat,
with the defense and the test that proves it. (`tests/adversarial/`
contains 12 scenarios; 11 fire correctly, 1 is a structural proof.)

| Attack | Defense | Test |
|---|---|---|
| Validator rewrites miner predictions | inner_sig invalid after re-encrypt | `test_rewriting_horizons_breaks_inner_sig` |
| Cross-epoch ciphertext replay | AES-GCM AAD = `b"sn21-prediction-v1:" + epoch_id` | `test_replaying_aes_ct_from_other_epoch_fails_aad` |
| Tampered AES_ct in archive | Chain `Sha256` commit anchors integrity | `test_byte_flip_in_archive_caught_by_sha` |
| Tampered K bytes | AES-GCM tag verification fails | `test_wrong_k_fails_aes_decrypt` |
| Forged miner identity | inner_sig hotkey field bound to chain account | `test_other_hotkey_fails_inner_sig` |
| Late submission | TimingBounds (drand round + chain block) | `test_late_submission_rejected` |
| Early submission | Same | `test_early_chain_block_rejected` |
| Rubber-stamp shadow | inner_sig validator_hotkey ≠ chain account | `test_shadow_using_primary_plaintext_fails_under_shadow_slot` |
| Registration forgery | ed25519 sig binds (role, ss58, pk) | `test_attacker_cannot_bind_victims_key` |
| Malicious archive serves wrong bytes | SHA-256 mismatch caught | `test_archive_serving_other_miners_bytes_caught` |
| Tampered episode entry | IMT root mismatches `episodes_root` | `test_tampered_entry_breaks_root` |
| Tampered episode entry (sig replay) | inner_sig digest mismatches | `test_tampered_entry_breaks_inner_sig` |

Every test is reproducible in 5 seconds: `pytest tests/adversarial/ -v`.

We cannot stop a determined operator-as-an-entity from being
dishonest; we can only make dishonesty publicly auditable and
economically painful (via Yuma stake-weighted clipping). The 12 tests
cover the cryptographic surface. The economic and operational
defenses live in the next section.

---

## 11. Economic model

SN21 is a Bittensor subnet. The token economics are inherited from the
chain, not invented by us.

### 11.1 How TAO flows

- Subnet 21 is registered on the Bittensor mainnet. The subnet's stake
  determines its share of network-wide TAO emissions.
- Within the subnet, validators submit weights for each miner via
  `commit_timelocked_weights`. Weights are uint16 ratios summing to 1.
- Yuma consensus aggregates validator weights using stake-weighted
  median: each miner's effective weight is the median of all
  validators' weights, weighted by validator stake.
- Emissions are split:
  - 18% to the subnet creator.
  - 41% to validators (in proportion to their stake).
  - 41% to miners (in proportion to their consensus weight).

There is no synthetic token. There is no play-money market. Miners
who predict accurately receive a share of real TAO emissions;
miners who don't, don't.

### 11.2 What weights are based on

A miner's score combines four signals (per
`hope/scoring/onchain_adapter.py:score_one_miner`):

- **Quantile accuracy**: CRPS-style distance between the miner's
  P10/P50/P90 and the measured outcome. Lower distance = higher score.
- **Calibration**: Brier-like distance between predicted miss
  probability and actual goal-miss frequency.
- **Coverage**: fraction of episodes the miner predicted on. Below 50%
  → score zeroed.
- **Reliability**: penalty if the miner's archive uploads consistently
  fail (signaled by retry-log appearances in 9.C.6).

Final score is a uint micro-units integer (0 to 1,000,000), committed
into the IMT in 9.C.2. The validator translates score → uint16 weight
via simple normalization and submits via `set_weights`.

> **Status (launch).** The tiered allocator is live in
> `hope/validator/tiered_weights.py:TieredAllocator`. Wire it via
> `WeightSetter(tiered_allocator=TieredAllocator())` and call
> `allocate_tiered(...)`; the call enforces all of the participation
> gate, EMA tier placement, Elite floor + redistribution, and pool
> shares from the reward spec, then applies burn. The legacy
> score-normalization path remains as a documented fallback for
> operators that want to disable tier mechanics during initial
> epochs. Epoch-type multipliers and the diversity bonus are still
> roadmap (Review 2 / Review 3); the rest of Components 1-2 ships at
> launch.

### 11.3 Why this is competitive, not extractive

A naive prediction service charges per-query and skims the spread.
SN21's model is different:

- **Anyone can run a miner**, anonymously, at the cost of one Bittensor
  hotkey registration.
- **No barrier to using better models** — better predictions → higher
  weights → more TAO. Worse predictions get clipped.
- **No vendor lock-in** — the protocol is open; advertisers can read
  any miner's predictions directly off chain.
- **The TAO emission funds the participants**, not a middleman.

The end state is something closer to Kaggle than to a B2B SaaS: a
public competition with a public scoreboard, settled by a public
chain.

---

## 12. Edge cases we've thought through

A protocol is only as good as its handling of the cases that don't fit
the happy path. Here are the ones we've explicitly designed for, with
the response.

### 12.1 drand pulse outage

Drand has run continuously since 2020. But "continuously" is not
"infallibly." If the pulse for the round that should auto-decrypt our
9.C.1/9.C.2 commits doesn't arrive, the chain can't decrypt them, and
the verifier can't read them.

**Response:**
- For outages < 5 min: chain catches up automatically. No action.
- For outages 5 min – 24h: the operator publishes a `Sha256` commit
  with payload `b"v1.0:epoch-skipped-drand-outage"`, miners are
  excused, the epoch is void.
- For outages > 24h: governance issues a `Sha256` with
  `b"v1.0:emergency-fallback-instructions"` pointing to a manual
  recovery procedure.

The protocol degrades visibly. Validators don't silently use stale
data; they explicitly skip the epoch.

### 12.2 Archive loss

If both Tier-2 (operator) and Tier-3 (miner self) lose a specific miner's
AES_ct between submission and scoring time, the validator can't
retrieve the bytes and excludes the miner with `plaintext_unavailable`.
The 9.C.6 retry log records the attempts, which a verifier can
reproduce.

This is failure mode by design. We chose three independent storage
tiers specifically because no single archive can be trusted; the
exclusion is the protocol working correctly under partial failure.

### 12.3 MaxSpace contention

A miner's epoch costs ~2,174 bytes against the chain's ~3,100-byte
MaxSpace per ~4-hour rolling window (per Phase 0 Q11 measurement;
empirical, not assumed). A miner submitting more than once per ~4.5
hours from the same hotkey will hit `SpaceLimitExceeded`.

**Mitigation in code:** the miner runner returns
`MinerSubmissionResult.failure_reason="chain_*_commit_failed: SpaceLimit..."`
and the operator's runbook §9.1 instructs them to wait for the window
to clear before retrying.

**Architectural mitigation:** the protocol's recommended minimum epoch
cadence is 4.5 hours — comfortably within the rate-limit window. A
24-hour cadence (the most common operational choice) has 5x headroom.

### 12.4 Validator MaxSpace contention

The validator's per-epoch cost is ~1,960 bytes (without retry log) or
~2,492 bytes (with). One validator + one epoch fits a window. The
9.C.6 retry log is only emitted when at least one miner was excluded
for `plaintext_unavailable` — so in healthy operation the validator
uses ~63% of its window budget.

Validators running shadow + primary on the SAME wallet on the SAME
epoch would bust MaxSpace; we explicitly require shadow on a separate
hotkey. Operator runbook §9.1 documents this.

### 12.5 Hotkey ↔ ed25519 binding loss

Every miner and validator publishes a 109-byte `Raw{N}` registration
commit binding their Bittensor hotkey to an ed25519 public key. Lose
the ed25519 PEM and you can't sign new predictions until you re-register.

**Recovery procedure** (operator runbook §9.4):
1. Generate a new ed25519 key (`scripts/sn21_keys.py generate`).
2. Register the new binding on chain (`sn21_keys.py register`).
3. The new commit overwrites the old binding in `CommitmentOf`.
4. Existing in-flight commits using the old key remain verifiable until
   their reveal_round; new commits must use the new key.

**Architectural caveat:** the chain's `CommitmentOf` storage holds ONLY
the latest commit per (netuid, hotkey). Historical bindings are NOT in
chain head state — auditing them requires an archive node. Operator
runbook §8.1 documents this explicitly.

### 12.6 Disputed scoring

If an advertiser thinks Miner Mike got credit for a prediction Mike
shouldn't have received credit for, the dispute path is:

1. Advertiser runs `verify_epoch.py` against the validator's chain
   commits at the disputed epoch's block_hash.
2. If the verifier verdict is `ok: True`, the validator's scoring is
   reproducible from chain state. Mike's score is what the chain says
   it is.
3. If `ok: False`, the divergence is recorded by-line in the verifier
   output. Either the validator's IMT root mismatches the verifier's
   recomputation, or an inner_sig fails. Both cases are public faults.
4. Advertisers with sufficient stake can publish their dispute via a
   `Sha256` commit from an operator-issued advertiser hotkey,
   anchoring the complaint into chain history.

This is not arbitration. There is no judge. The dispute path is purely
mechanical: replay the chain, see who's right.

---

## 13. What we deliberately defer

A protocol designed in 2026 must be honest about what it doesn't yet
solve. Here is the list.

### 13.1 Chain-side weights ↔ scoring binding

The `commit_timelocked_weights` extrinsic publishes weights at block X.
Our 9.C.2 commits contain `weights_commit_block_hash = X`. A verifier
checks both. But the chain itself does NOT enforce that the weights
committed via `commit_timelocked_weights` correspond to the scoring
artifact in 9.C.2. A malicious validator could in principle commit
weights derived from a different scoring artifact and serve a falsified
9.C.2 referencing them.

**Current mitigation:** a verifier reading both 9.C.2 and the on-chain
weights at `weights_commit_block_hash` detects the divergence
mechanically. Yuma stake-weighted median naturally clips the
dishonest validator if a shadow is running.

**Long-term fix:** an upstream Bittensor runtime patch adding a 32-byte
`external_anchor` field to `WeightsTlockPayload`. We've drafted the RFC
in `docs/proposals/q26_weights_payload_anchor.md`. With that field, the
chain itself binds weights to the scoring artifact — no off-chain check
needed. We'll submit the proposal to the Subtensor maintainers when the
on-chain protocol has run cleanly for one operational cycle.

> **Read this carefully.** Earlier sections of this paper describe
> 9.C.2 as "binding the score table to the weights commit block hash."
> That is true at the off-chain verifier level — and at launch the
> verifier in `scripts/verify_epoch.py` re-derives expected u16
> weights from the score table via
> `WeightSetter.derive_u16_weights` and compares them, UID by UID,
> against the actual weights at `weights_commit_block_hash`.
> Mismatched UIDs surface in
> `VerifierVerdict.weights_binding_mismatches`; the adversarial test
> `test_forged_weights_caught` proves a forged 9.C.2 paired with
> unrelated chain weights fails verification. **It is still not a
> chain-level cryptographic binding today.** A reader looking for
> "the chain itself rejects a weights commit that doesn't match its
> scoring artifact" will not find that property until the upstream
> change above lands. The verifier-side cross-check + the shadow
> validator + Yuma median is the operational defense in the interim.

### 13.2 Per-episode scoring commitments

Phase E added per-episode artifacts: each miner's CBOR bundle of
per-(episode × horizon) quantiles, with an IMT root committed inside
the aggregated 9.C.1 plaintext. This lets the verifier reproduce
per-episode scoring against the specific predictions for that episode.

What we have NOT done is split the on-chain commit into one per
episode. A 100-episode epoch would need 100 chain commits per miner;
MaxSpace forbids it. Per-episode artifacts live off-chain in archives
with a chain-anchored root. This is a deliberate trade-off: cheaper
chain footprint, slightly more off-chain trust.

> **Status (launch).** Phase E ships at launch. Miners that pass
> `per_episode_entries=...` to
> `submit_miner_epoch` build the off-chain bundle, upload it to the
> same archive endpoints alongside `AES_ct`, and the aggregated
> on-chain plaintext binds:
> - `episodes_root` — IMT root over per-(episode, horizon) entries.
> - `episodes_bundle_sha256` — SHA-256 of the bundle bytes.
>
> Both fields are inside the inner_sig'd plaintext, so a tampered
> bundle, a substituted bundle, or a divergent root all fail
> verification. The verifier scores per-(episode × horizon) via
> `score_one_miner_per_episode` from
> `hope.scoring.onchain_adapter`, and the legacy
> aggregate-per-horizon path is preserved for miners that haven't yet
> adopted Phase E. Adoption is by configuration, not protocol-version
> bump.

### 13.3 Privacy of predictions

A miner's predictions become public after the chain auto-decrypts K
and verifiers fetch the AES_ct. There is no privacy mechanism for
"ship a prediction nobody else can ever see, including the operator."

This is by design. Auditing requires plaintext access. A miner who
wants their model's outputs private should not participate in a
verifiable prediction subnet; they should sell predictions privately.

### 13.4 Mainnet TAO fees

Phase 0 Q13 measured `set_commitment` extrinsic fees on testnet 466 at
0 µTAO. Mainnet may differ. We'll measure once before the mainnet
flip; the operator runbook §12 has the pre-launch checklist that
includes this measurement.

### 13.5 What we tried first and rejected

A list of design attempts that didn't survive empirical contact with
the chain. We're including these because they're the most honest
record of the design process: ideas that looked good on paper, broke
on real testnet, and forced revisions.

**Multi-field commit (Q36).** The Bittensor `Commitments` pallet
accepts `info = {"fields": [[Sha256, TimelockEncrypted, Raw]]}` —
multiple Data variants in one extrinsic. We prototyped this hoping
to compress all three Layer 9.B miner commits (TLE'd K, Sha256 of
ciphertext, archive URL) into a single chain submission, saving
~2 of 3 extrinsic fees per miner per epoch. The Q36 testnet probe
confirmed: the chain RUNTIME accepts the extrinsic (success=True
returned), but the auto-decrypt subsystem silently skips
multi-variant slots, AND the SDK readback methods do not return
them. The variant exists in the chain types but is unsupported in
practice. We gated `submit_layer_9b_multi_field` with
`NotImplementedError` and kept the 3-extrinsic path. Cost: 3× the
per-extrinsic fee per miner per epoch. Acceptable given the testnet
fee is 0 µTAO and the mainnet fee should be nominal.

**Burst probe with small payloads (Q11 v1).** The first version of
the rate-limit probe submitted ~17-byte payloads in a tight loop,
hoping to trip MaxSpace. It didn't — 21 commits at 17 bytes each
totalled ~360 bytes, well under the 3,100-byte cap. We re-ran with
128-byte Raw{128} payloads (Q11 v2) and got the empirical answer
(252 minutes per window, ~500 bytes per-commit overhead). The
mistake was assuming MaxSpace was per-call rather than per-byte;
the corrected probe confirmed it's per-byte.

**Chain auto-decrypt assumption (H-3).** Detailed in §6.4. We
initially used `bittensor_drand.encrypt(bytes, ...)`. The chain
silently dropped our commits despite returning success=True. H-3
diagnosed; H-4 found the right helper; H-6 shipped the fix.

**768-byte plaintext budget (pre-H-6).** Before the H-6 fix, we
budgeted for ~768 bytes of raw binary plaintext per TLE commit.
After switching to `get_encrypted_commitment(str)` and hex-encoding,
the effective budget halved to ~380 bytes. The 9.C.1 / 9.C.2 builders
fit (real plaintexts measure 364-380 bytes for realistic 50-miner
epochs), but barely. A larger validator population — say 200 miners —
would exceed the budget and force splitting commits into multiple
extrinsics. The architecture records this as a Phase E follow-up.

**SDK readback path (Phase G).** Bittensor SDK 10.2.1's
`get_revealed_commitment_by_hotkey()` lossily UTF-8 decodes binary
chain bytes — codepoints >127 mangle into multi-byte sequences. We
hit this when our 32-byte AES key K came back as garbled string. The
fix was to drop to `subtensor.substrate.query("Commitments",
"RevealedCommitments", ...)` directly and convert SCALE int-tuples
via `bytes(t)`. `hope/commitment/chain_reader.py` is that bypass.

These are not embarrassments; they are the work. Each one is a
specific decision a human made by reading code, running tests, and
choosing the next move.

---

## 14. What ships in v1.0

Here is the inventory, with file paths so you can audit.

### 14.1 Code surface

| Module | Lines | Responsibility |
|---|---|---|
| `hope/commitment/canonical.py` | 60 | RFC 8949 §4.2.1 canonical CBOR + AAD |
| `hope/commitment/drand_lib.py` | 73 | drand quicknet round math + constants |
| `hope/commitment/imt.py` | 245 | indexed Merkle tree (sorted-leaf) |
| `hope/commitment/inner_sig.py` | 119 | ed25519 signature over canonical-CBOR |
| `hope/commitment/on_chain.py` | 460 | 7-variant chain commit helpers |
| `hope/commitment/prediction_payload.py` | 305 | 9.B miner CBOR + AES-GCM envelope |
| `hope/commitment/archives.py` | 341 | three-tier client (upload + verified fetch) |
| `hope/commitment/scoreability.py` | 283 | the 8-check rule with discrete failures |
| `hope/commitment/scoring_state.py` | 269 | 9.C.1 / 9.C.2 builders + IMT roots |
| `hope/commitment/retry_log.py` | 167 | 9.C.6 JSON blob builder |
| `hope/commitment/registration.py` | ~190 | hotkey ↔ ed25519 binding |
| `hope/commitment/episode_artifacts.py` | 230 | per-episode bundle (Phase E) |
| `hope/commitment/chain_reader.py` | 270 | substrate-direct reads (Phase G/H) |
| `hope/hope_outcomes/release_commit.py` | 170 | 9.A.1 release_commit |
| `hope/hope_outcomes/reveal_blob.py` | 200 | 9.A.2 reveal blob |
| `hope/miner/onchain_submitter.py` | 220 | full 9.B pipeline |
| `hope/miner/runner.py` | 320 | miner CLI with `--mode {http,onchain}` |
| `hope/validator/onchain_reader.py` | 218 | chain reads → scoreability per miner |
| `hope/validator/onchain_runner.py` | 290 | full 9.C orchestration |
| `hope/validator/weights_commit.py` | 170 | 9.C.3 wrapper |
| `hope/validator/migration.py` | 200 | HTTP → on-chain replay tool |
| `hope/scoring/onchain_adapter.py` | 320 | EpochScorer adapter + CRPS scorer |
| `hope/archive_server/app.py` | 280 | FastAPI Tier-2/Tier-3 archive |
| `hope/archive_server/store.py` | 130 | InMemory + Filesystem stores |
| `hope/archive_server/metrics.py` | 90 | Prometheus metrics |
| `hope/hope_shadow_validator/runner.py` | 100 | 9.E shadow wrapper |
| `scripts/verify_epoch.py` | 600 | public verifier (CLI + library) |
| `scripts/sn21_keys.py` | 320 | ed25519 key-management CLI |
| `scripts/score_predictions.py` | 90 | offline scoring tool (miners) |
| `scripts/train_example_model.py` | — | reference XGBoost training (miners) |
| `scripts/generate_training_data.py` | — | training-set fetch helper (miners) |
| `hope/validator/tiered_weights.py` | ~260 | participation gate + EMA tiers + Elite floor |

Total: ~7,500 LOC code, ~6,800 LOC tests. 488 tests pass — see
`tests/` for the unit, adversarial, and end-to-end surface; lint
clean under `ruff check`.

### 14.2 Documentation surface

| Doc | Purpose |
|---|---|
| `docs/verifiable_scoring_architecture.md` | Internal architecture spec, v1.0 + Phase G + Phase H. Auditor-facing. (Gitignored.) |
| `docs/operator_runbook.md` | Operator playbook: setup, daily ops, incidents, rotation, mainnet pre-launch. |
| `docs/proposals/q26_weights_payload_anchor.md` | Upstream Bittensor RFC for chain-side T-20b binding. |
| `docs/whitepaper.md` | This document. Human-friendly summary. |
| `deploy/archive_server/README.md` | Archive server deployment (Docker / systemd). |
| `deploy/grafana/README.md` | Sample Grafana dashboard for archive server metrics. |

### 14.3 Empirical record (testnet 466)

What we've actually run on chain:
- 4 ed25519 keys generated, mode 0600 PEMs, ALL backed up offline.
- 1 validator-role registration commit (block 7041171, success=True).
- 1 9.C.1 pre-scoring TLE commit (success=True, reveal_round 28336161).
- 1 9.C.3 weights commit via `commit_timelocked_weights` (success=True).
- 1 9.C.2 post-scoring TLE commit (success=True, reveal_round 28336216).
- 1 H-3 TLE auto-decrypt probe (FAIL — found the format bug).
- 1 H-4 verification probe via `set_reveal_commitment` (PASS — found the fix).
- 1 H-6 end-to-end round-trip (PASS — submit → auto-decrypt → decode in 105s).

Validator running live (chain mode flip pending operator decision).

---

## 15. Future expansion

The protocol is generic over "predict some future quantity that has
verifiable ground truth." Google Ads is the v1 target because:

- The operator has authoritative measurement infrastructure (the
  authoritative oracle for participating customers' campaigns).
- The data is dense, daily, and has natural prediction horizons (7d, 14d).
- The questions are commercially valuable (advertisers pay real money
  for accurate forecasts).

But nothing in the protocol is Google-Ads-specific. The architecture
generalizes to:

### 15.1 Other ad platforms

Meta, TikTok, LinkedIn — same protocol, different episode schema.
Adapters live in `hope/protocol/episode.py` and the scoring weights in
`hope/scoring/weights.py`. A new platform is a new schema + a new
authoritative oracle.

### 15.2 Other data domains with a similar shape

Anything where:
- Inputs are visible to predictors at T=0.
- Outcomes are visible only at T=deadline.
- An authoritative oracle measures and publishes outcomes.
- Quantile predictions (P10/P50/P90) are valuable.

E-commerce demand forecasting fits. Supply chain logistics fit. Crop
yield forecasting fits. Each requires an authoritative oracle; the
protocol is otherwise unchanged.

### 15.3 Protocol-level upgrades

- **Per-episode chain commits**: when a future Bittensor runtime allows
  larger `TimelockEncrypted` payloads or batched commits, we can move
  per-episode roots on chain (replacing the off-chain bundle).
- **Cross-subnet scoring**: a miner submitting to multiple data domains
  (Google Ads + Meta + e-commerce) could be scored by a cross-subnet
  validator; their TAO emission would aggregate. Requires Bittensor
  multi-subnet weight protocols, currently exploratory.
- **External validator participation**: Phase 3 of the architecture
  opens validator slots to third-party operators. The operator runbook
  is designed to make this a pure runtime operation — no special
  privileges required.

---

## 16. Conclusion

The mechanism is not subtle. Predictions are committed on chain.
Outcomes are committed on chain. Scoring is a function of public state.
Anyone can verify. The end.

What's subtle is the chain of bindings that hold this together — the
`inner_sig` over canonical CBOR; the AES-GCM AAD bound to epoch; the
TLE encryption that auto-decrypts on schedule; the IMT root that lets a
verifier prove inclusion AND non-inclusion of any single miner; the
shadow validator whose mere existence makes single-validator
dishonesty publicly auditable.

We spent more cycles than expected discovering empirical truths the
chain hides — the auto-decrypt format bug in Phase H was the most
expensive (§6.4 narrates that one) — but every discovery tightened the
protocol. The result is v1.0: a verifiable prediction subnet that runs
on testnet, with every binding tested against a 12-attack adversarial
suite, every scoring decision reproducible from chain state, and every
commit empirically proven to round-trip through real chain auto-decrypt.

The protocol is not the contribution. The contribution is what the
protocol replaces: it removes the trust assumption that a vendor will
honestly grade their own homework.

To the question we opened with — "do we understand this solution?" —
the only honest answer is the one this paper has tried to give: yes,
because we made the design choices and we found the bugs and we ran
the probes; and we used an AI coding agent to help compose the
result, the way we use IDEs and linters and continuous integration.
The agent's contribution and ours are both visible in the commit
history. Any reader can audit either independently.

Mainnet next.

---

## Appendix A — Test surface

```
tests/unit/commitment/      258 tests
tests/unit/miner/             9 tests
tests/unit/validator/        34 tests
tests/unit/hope_outcomes/    26 tests
tests/unit/hope_shadow_validator/   2 tests
tests/unit/archive_server/   38 tests
tests/unit/scripts/           5 tests
tests/unit/scoring/          26 tests
tests/adversarial/           12 tests  (every claimed defense gets a test)
─────────────────────────────────────
Total:                      453 pass, 7 skipped
```

Adversarial tests are in `tests/adversarial/test_attack_surface.py`.
Each test stages an attack scenario from the architecture's threat
model, runs the protocol's defense, and asserts the defense fires. If
any test passes a malicious payload through, the build fails CI.

Run locally: `pytest tests/`. Run only adversarial: `pytest tests/adversarial/ -v`.

---

## Appendix B — Empirical findings (testnet 466)

The architecture's claims are backed by measurement. Here is the
condensed record; the phase-by-phase build narrative — including who
ran what probe, when, and why — is in `docs/build_journey.md`.

### B.1 Q11 — RateLimit window (2026-05-03)

- 5 × 128-byte `Raw{128}` commits succeeded before `SpaceLimitExceeded`.
- Per-commit overhead inferred: ~500 bytes.
- Window: 1,259 blocks ≈ 252 minutes ≈ 4.2 hours ≈ 3.5 × subnet tempo.
- Implication: minimum supported epoch cadence ≈ 4.5h; 24h cadence has
  5x headroom.

### B.2 Q13 — Extrinsic fee (2026-05-03)

- `set_commitment` on testnet 466: 0 µTAO.
- Mainnet measurement deferred to pre-launch.

### B.3 Q35 — Lower-level commit path (2026-05-03)

- `subtensor.set_commitment(data: str)` is limited to `Raw{0..128}`.
- `publish_metadata_extrinsic(data_type=...)` accepts `Sha256` (32 B)
  and `TimelockEncrypted` (≤ 1024 B) directly.
- We use the lower-level path; the higher-level helper would waste
  capacity on hex/UTF-8 wrappers.

### B.4 Q36 — Multi-field commit (2026-05-03)

- Multi-variant `info.fields[0]` is ACCEPTED by the chain runtime.
- But: chain auto-decrypt does NOT walk multi-variant slots, AND SDK
  readback does not return them.
- Implication: the 3-extrinsic Layer 9.B path is authoritative.
  `submit_layer_9b_multi_field` is gated with `NotImplementedError`.

### B.5 H-3 — TLE auto-decrypt probe (2026-05-04)

- Submitted via `bittensor_drand.encrypt(bytes, ...)` + `publish_metadata_extrinsic`.
- 30-min poll, NO auto-decrypt observed.
- Confirmed Hypothesis B: format incompatible.

### B.6 H-4 — Discovery (2026-05-04)

- The SDK's `set_reveal_commitment(data: str)` calls
  `bittensor_drand.get_encrypted_commitment(data: str, ...)` — a
  DIFFERENT C function from `encrypt(bytes, ...)`.
- Verification probe: marker auto-decrypted in 105s. The chain accepts
  this format.

### B.7 H-6 — End-to-end fix verification (2026-05-04)

- Updated `submit_timelock_commit` to hex-encode + use
  `get_encrypted_commitment`.
- Test: 20-byte plaintext `0xc0ffee...deadbeef` submitted, auto-decrypted
  at block 7049220 (105 seconds after submission), decoded byte-exact
  via `chain_reader.decode_revealed_tle_plaintext`.
- The protocol is now end-to-end verified on real chain.

---

## Appendix C — References

### C.1 External standards

- [RFC 8949] CBOR specification, §4.2.1 canonical encoding.
- [RFC 7748] X25519 / Ed25519 signature scheme.
- [NIST SP 800-38D] AES-GCM authenticated encryption.
- [Aztec Indexed Merkle Tree](https://docs.aztec.network/aztec/concepts/storage/trees/indexed_merkle_tree)

### C.2 Bittensor

- [Subtensor source](https://github.com/opentensor/subtensor) — `pallets/commitments` and `pallets/subtensor` for the chain-side primitives.
- [bittensor SDK](https://github.com/opentensor/bittensor) — `bittensor.core.extrinsics.serving.publish_metadata_extrinsic` and `Subtensor.set_reveal_commitment` are the main entry points.
- [bittensor_drand](https://github.com/opentensor/bittensor-drand) — `get_encrypted_commitment` is the chain-correct TLE helper.

### C.3 drand

- [League of Entropy](https://drand.love/) — operates the quicknet beacon.
- Quicknet chain hash: `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971`.
- Quicknet genesis: 1692803367 (Unix), 3-second period.

### C.4 Internal documents

- `docs/verifiable_scoring_architecture.md` — full architecture spec.
- `docs/operator_runbook.md` — production playbook.
- `docs/proposals/q26_weights_payload_anchor.md` — upstream RFC.

### C.5 Code

- Architecture commits 5d5a195 → present: every phase landed on `main`.

---

*Last updated 2026-05-04, v1.0+G+H. Composed with substantial authoring assistance from an AI coding agent. Engineering decisions, design judgments, and empirical validation are the maintainers'; the paper's prose was iterated through the agent. All claims are linked to source files and commit hashes; readers should verify directly.*
