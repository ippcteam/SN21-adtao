# Build Journey — SN21

> **Historical record.** This describes the build of the **weekly era**, up to
> 5 May 2026. SN21 moved to a daily stream on 4 August 2026: tiered
> Elite / Competitive / Participating bands and four-epoch EMA pools described
> below are retired, and the test and line counts are as they stood then.
> For how the subnet works today, start at
> [SN21_WHY_DAILY.md](./SN21_WHY_DAILY.md) and
> [SN21_REWARDS.md](./SN21_REWARDS.md). The cryptographic and verification
> machinery described here still stands.

How this subnet was built, phase by phase, with the receipts.

The whitepaper (`docs/whitepaper.md`) describes the protocol as it
ships. This doc describes the **work that produced it**: which decisions
came first, which assumptions broke against real testnet, and what
actually got committed in each phase. Every claim links to a concrete
artifact in this repo (a file path, a test, or a commit hash on `main`).

The protocol was built between **2026-04-30 and 2026-05-04** — five
days of intense work — followed by an audit-feedback wave on
**2026-05-05** that closed the remaining design-call gaps. An AI
coding agent (Claude) authored substantial parts of the test
scaffolding and prose; design choices, empirical validation, and
chain-side debugging were human work. See whitepaper §0 for the
disclosure.

---

## Phase 0 — Testnet calibration (2026-05-03)

Before writing protocol code we measured the chain. Five empirical
questions had to be answered before anything else made sense:

| Q | Question | Verdict | Where |
|---|---|---|---|
| Q11 | What is the MaxSpace rate-limit window per (netuid, hotkey)? | **1,259 blocks ≈ 252 min ≈ 3.5× tempo.** Per-byte limit, not per-call. | Two iterations needed (Q11 v1 used 17-byte payloads — too small to trip the limit; v2 with 128-byte payloads got the answer). |
| Q13 | Mainnet TAO fee per `set_commitment` extrinsic? | **0 µTAO on testnet 466.** Mainnet measurement deferred to launch. | — |
| Q34 | Does the `timelock` PyPI package work? | **No** — only dev wheels published. `bittensor_drand.encrypt(...)` covers TLE. | — |
| Q35 | Lower-level `publish_metadata_extrinsic` vs higher-level `set_commitment`? | **Lower-level wins.** The string helper wraps payloads in hex/utf-8 wrappers that waste plaintext capacity. | `hope/commitment/on_chain.py` |
| Q36 | Multi-field commit (`info.fields[0] = [Sha256, TimelockEncrypted, Raw]`)? | **Chain accepts the extrinsic, but auto-decrypt does NOT walk multi-variant slots, AND SDK readback can't see them.** Three separate extrinsics is the authoritative path. | `submit_layer_9b_multi_field` is gated with `NotImplementedError`. |

These calibrations made the protocol concrete: epoch cadence ≥ 4.5h,
one extrinsic per commit, and `bittensor_drand` is the TLE library.

---

## Phase A — Foundation modules (2026-04-30 → 2026-05-01)

Pure-Python cryptographic primitives, no Bittensor dependency. Each
module has a focused unit test surface.

| Module | Lines | Responsibility |
|---|---|---|
| `hope/commitment/canonical.py` | ~60 | RFC 8949 §4.2.1 canonical CBOR + AES-GCM AAD construction |
| `hope/commitment/drand_lib.py` | ~75 | drand quicknet round math + chain hash constants |
| `hope/commitment/imt.py` | ~245 | Indexed Merkle tree (sorted-leaf + low/high pointers) — supports both inclusion and non-inclusion proofs |
| `hope/commitment/inner_sig.py` | ~120 | ed25519 signature over `blake2b_256(canonical_cbor(plaintext sans inner_sig))` |
| `hope/commitment/on_chain.py` | ~480 | High-level chain commit helpers for Sha256 / TimelockEncrypted / Raw{N} variants |

**Test surface at the end of Phase A:** ~101 unit tests, all passing.

---

## Phase B — Commit-reveal protocol layers (2026-05-01)

Layer 9.A / 9.B / 9.C end-to-end. The cryptographic shape of the
protocol from this point is what ships at launch.

| Layer | Built | Where |
|---|---|---|
| 9.A.1 release_commit | ✅ | `hope/hope_outcomes/release_commit.py` |
| 9.A.2 reveal_blob | ✅ | `hope/hope_outcomes/reveal_blob.py` |
| 9.B miner CBOR + AES-GCM envelope | ✅ | `hope/commitment/prediction_payload.py` |
| 9.B.1 per-episode artifact bundle (Phase E) | ✅ | `hope/commitment/episode_artifacts.py` |
| 8-check scoreability rule | ✅ | `hope/commitment/scoreability.py` |
| 9.C.1 pre-scoring state builder | ✅ | `hope/commitment/scoring_state.py:build_pre_scoring_state` |
| 9.C.2 post-scoring artifacts builder | ✅ | `hope/commitment/scoring_state.py:build_post_scoring_artifacts` |
| 9.C.3 weights commit wrapper | ✅ | `hope/validator/weights_commit.py` |
| 9.C.6 retry log builder | ✅ | `hope/commitment/retry_log.py` |

