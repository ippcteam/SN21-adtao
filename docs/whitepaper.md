# SN21 Whitepaper

### Predict Google Ads outcomes. Earn from accuracy alone.

Every prediction is sealed on chain before the outcome is knowable.
Every score is reproducible by anyone with a chain reader.
No one — including the operator — can rewrite the record after the fact.

Bittensor Subnet 21 · MIT-licensed · Public verifier ships at launch

---

## Launch status (read first)

| Item | Status | Where |
|---|---|---|
| Mainnet netuid | **21** (Bittensor `finney`) | Subnet registration |
| Testnet netuid | **466** (Bittensor `test`) | testnet validation |
| Current phase | Pre-mainnet — testnet validation complete | Appendix B |
| Validator registration | **Closed at launch.** Operator runs primary + shadow. Opening is on the Review 4 agenda | `docs/SN21_REWARD_MECHANISM.md` |
| Miner registration | Open on testnet 466 today; mainnet 21 opens when launch announces | `docs/miner_quickstart.md` |
| Public verifier | **Live at launch in two modes.** Default mode runs chain reads, `inner_sig` checks, IMT root recomputation, weights-binding cross-check, and per-miner scoreability re-derivation. Full score recomputation requires `--truth-file` derived from the 9.A.2 reveal blob; without it, scoring returns zero and `final_score_match` fails by design (a startup warning makes this explicit). Past-epoch reads need an archive node RPC. Recorded-epoch fixture under `tests/fixtures/recorded_epoch/` proves `ok=true` round-trip | `tests/scripts/test_verify_epoch_live_scorer.py` + README §"Verifying any epoch" |
| Weights ↔ scoring binding | **Operational at launch + verifier-side cross-check live.** Verifier compares chain weights at `weights_commit_block_hash` against weights re-derived from the score table; mismatched UIDs are surfaced. The chain-side anchor (32-byte field in `WeightsTlockPayload`) is being pursued as an upstream Bittensor change | §14.1 + adversarial test |
| Per-episode artifacts | **Available via configuration.** `submit_miner_epoch(per_episode_entries=...)` builds the bundle, binds `episodes_root` + `episodes_bundle_sha256` in the aggregated plaintext, and uploads to the archives. The default `hope-miner` CLI submits aggregate-per-horizon at launch; per-episode is opt-in for miners that wire it. Default behaviour will move to per-episode after operational-cycle-1 | §14.2 |
| Reward mechanism | **`TieredAllocator` available, default runner uses simple normalization + burn.** `hope/validator/tiered_weights.py:TieredAllocator` enforces the full participation gate / EMA tier placement / Elite floor / pool shares spec. The default `hope-validator` CLI runs `WeightSetter(burn_fraction=0.95)` + `normalize_scores(...)` at launch. To enable tiers, an operator constructs `WeightSetter(tiered_allocator=TieredAllocator())` and calls `allocate_tiered(...)`. Tier mechanics are scheduled to become the default after Review 1 | `docs/SN21_REWARD_MECHANISM.md`, `hope/validator/tiered_weights.py` |
| Conditional-prior baseline | **Live at launch.** The release artifact's `scoring_metadata.conditional_prior` per episode plumbs through `ScoringMetadata` and `SkillScoreCalculator.compute_baseline_prediction(...)`. Episodes with no published prior fall through to predict-zero — no crash, no silent gate-zeroing | §12 |

When in doubt about a claim in the rest of this paper, this table is the
authoritative read on what is shipping at launch versus what is
described as the target architecture.

**Conventions used below:**
- *Launch* = behaviour available the day mainnet opens.
- *Roadmap* = explicitly future, marked as such.

---

## 1. The Problem

Modern advertising platforms make millions of automated decisions per
day. Budgets shift, bid strategies retrain, campaigns pause and
resume. Each intervention has a measurable impact on cost,
conversions, and efficiency over the days that follow.

**Nobody predicts that impact before the change is made.**

This is not a gap in any one product. It is structural across the
industry. PPC managers make decisions based on experience, partial
data, and rough estimates. PPC software offers reporting dashboards,
automation rules, and template-based recommendations — not calibrated
forecasts of what specific interventions will do to specific
accounts. The closest thing to a forecast in the field is a manual
A/B test, which is slow, expensive, and only available to the largest
advertisers.

Predicting intervention impact has been on the "too difficult" pile
for good reasons:

- **Heterogeneous action space.** Budget changes, bid strategy
  switches, target value adjustments, structural edits, creative
  changes, targeting changes — each has a different mechanism and a
  different time-to-effect. A model that handles one action class
  well is silent on the others.
- **Noisy short-horizon outcomes.** Day-to-day variance in
  impressions, clicks, and conversions is large relative to the size
  of most interventions. Extracting a clean impact signal at 7 or
  14 days requires modelling the noise, not just the signal.