---

## Phase C — Validator orchestration + archive server (2026-05-01 → 2026-05-02)

| Component | Where |
|---|---|
| Three-tier archive client | `hope/commitment/archives.py` |
| Archive server (FastAPI) | `hope/archive_server/{app,store,metrics}.py` |
| Validator chain reader | `hope/validator/onchain_reader.py` |
| Validator chain orchestration | `hope/validator/onchain_runner.py:run_epoch_scoring` |
| Miner runner integration | `hope/miner/runner.py:run_epoch_onchain` |
| Public verifier | `scripts/verify_epoch.py` |
| Shadow validator runner | `hope/hope_shadow_validator/runner.py` (SH-5 rubber-stamp defence proven by adversarial test) |

---

## Phase D — Adversarial test surface (2026-05-02)

12 explicit attack scenarios, each with a defence and a test that
proves the defence fires. Run locally:

```bash
pytest tests/adversarial/ -v
```

Sample of scenarios covered:

* Validator rewrites miner predictions → `inner_sig` invalid after re-encrypt
* Cross-epoch ciphertext replay → AES-GCM AAD = `b"sn21-prediction-v1:" + epoch_id`
* Tampered AES_ct in archive → on-chain `Sha256` commit mismatches
* Forged miner identity → `inner_sig.miner_hotkey` ≠ chain account
* Rubber-stamp shadow → `inner_sig.validator_hotkey` ≠ shadow's chain account
* Tampered episode entry → IMT root mismatch on `episodes_root`

Full list in `tests/adversarial/test_attack_surface.py`.

---

## Phase E — Per-episode artifacts (2026-05-03)

The Phase B miner CBOR carried one entry per horizon for the whole
epoch. Phase E added an off-chain bundle of per-(episode × horizon)
quantiles whose IMT root is committed inside the aggregated TLE
plaintext. This lets verifiers reproduce per-episode scoring.

* Bundle schema + builder: `hope/commitment/episode_artifacts.py`
* Aggregated plaintext fields: `episodes_root` + `episodes_bundle_sha256`
* Verifier path: `score_one_miner_per_episode` in `hope/scoring/onchain_adapter.py`

The miner submission path accepts `per_episode_entries=...` —
opting in is per-miner, not a protocol-version bump.

---

## Phase F — Local + testnet drivers (2026-05-03)

End-to-end integration tests on real chain.

* `tests/e2e/test_miner_flow.py` — 15 end-to-end miner tests with real ed25519 signatures
* Five testnet 466 commits proven on-chain across registration, 9.C.1 / 9.C.2 / 9.C.3 (`pre_scoring`, `post_scoring`, `weights`)

---

## Phase G — Substrate-direct chain reads (2026-05-03 → 2026-05-04)

The Bittensor SDK 10.2.1's `get_revealed_commitment_by_hotkey()`
lossily UTF-8 decodes binary chain bytes — codepoints > 127 mangle
into multi-byte sequences. A 32-byte AES key K came back as garbled
string when read this way.

**Fix:** drop to `subtensor.substrate.query("Commitments",
"RevealedCommitments", ...)` directly; convert SCALE int-tuples via
`bytes(t)`. Implementation: `hope/commitment/chain_reader.py`.

---

## Phase H — Chain auto-decrypt format bug (2026-05-04)

The most expensive surprise. Phase B used
`bittensor_drand.encrypt(bytes, n_blocks, block_time)` —
binary-in, binary-out, ostensibly the obvious helper. The chain
accepted our `TimelockEncrypted` extrinsics with `success=True`.

It was wrong.

**H-3 (probe):** submitted a known 32-byte plaintext, polled
`RevealedCommitments` for 30 minutes past the reveal_round, observed
nothing. We had assumed `success=True` meant auto-decrypt would fire;
in fact the chain runtime silently drops ciphertexts whose format it
doesn't recognise.

**H-4 (diagnosis):** read the SDK source, found that
`Subtensor.set_reveal_commitment(...)` calls a DIFFERENT C function —
`bittensor_drand.get_encrypted_commitment(data: str, ...)` — and that's
what the chain runtime decodes. The two functions look identical at the
Python signature level; their underlying ciphertext formats are not.

**H-6 (fix + verification):** hex-encode binary plaintext, call the
right C helper. Re-ran the round-trip on testnet 466:

```
plaintext (hex):    0xc0ffee64284082626c6ebdf1b074c9afdeadbeef
submit:             success=True reveal_round=28367904
auto-decrypt fired: block 7049220, 105 seconds after submission
decode round-trip:  byte-exact match
```

Cost of the lesson: halved the TLE plaintext budget from 768 → 380
bytes (hex doubles byte count). The 9.C.1 / 9.C.2 builders fit (real
plaintexts measure 364–380 bytes for realistic 50-miner epochs), but
barely. A future Phase E follow-up addresses populations larger than
~200 miners by splitting commits across multiple extrinsics.

The lesson generalises: *"the chain accepted the extrinsic"* is not the
same as *"the chain processed the extrinsic correctly."* Substrate
storage is permissive about what gets written; the runtime's
interpretation is where the work actually happens. Every claim in the
whitepaper that depends on the chain processing something correctly
has a probe behind it.

---

## Audit-feedback wave — design-call findings #1–#5 (2026-05-05)

External review surfaced five places where the whitepaper described
behaviour the production code didn't implement. The wave closed all
five at launch. Test count after the wave: **488 passing** (was 426).

| # | Gap | Resolution | Where to read it |
|---|---|---|---|
| **#5** | Whitepaper specifies `conditional_prior` baseline; code used `predict_zero` | `ConditionalPriorBaseline` schema + plumbing through `ScoringMetadata`; baseline values come from the release artifact; predict-zero fall-through when no prior is published | `hope/protocol/outcomes.py`, `hope/scoring/skill_score.py`, `tests/unit/scoring/test_skill_score_baseline.py` |
| **#3** | Whitepaper said per-episode predictions; chain submit was per-horizon aggregate | Per-episode IMT root + bundle SHA bound in aggregated plaintext; `score_one_miner_per_episode` for the verifier | `hope/commitment/episode_artifacts.py`, `hope/miner/onchain_submitter.py`, `tests/unit/commitment/test_episodes_root_binding.py` |
| **#4** | Whitepaper specified tier mechanics; weight-setter used flat normalisation | `TieredAllocator` enforces participation gate, four-epoch EMA tier placement, Elite/Competitive/Participating bands, Elite-floor redistribution, single-pool fallback | `hope/validator/tiered_weights.py`, `tests/unit/validator/test_tiered_weights.py` |
| **#2** | Whitepaper claimed weights ↔ scoring binding; chain doesn't enforce it | Verifier-side cross-check: re-derive expected u16 weights from score table, compare to `actual_weights_at_commit_block`. Adversarial test catches a forged 9.C.2. Chain-side anchor RFC tracked in `docs/proposals/q26_weights_payload_anchor.md` | `scripts/verify_epoch.py:_verify_weights_binding`, `tests/unit/scripts/test_verify_epoch.py:TestWeightsBinding` |
| **#1** | Public verifier had a placeholder scorer | `make_live_scorer` wraps the production `score_one_miner` adapter; `--truth-file` loads `HorizonTruth` from the 9.A.2 reveal blob. Recorded-epoch fixture + regression-guard test fails the build if the placeholder ever returns | `scripts/verify_epoch.py`, `tests/fixtures/recorded_epoch/recorded_epoch.json`, `tests/unit/scripts/test_verify_epoch_live_scorer.py` |

---

## What ships at v1.0

* **~7,500 LOC** code, **~6,800 LOC** tests
* **488 unit + adversarial + e2e tests** — `pytest tests/`
* **Lint-clean** under `ruff check` per `pyproject.toml` config
* **Public verifier live** — anyone can audit any epoch end-to-end
* **Tiered emissions live** — gate, EMA tiers, Elite floor, pool shares
* **Per-episode binding live** — off-chain bundle + on-chain root
* **Conditional-prior baseline live** — from release artifact
* **Weights-binding cross-check live** — verifier-side, awaiting upstream chain anchor

The whitepaper's launch-status table is the canonical record of what
ships and what's deferred. Every "live at launch" entry has a test in
`tests/` and a code path in `hope/`.

---

## What's next

* **Mainnet TAO fee measurement** — Q13 was 0 µTAO on testnet; mainnet may differ
* **Q26 upstream RFC** — submit the `external_anchor` patch to the Subtensor maintainers when the on-chain protocol has run cleanly for one operational cycle
* **Review 1 (after epoch 4)** — re-tune tier boundaries, baseline, Elite floor based on real data
* **Review 4 (after epoch 16)** — formal third-party validator programme (deployment guides, scoring spec reference implementation, operator coordination channels). Validator registration on SN21 is already open by Bittensor protocol; the programme is about convergence of registered operators on canonical scoring.

---

*Last updated 2026-05-05. Every artifact referenced above is a file in
this repo or a commit on `main`. Read the code; trust the tests; verify
on chain.*