- **Portfolio effects.** Changing one campaign reshapes the budget
  and impression flow across an account. A model that predicts a
  single-campaign delta in isolation is wrong by construction.
- **Learning-period dynamics.** Bid strategies retrain over a 7–14
  day window after a change. Outcomes during that window are
  dominated by the retrain, not the underlying account economics.
- **Skill mismatch.** Building accurate impact models is a
  probabilistic forecasting problem. The expertise lives in ML and
  statistics, not in the agency or in-house teams that own ad
  accounts.
- **No accountability layer.** Even if a forecaster did emerge, a
  track record on this kind of prediction is unverifiable today:
  outcomes are private to the advertiser, screenshots can be
  cherry-picked, and "I called it last quarter" is impossible to
  falsify.

The cost of operating without impact forecasts is paid every day.
Advertisers commit budget to changes whose effect they cannot
quantify in advance. Optimisation systems grade their own homework.
Decisions that look identical on paper produce wildly different
outcomes, and the industry treats the variance as irreducible.

**It is not irreducible. It is unmeasured.**

The structural gap is that there is no infrastructure to (a) publish
impact-prediction problems consistently and at scale, (b) settle
predictions against measured outcomes, and (c) reward predictive
skill independent of who owns the underlying ad account. Without
that infrastructure, no market for impact prediction can form.

---

## 2. The Solution

SN21 builds the missing prediction layer.

A weekly stream of structured prediction problems — **episodes** — is
published from real Google Ads management data. Each episode
describes an account's pre-window state, the actions that were taken,
and the environmental context. Miners predict the impact of those
actions on cost, conversions, and efficiency over 7-day and 14-day
horizons. Validators score those predictions against measured
outcomes. Bittensor distributes emissions in proportion to predictive
skill.

The mechanism solves each piece of the structural gap:

- **Structured prediction problems at scale.** The data pipeline
  normalises thousands of Google Ads accounts into a consistent v1.9
  episode schema and publishes weekly. Miners face the same problem
  shape every epoch and can build models that compound across cycles.
- **Settlement against measured outcomes.** Outcomes are measured
  from live Google Ads data and committed on chain. The scoring
  function is open source and deterministic. Prediction skill is
  separated from outcome measurement at the protocol level.
- **Skill rewarded independent of execution surface.** Miners do not
  need ad accounts, advertiser relationships, or platform access.
  Bittensor pays them in TAO emissions for accurate predictions. A
  statistician with no PPC background can compete on equal terms
  with a domain expert.

The protocol is designed around two core cryptographic guarantees:

1. **Predictions cannot be re-written after outcomes are known.**
   Every miner prediction is committed on chain via timelock-
   encryption (TLE) before the validator publishes outcomes. The
   chain auto-decrypts the prediction key only after the deadline
   has passed. Late or rewritten predictions are detectable from
   chain state alone.

2. **Validator scoring is independently reproducible.** Every input
   that affects a miner's score is anchored on chain through a
   Merkle root. A public verifier (`scripts/verify_epoch.py`) reads
   the chain, fetches the off-chain artifacts, re-runs the
   open-source scoring code, and either confirms the chain-anchored
   result or produces a structured diff identifying the divergence.

Both guarantees are structural, not procedural. The protocol does
not run privileged infrastructure that miners must trust. The
validator is open-source and reproducible. The scoring algorithm is
open-source. The chain is the system of record.

---

## 3. The Verification Layer

Most subnets settle weights opaquely: the validator computes scores
in private, sets weights via `set_weights`, and miners take the
result on faith. SN21 replaces faith with cryptographic evidence at
every layer.

### Commit-Reveal at Each Layer

Three protocol layers commit to immutable hashes on chain *before*
anyone can act on the underlying data:

- **Layer 9.A — Outcome signer.** Commits the canonical CBOR digest
  of the epoch's release package (episode set, queries, scoring
  rules) at T=0, before miners see anything. After the deadline,
  commits the SHA-256 of the reveal blob (measured outcomes, salts)
  before serving the blob via HTTPS. The operator cannot retroactively
  change which episodes were in scope or what outcomes were measured.

- **Layer 9.B — Miner predictions.** Each miner submits one
  timelock-encrypted on-chain commit per epoch whose plaintext
  bundles three values: the AES key K, the SHA-256 of the
  AES-encrypted prediction ciphertext, and the miner's self-archive
  URL. The chain auto-decrypts the bundle only after the deadline,
  surfacing all three values atomically. The full prediction itself
  never touches operator infrastructure.

- **Layer 9.C — Validator scoring.** Before scoring, each validator
  commits an Indexed Merkle Tree (IMT) root over per-miner chain
  commits and an excluded-miners hash. After scoring, it commits an
  IMT root over per-miner final scores plus the block hash where its
  `commit_timelocked_weights` extrinsic landed. Validators cannot
  rewrite the score table without producing a contradicting chain
  commit.

### Inner Signatures Defeat Rubber-Stamping

Every committed plaintext carries an `inner_sig` — an ed25519
signature over the canonical CBOR encoding of the plaintext
(excluding the signature field itself), bound to the writer's
hotkey. A second validator that copies the primary's ciphertext
bytes verbatim cannot pass off the result as its own work: the
`validator_hotkey` field inside the plaintext would not match its
storage account, and any verifier checking both slots sees the
divergence immediately.

### Indexed Merkle Trees for Per-Miner Inclusion

Score artifacts are anchored as IMT roots, not flat lists. The
Merkle structure is sorted-leaf with low/high pointers (Aztec-style),
giving cheap inclusion *and* non-inclusion proofs. A miner can prove
their score is `S` under the chain-anchored root without downloading
the entire score table; a validator can prove a contested miner is
*absent* from the score set by exhibiting the bracketing leaf.

### Drand Timelock Encryption

Timelock encryption uses [drand quicknet](https://drand.love) — a
public, distributed beacon producing a verifiable BLS pulse every
3 seconds. Encrypted commits target a specific future round; the
network reveals the decryption key when that round is reached.
Subtensor's chain runtime auto-decrypts qualifying commits in the
block where the round arrives. The protocol does not depend on any
single drand operator: drand quicknet is run by an independent set of
organisations and verified at the BLS-on-BLS12-381 level.

### Hotkey ↔ ed25519 Binding

Bittensor hotkeys are typically `sr25519`, but inner signatures need
an ed25519 verifier. Each role (miner, validator, outcome signer)
publishes a one-time on-chain `Raw{109}` registration linking its
hotkey SS58 to a 32-byte ed25519 public key, co-signed by both keys.
The binding is auditable from chain state alone —
`python scripts/sn21_keys.py verify-reg --hotkey-ss58 <ss58>`
returns `sig valid: True/False`.

---

## 4. Vocabulary

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

## 5. How an epoch runs (narrative)

Three characters: **Hannah** the outcome signer, **Miner Mike** the
prediction model operator, **Validator Vera** the scoring honest party.
A fourth, **Adversary Adam**, will join in §11.

> (Reminder: the 7-day and 14-day windows have already played
> out by the time the epoch opens. Hannah already knows what happened.
> What the protocol prevents is Hannah re-measuring the outcomes after
> she sees the predictions.

### T = 0 — Episodes go out

Hannah builds the epoch from real Google Ads data. She picks 182
campaigns from a past anchor date — say the action she wants miners to
predict happened on `T = 2026-04-01`. The 60-day pre-window covers
`[2026-01-31, 2026-03-31]`. The 7-day and 14-day outcomes (the answers)
cover `[2026-04-02, 2026-04-15]`. All of those windows are in the past.
The data is sitting in her measurement system right now.

For each campaign she records a question — "what will the
cost-per-conversion be 7 days from now?" — and the inputs the miners
will see. She does NOT publish the outcomes yet. They exist; they just
stay sealed on her side until reveal.

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

Miner Mike runs his prediction model on the inputs Hannah published.
He generates P10/P50/P90 quantile forecasts for each
(campaign × horizon) — say, P50 cost goes down 3.7% over 7 days, with
the P10/P90 band at −8.5% to +1.2%.

Mike has no way to know the real outcome. The pre-window is public,
but the 7-day and 14-day deltas are sealed inside Hannah's system; the
only on-chain reference to them right now is the `release_commit`
hash, which doesn't reveal them.

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

### T = deadline + δ — Outcomes revealed

The submission deadline has now passed. Hannah takes the outcomes she
already had on her side — cost change, conversion change, did the goal
miss — and packages them into a `reveal_blob` JSON. She signs the
blob, then submits a `Sha256(reveal_blob)` commit on chain. After that
commit finalizes, the blob goes to her HTTPS endpoint.

The order matters. If Hannah served the blob before the chain commit,
she could serve different blobs to different validators. The chain
commit ensures that whatever blob anyone sees, it is the SAME blob.

What the chain commit at T=0 (`release_commit`) and the chain commit
at deadline (`reveal_blob_hash`) together enforce is: the outcomes
revealed at the deadline are bound to the rules pinned at T=0, with
no opportunity for Hannah to re-pick or re-measure between the two.

### T = deadline + δ + ε — Validator scores

Vera now has everything she needs:
- The on-chain `release_commit` digest (rules of the epoch).
- The on-chain `reveal_blob_hash` (with the blob fetchable from
  Hannah's HTTPS).
- For each miner, one timelock-encrypted on-chain commit (whose
  plaintext bundles K, SHA-256 of the ciphertext, and the self-archive
  URL) plus the off-chain ciphertext at that URL.

The drand round of each miner's bundle commit has by now passed. The
chain has auto-decrypted the bundle and stored the plaintext in
`Commitments::RevealedCommitments`.

Vera reads each miner's bundle from chain and decodes it into K,
sha256_ct, and url. She fetches the AES_ct from the URL (which a
miner may also mirror to operator Tier-2 archives; she trusts none of
them and verifies SHA-256 against the chain-revealed digest). She
decrypts AES_ct with K, with the epoch ID as AAD. She gets the
miner's CBOR plaintext.

She runs **the eight-check scoreability rule** (§8) on each miner.
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
> `tests/scripts/test_verify_epoch_live_scorer.py`, and a
> regression guard test fails the build if a placeholder scorer is
> ever reintroduced into `scripts/verify_epoch.py`. The verifier also
> cross-checks `actual_weights_at_commit_block` against weights
> re-derived from the score table — see §14.1.

---

## 6. Seven chain anchors

Every binding moment in the protocol is a chain commit. Here is the
exhaustive list.

| # | Layer | Who | What | Variant | Purpose |
|---|---|---|---|---|---|
| 1 | 9.A.1 | Operator | release_commit_digest | Sha256 | Pin epoch rules at T=0 |
| 2 | 9.A.2 | Operator | reveal_blob_sha256 | Sha256 | Pin measured outcomes at T=deadline+δ |
| 3 | 9.B   | Miner | TimelockEncrypted bundle | TimelockEncrypted | One commit carrying `{K, sha256(AES_ct), self_archive_url}`; chain auto-decrypts at reveal_round and surfaces all three values atomically |
| 4 | 9.C.1 | Validator | pre_scoring_state | TimelockEncrypted | IMT root over miner commits + outcome-fetch round |
| 5 | 9.C.3 | Validator | weights | (subtensor) | Yuma weights via `commit_timelocked_weights` |
| 6 | 9.C.2 | Validator | post_scoring_artifacts | TimelockEncrypted | IMT root over scores + scoring-inputs hash + weights block hash |
| 7 | 9.C.6 | Validator | retry_log_blob_sha256 | Sha256 | Only when ≥1 miner excluded for plaintext_unavailable |

Seven anchors total: two operator, one miner, four validator.

**Why a single bundled miner commit (and not three separate ones):**
Substrate's `set_commitment` is single-slot, last-write-wins on
`CommitmentOf`. A three-extrinsic flow (TLE'd K → Sha256 → Raw URL)
gets the TLE'd K commit overwritten by the subsequent non-TLE commits
before its drand reveal_round fires; the chain then has nothing to
auto-decrypt. Bundling K, sha256(ct), and the URL into one TLE'd
plaintext eliminates that failure mode entirely. Plaintext stays well
under the ≤380-byte TLE budget (typical: ~110 bytes).

The chain footprint is tight. A miner's epoch costs ~700 bytes (one
TimelockEncrypted commit) against a 3,100-byte MaxSpace; a validator's
costs ~1,960 bytes (2,492 with the optional retry log). All commits
fit comfortably in one rate-limit window.

---

## 7. Cryptographic primitives, intuitive depth

This section walks through the load-bearing crypto at the depth a
curious reader can follow without a textbook open. None of the
building blocks are exotic — they're all standard primitives — but
the way they fit together is what makes the rest of the protocol
stand up. Implementation lives in `hope/commitment/`; every claim
in this section maps to a function in that package and a unit test
in `tests/commitment/`.

### 7.1 Canonical CBOR

Canonical CBOR is the encoding we reach for whenever bytes are about
to be hashed or signed. "Canonical" means there is exactly one way
to encode any given map: definite-length items, sorted keys,
smallest-form integers, no indefinite-length anything. The
`cbor2.dumps(..., canonical=True)` library call gives us all of that
for free.

It sounds like a footnote, but it's load-bearing. Two different
implementations encoding the same map produce byte-identical output,
so the hash of those bytes is identical on both sides. Without the
rule, two implementations could produce different hashes for "the
same" map and the protocol would silently break — the kind of bug
that doesn't surface until someone disputes a score and nobody can
agree on what the canonical input even was.

### 7.2 AES-GCM with epoch-bound AAD

Each miner generates a fresh 32-byte AES key K per epoch and
encrypts their prediction CBOR with it under AES-256-GCM. The AAD
(Associated Authenticated Data) is the literal byte string
`b"sn21-prediction-v1:" + epoch_id`.

Why bother with AAD? Without it, AES-GCM gives confidentiality and
integrity of the ciphertext, but the ciphertext isn't bound to any
particular context. A malicious validator who got hold of K could
try to replay it under a different epoch ID, splicing an old
prediction into a new scoring round. With AAD, the decrypt simply
fails unless the verifier supplies the same epoch ID that was used
at encrypt time. The chain commit pins which epoch ID is the right
one, and the AAD makes sure the ciphertext can only ever live in
that epoch.

### 7.3 ed25519 inner_sig

Every miner and every validator owns an ed25519 keypair, separate
from their Bittensor SS58 hotkey. They register their public key on
chain once — a 109-byte `Raw{N}` commit binding the hotkey to the
ed25519 public key. From that point on, every prediction or scoring
commit they make is signed with their ed25519 private key.

The inner_sig is computed over
`blake2b_256(canonical_cbor(plaintext minus inner_sig))`. The clever
part — and it really is the linchpin — is that the plaintext
carries its own public key inside itself, AND the chain storage slot
it lives in is owned by the writer's hotkey. Verifying the inner_sig
is therefore two checks, not one:

1. The signature is valid against the public key in the plaintext.
2. The public key in the plaintext matches the one bound to the chain
   account that wrote this slot.

That second check is what kills the rubber-stamp attack. A second
validator can absolutely copy a primary's plaintext bytes into its
own storage slot — Substrate authorizes the writer, not the
contents. But the inner_sig in those copied bytes still verifies
against the **primary's** hotkey, not the second's. A verifier
comparing the second's chain account against the inner_sig's hotkey
field spots the mismatch immediately.

### 7.4 drand timelock encryption (TLE)

Drand quicknet is a randomness beacon run by the League of Entropy.
Every 3 seconds it publishes a BLS signature for the next round; any
future round's signature is unknowable until that round actually
drops. Drand TLE wraps that property — encrypt a message such that
it can only be decrypted with the round-N signature, and you've
effectively sealed it until time N.

Subtensor builds on top: a `TimelockEncrypted` commit with a
`reveal_round` field is auto-decrypted by the chain runtime once the
round fires, and the plaintext is stored back on chain. We use this
for two things:

- The miner's AES key K (auto-reveals after the miner deadline).
- The validator's 9.C.1 and 9.C.2 plaintexts (auto-reveal 1–2 hours
  later).

The chain runtime is strict about which TLE format it will
auto-decrypt — and this is where we paid tuition. We use
`bittensor_drand.get_encrypted_commitment(data: str, ...)`, the same
helper `Subtensor.set_reveal_commitment(...)` calls internally.
Binary plaintext is hex-encoded into the string on the way in, which
halves the effective plaintext budget; the cap is
`MAX_TLE_PLAINTEXT_BYTES = 380` bytes raw. The fix is
regression-tested in `tests/commitment/test_on_chain.py`, and the
end-to-end round-trip on testnet — submit → auto-decrypt →
byte-exact decode — completes in 105 seconds.

There's a generalisable lesson buried in that paragraph: *"the chain
accepted the extrinsic"* is not the same as *"the chain processed
the extrinsic correctly."* Substrate storage will write almost any
bytes you hand it; the runtime's interpretation of those bytes is
where the real work happens, and that's the gap where assumptions
die quietly. Every claim in this paper that depends on chain
processing has a probe behind it for exactly that reason.

### 7.5 Indexed Merkle trees

A standard Merkle tree lets you prove "X is a leaf of this tree." An
indexed Merkle tree (IMT) goes one step further. Every leaf carries
a `(key, value, next_key)` triple, where `next_key` points to the
next-larger key in sorted order. With that structure you can also
prove "X is NOT a leaf of this tree" — exhibit a leaf whose key is
less than X and whose `next_key` is greater than X. No leaf can fit
in between, so X cannot be in the tree.

That non-inclusion proof is the property we actually need. We use
IMT roots for two things:

- **Miner commits root** (in 9.C.1): leaves are
  `(miner_hotkey, blake2b(k_block || k_round || sha256_ct))`. Any
  verifier can check whether a given miner's chain commit was
  considered by the validator at scoring time — and, just as
  importantly, whether a miner who claims to have been wrongly
  excluded actually appears in the tree at all.
- **Final score root** (in 9.C.2): leaves are
  `(miner_hotkey, score_micro)`. Same check, but for the score the
  validator awarded.

The IMT root itself is 32 bytes. Inclusion and non-inclusion proofs
run ~16 hashes (2,048 bits) per query. Cheap to compute, cheap to
verify — which is what makes "every miner can audit their own slot"
practical rather than theatrical.

---

## 8. The eight-check scoreability rule

Eight checks decide whether a miner's submission is scoreable for an
epoch. Validator Vera reads each miner's revealed Layer 9.B bundle and
the archived AES_ct, then runs the checks below in order. Any failure
excludes that miner from the epoch with a discrete reason.

| # | Name | What it catches |
|---|---|---|
| 1 | `ON_CHAIN_PRESENT` | Miner has no revealed 9.B bundle → exclude (no submission, or commit was malformed and never auto-decrypted) |
| 2 | `CIPHERTEXT_MATCH` | SHA-256(AES_ct) doesn't match the bundle's `sha256_ct` → archive served wrong bytes |
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

### 8.1 Worked example — Adversary Adam tries to rewrite Mike's prediction

Adam controls the validator. Mike has submitted his epoch as in §5.

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

## 9. Three-tier archive durability

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

## 10. Validator architecture: primary + shadow

A single validator scoring all miners is a weakness, no matter how
honest. Validator Vera could be subtly wrong, compromised, or
strategically dishonest — and a single verifier checking only her
work cannot tell the difference.

The defence is a **shadow validator**: a parallel operator-run
process on a separate hotkey, a separate host, ideally a separate
cloud provider. The shadow:

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

### 10.1 The rubber-stamp attack and why it doesn't work

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

### 10.2 What the shadow doesn't fix

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
change (see §14).

---

## 11. Adversarial defense matrix

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

## 12. Economic model

SN21 is a Bittensor subnet. The token economics are inherited from the
chain, not invented by us.

### 12.1 How TAO flows

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

### 12.2 What weights are based on

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

> **Status (launch).** The tiered allocator is **available** in
> `hope/validator/tiered_weights.py:TieredAllocator` and fully unit-
> tested (`tests/validator/test_tiered_weights.py`), but it is
> **not the default path in the production validator runner**. The
> default `hope-validator` CLI constructs
> `WeightSetter(burn_fraction=0.95)` and calls `normalize_scores(...)`
> — simple linear normalisation of raw scores + 95% burn. Operators
> who want tier mechanics on day one wire it explicitly:
> ```python
> setter = WeightSetter(burn_fraction=0.95,
>                       tiered_allocator=TieredAllocator())
> uids, weights, allocation = setter.allocate_tiered(...)
> ```
> Tier mechanics are scheduled to become the runner default after
> Review 1, when we have enough operational data to tune the gate
> thresholds and Elite floor against real miner behaviour.
> Epoch-type multipliers and the diversity bonus are roadmap
> (Review 2 / Review 3).

### 12.3 Worked example — 20 miners, one epoch

Concrete numbers help. Twenty miners submit predictions in one epoch.
Their raw scores (after the conditional-prior gate has already
filtered, on a 0.0–1.0 scale) are:

| Group | Count | Raw score | Coverage |
|---|---|---|---|
| Top performers | 4 | 0.85 each | 100% |
| Mid performers | 8 | 0.70 each | 100% |
| Lower performers | 6 | 0.55 each | 100% |
| Below baseline | 1 | 0.40 | 100% |
| Below coverage gate | 1 | 0.80 | 60% |

**Step 1 — Participation gate.** The two miners at the bottom of the
table fail. The "below baseline" miner does not beat the published
prior. The "below coverage gate" miner submitted on only 60% of
episodes (gate is 80%). Both are excluded. **18 miners qualify.**

**Step 2 — EMA tier placement.** With 18 ≥ 15 the tier split is on.
The four-epoch EMA (alpha = 0.5) is computed for each miner; in this
example all 18 had the same score in the prior epoch, so the EMA
matches the current raw score. Sorted by EMA:

- Top 20% → Elite candidates: 4 miners at 0.85 (`round(18 × 0.20) = 4`).
- Next 40% → Competitive: 7 miners at 0.70 (`round(18 × 0.40) = 7`).
- Bottom 40% → Participating: the remaining 7 (1 at 0.70, 6 at 0.55).

**Step 3 — Elite quality floor.** The floor is `mean_baseline +
1·sigma_of_(raw − baseline)`. With baseline 0.50 across all 18 and
varied raw scores (0.85 / 0.70 / 0.55), `sigma ≈ 0.116` and the floor
sits at `0.50 + 0.116 = 0.616`. All 4 Elite candidates score 0.85,
clearly above 0.616 — Elite forms.

**Step 4 — Pool allocation (proportional within tier).**

| Tier | Members | Pool share | Per-miner weight (within tier) |
|---|---|---|---|
| Elite | 4 × 0.85 | 60% | `0.60 × (0.85 / 3.40) = 0.150` each |
| Competitive | 7 × 0.70 | 30% | `0.30 × (0.70 / 4.90) = 0.0429` each |
| Participating (1×0.70, 6×0.55) | 7 | 10% | proportional to current score |

**Step 5 — Apply burn (95% to UID 0).** Multiply every miner's
allocation by `(1 − 0.95) = 0.05`, give the rest to UID 0. The Elite
miners' chain weight becomes `0.150 × 0.05 = 0.0075` of total
emissions each.

**Concrete TAO numbers.** Suppose this epoch's miner pool is **1,000
α** (illustrative — actual values are network-determined and follow
Yuma stake-weighted median across validators). After the 95% burn,
**50 α** is distributed to the 18 qualifying miners across the three
tiers:

- Each Elite miner: `50 × 0.60 / 4 = 7.5 α`.
- Each Competitive miner at 0.70: `50 × 0.30 / 7 ≈ 2.14 α`.
- The single Participating miner at 0.70 + 6 at 0.55:
  proportional split of `50 × 0.10 = 5 α`. Working out the
  shares: top Participating gets `5 × (0.70 / 4.00) = 0.875 α`;
  each lower Participating gets `5 × (0.55 / 4.00) ≈ 0.6875 α`.
- The 2 excluded miners: **0**.

The math is in `hope/validator/tiered_weights.py` and pinned by
`tests/validator/test_tiered_weights.py`. A reader who wants to
replay the table can run those tests.

### 12.4 Why this is competitive, not extractive

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

## 13. Edge cases we've thought through

A protocol is only as good as its handling of the unhappy paths. The
ones below are designed for, not deferred.

### 13.1 drand pulse outage

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

### 13.2 Archive loss

If both Tier-2 (operator) and Tier-3 (miner self) lose a specific miner's
AES_ct between submission and scoring time, the validator can't
retrieve the bytes and excludes the miner with `plaintext_unavailable`.
The 9.C.6 retry log records the attempts, which a verifier can
reproduce.

This is failure mode by design. We chose three independent storage
tiers specifically because no single archive can be trusted; the
exclusion is the protocol working correctly under partial failure.

### 13.3 MaxSpace contention

A miner's epoch costs ~2,174 bytes against the chain's ~3,100-byte
MaxSpace per ~4-hour rolling window (empirical, measured on testnet).
A miner submitting more than once per ~4.5 hours from the same hotkey
will hit `SpaceLimitExceeded`.

**Mitigation in code:** the miner runner returns
`MinerSubmissionResult.failure_reason="chain_*_commit_failed: SpaceLimit..."`
and operators wait for the rate-limit window to clear before retrying.

**Architectural mitigation:** SN21's launch epoch cadence is **weekly**
(see `docs/SN21_EPOCH_STRUCTURE.md` and `docs/MINER_ECONOMICS.md`).
That is the protocol-level cycle for scoring + payout. The "4.5
hour" number is something different: it is the chain's **MaxSpace
rate-limit floor** below which a single hotkey can't reliably commit
again. As long as miners submit at most once per epoch (weekly), they
sit comfortably above this floor with ~37× headroom. The 4.5h floor
matters only for operators who run short experimental epochs — not
for the launch cadence.

### 13.4 Validator MaxSpace contention

The validator's per-epoch cost is ~1,960 bytes (without retry log) or
~2,492 bytes (with). One validator + one epoch fits a window. The
9.C.6 retry log is only emitted when at least one miner was excluded
for `plaintext_unavailable` — so in healthy operation the validator
uses ~63% of its window budget.

Validators running shadow + primary on the SAME wallet on the SAME
epoch would bust MaxSpace; we explicitly require shadow on a separate
hotkey. Operators run shadow on a separate hotkey to avoid this.

### 13.5 Hotkey ↔ ed25519 binding loss

Every miner and validator publishes a 109-byte `Raw{N}` registration
commit binding their Bittensor hotkey to an ed25519 public key. Lose
the ed25519 PEM and you can't sign new predictions until you re-register.

**Recovery procedure**:
1. Generate a new ed25519 key (`scripts/sn21_keys.py generate`).
2. Register the new binding on chain (`sn21_keys.py register`).
3. The new commit overwrites the old binding in `CommitmentOf`.
4. Existing in-flight commits using the old key remain verifiable until
   their reveal_round; new commits must use the new key.

**Architectural caveat:** the chain's `CommitmentOf` storage holds ONLY
the latest commit per (netuid, hotkey). Historical bindings are NOT in
chain head state — auditing them requires an archive node. Operator
operators must use an archive node to read past-epoch state.

### 13.6 Disputed scoring

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

## 14. What we deliberately defer

Some properties are roadmap, not launch. The list below is honest
about which, and why.

### 14.1 Chain-side weights ↔ scoring binding

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

**Long-term fix:** an upstream Bittensor runtime change that adds a
32-byte `external_anchor` field to `WeightsTlockPayload`. With that
field, the chain itself binds weights to the scoring artifact — no
off-chain check needed. The proposal will be submitted to the
Subtensor maintainers once the on-chain protocol has run cleanly for
one operational cycle.

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

### 14.2 Per-episode scoring commitments

We added per-episode artifacts: each miner's CBOR bundle of
per-(episode × horizon) quantiles, with an IMT root committed inside
the aggregated 9.C.1 plaintext. This lets the verifier reproduce
per-episode scoring against the specific predictions for that episode.

What we have NOT done is split the on-chain commit into one per
episode. A 100-episode epoch would need 100 chain commits per miner;
MaxSpace forbids it. Per-episode artifacts live off-chain in archives
with a chain-anchored root. This is a deliberate trade-off: cheaper
chain footprint, slightly more off-chain trust.

> **Status (launch).** Per-episode primitives are **available** as
> `submit_miner_epoch(per_episode_entries=...)` —
> `episodes_root` (IMT root over per-(episode, horizon) entries) and
> `episodes_bundle_sha256` (SHA-256 of the bundle bytes) get bound
> inside the inner_sig'd aggregated plaintext, and the verifier
> scores per-(episode × horizon) via `score_one_miner_per_episode`.
> However, the default `hope-miner` CLI does **not** pass
> `per_episode_entries` at launch — it converts predictions into
> aggregate per-horizon entries via
> `_predictions_to_horizon_entries(...)` and submits those. Miners
> that want exact per-episode binding wire their own caller around
> `submit_miner_epoch(...)`. Default CLI behaviour will switch to
> per-episode after operational-cycle-1, once the field is exercised
> end-to-end on mainnet.

### 14.3 Privacy of predictions

A miner's predictions become public after the chain auto-decrypts K
and verifiers fetch the AES_ct. There is no privacy mechanism for
"ship a prediction nobody else can ever see, including the operator."

This is by design. Auditing requires plaintext access. A miner who
wants their model's outputs private should not participate in a
verifiable prediction subnet; they should sell predictions privately.

### 14.4 Mainnet TAO fees

Testnet measurement of `set_commitment` extrinsic fees on testnet 466 at
0 µTAO. Mainnet may differ. We'll measure once before the mainnet
flip; pre-launch operators measure this on mainnet before opening miner registration.

### 14.5 Known operational limits

A handful of empirical limits the implementation has hit and routed
around:

- **Multi-field chain commits are unsupported in practice.** The
  `Commitments` pallet accepts a multi-variant `info.fields[0]`, but
  the auto-decrypt subsystem silently skips multi-variant slots and
  SDK readback can't see them. We use the 3-extrinsic per-miner path.
- **TLE plaintext budget is 380 bytes raw**, not 768 — the chain's
  auto-decrypt requires hex-encoded payloads via
  `get_encrypted_commitment`, which doubles the byte count. The 9.C.1
  / 9.C.2 builders fit comfortably for ≤ 50-miner epochs; populations
  of 200+ would need split commits across multiple extrinsics.
- **SDK lossy UTF-8 readback.** Bittensor SDK 10.2.1's
  `get_revealed_commitment_by_hotkey()` mangles binary bytes >127.
  Bypassed by `hope/commitment/chain_reader.py` which queries
  substrate directly.
- **MaxSpace is per-byte, not per-call.** ~3,100 bytes per (netuid,
  hotkey) per ~4-hour window. Per-extrinsic byte cost includes ~500
  bytes of overhead.

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
chain hides — the auto-decrypt format bug was the most
expensive (§7.4 narrates that one) — but every discovery tightened the
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

Live count: `pytest tests/ --collect-only -q | wc -l`. Layout:

```
tests/commitment/   crypto primitives + chain commit helpers
tests/scoring/      scoring components + adapter + skill score
tests/validator/    validator runtime + tier mechanics
tests/miner/        miner runtime + on-chain submitter
tests/scripts/      public verifier (verify_epoch.py)
tests/e2e/          full miner flow against a live validator
tests/adversarial/  every claimed defence has a passing attack test
tests/fixtures/     recorded-epoch fixture for verifier round-trip
```

Adversarial tests are in `tests/adversarial/test_attack_surface.py`.
Each test stages an attack scenario from the architecture's threat
model, runs the protocol's defense, and asserts the defense fires. If
any test passes a malicious payload through, the build fails CI.

Run locally: `pytest tests/`. Run only adversarial: `pytest tests/adversarial/ -v`.

---

## Appendix B — Empirical findings (testnet 466)

The architecture's claims are backed by testnet measurement.

| Property | Value | Implication |
|---|---|---|
| MaxSpace per (netuid, hotkey) per ~4-hour window | ~3,100 bytes | Min epoch cadence ≈ 4.5h; 24h cadence has 5× headroom |
| Per-commit byte overhead | ~500 bytes | Plus payload bytes count toward MaxSpace |
| `set_commitment` extrinsic fee on testnet | 0 µTAO | Mainnet TBC pre-launch |
| `Raw{N}` capacity via `subtensor.set_commitment(data: str)` | N ≤ 128 | Use `publish_metadata_extrinsic` for larger / Sha256 / TimelockEncrypted |
| TLE plaintext budget (raw bytes) | 380 | Hex-encoded for `get_encrypted_commitment`, doubling on-the-wire byte count |
| TLE auto-decrypt latency (from submit to revealed plaintext on chain) | ≈ 105 seconds | At quicknet 3s period + ~100-round safety margin |
| Multi-field commits (`info.fields[0]` with multiple variants) | Accepted by chain, NOT auto-decrypted | Use 3-extrinsic Layer 9.B path |

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

### C.4 Companion documents in this repo

- `docs/SN21_REWARD_MECHANISM.md` — full reward spec.
- `docs/SN21_EPOCH_STRUCTURE.md` — epoch progression.
- `docs/miner_quickstart.md` — miner onboarding tutorial.
- `docs/MINER_ECONOMICS.md` — short reference for emissions.
- `docs/validator_setup.md` — validator deployment guide.

### C.5 Code

The protocol implementation is in this repository; commit history is
the audit trail.

---

*v1.0. All claims in this paper are linked to source files in this
repository and to commit hashes on `main`. Verify directly.*
