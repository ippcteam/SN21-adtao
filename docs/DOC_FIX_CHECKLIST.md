# SN21 — Documentation fix checklist (daily stream cutover)

| | |
| :---- | :---- |
| **Audience** | Developer / docs owner |
| **Purpose** | Close miner-facing gaps without rewriting working technical code |
| **Created** | 2026-08-04 |
| **Status** | Open |

This checklist comes from a miner-eye review of the repo as an instruction set.  
**Do not treat checked items as done in code** — only mark when the *documentation* (and any linked tooling docs) actually resolve the gap.

**Authoritative daily-stream docs (keep / extend):**

- [docs/miner_quickstart.md](./miner_quickstart.md)
- [docs/MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)
- [docs/SN21_SCORING.md](./SN21_SCORING.md)
- [docs/SN21_REWARDS.md](./SN21_REWARDS.md)
- [docs/SN21_STAKING.md](./SN21_STAKING.md)
- [docs/SN21_TRANSITION_PLAN.md](./SN21_TRANSITION_PLAN.md)
- [docs/DAILY_STREAM_DIRECTION.md](./DAILY_STREAM_DIRECTION.md)

**Decided:** daily miner-submission wall clock is **midnight EST** each day (published in quickstart §3, MINER_MODEL_SPEC §4, SN21_SCORING, transition plan, DAILY_STREAM_DIRECTION).

**Public site (human-readable mirror + ops UI):** [https://adtao.io/sn21/](https://adtao.io/sn21/)  
Leaderboard, changelog, and mirrored docs live there. The site is being updated for the daily stream **during the week of 4 August 2026**. Repo docs remain authoritative where the site disagrees (as the site itself states). Until the site cutover finishes, miner docs should still point miners at `/sn21/` for leaderboard / changelog, and call out that mirrored pages may briefly lag the repo.

---

## How to use

1. Work top → bottom within each priority band (P0 first).
2. Prefer editing the authoritative daily docs; avoid expanding obsolete weekly specs.
3. When a fix needs new tooling, document the **current** operator/Discord path and file a code TODO — do not invent CLIs in prose.
4. Check the box only when a miner could follow the written steps without asking Discord for basics.

---

## P0 — Front door and authority

- [ ] **Rewrite [README.md](../README.md) miner path** for daily stream (container + digest, not weekly `hope-miner` / TLE archives).
- [ ] **README links** point to scoring / rewards / staking / transition / model spec / quickstart — **not** to obsolete reward/epoch/economics specs as primary.
- [ ] **README “How it works / scoring / emissions”** sections match daily standing + weight curve (or clearly say “see SN21_SCORING / SN21_REWARDS”).
- [ ] **Finalize [SN21_WHY_DAILY.md](./SN21_WHY_DAILY.md)** (draft why doc) — then retire or pointer-ize [DAILY_STREAM_DIRECTION.md](./DAILY_STREAM_DIRECTION.md).
- [ ] **README (or quickstart §1)** includes a short “why daily” blurb linking to the finalized why doc.
- [ ] Discord / site announcement **derived from** finalized `SN21_WHY_DAILY.md` (not a third source of truth).

---

## P0 — Archive obsolete weekly miner specs

- [ ] Move or clearly quarantine weekly specs so miners do not treat them as current, e.g. `docs/archive/weekly/` **or** keep in place with a hard banner + README purge:
  - [ ] [SN21_REWARD_MECHANISM.md](./SN21_REWARD_MECHANISM.md)
  - [ ] [SN21_EPOCH_STRUCTURE.md](./SN21_EPOCH_STRUCTURE.md)
  - [ ] [MINER_ECONOMICS.md](./MINER_ECONOMICS.md)
- [ ] Grep docs + README + CONTRIBUTING for links to the three files above; retarget to SN21_SCORING / SN21_REWARDS / SN21_TRANSITION_PLAN / SN21_STAKING.
- [ ] [whitepaper.md](./whitepaper.md) — mark weekly reward/tempo sections historical **or** update; do not leave them sounding authoritative for launch.

---

## P0 — Submit → verify → score / rank loop

Miner must have a written path for each. If tooling is missing, say so explicitly and give the interim operator channel — do not leave a cliff.

### Submit

- [ ] Document the **exact** on-chain commit step for `sn21-model:v1:<repo>@sha256:<digest>` (CLI if it exists; otherwise copy-paste extrinsic / script once available).
- [ ] Document registry requirements (public pull, digest pin, auth if any).
- [ ] Document gate intake: who runs it, when, how miners are notified of pass/fail.
- [ ] Document that live baskets are **operator-executed** (miner does not POST daily predictions).
- [x] Publish **daily cut-off**: **midnight EST** each day — in MINER_MODEL_SPEC + quickstart + scoring + transition + direction note.

### Check submission succeeded

- [ ] How to confirm digest commitment is on chain for your hotkey.
- [ ] How to confirm intake pulled the matching `RepoDigests`.
- [ ] How to confirm **gate admission** (pass/fail URL, Discord post, or file — whatever is real).
- [ ] How to confirm the subnet **ran your image** for a given `BD-YYYY-MM-DD` (coverage / predictions_out).
- [ ] How to confirm bridge “submitted” (≥50% coverage) for a day.

### Score and rank

- [ ] Where to read **standing** (moving-average score) for your hotkey.
- [ ] Where to read **rank / weight share** under the curve.
- [ ] Where to read **champion** vs earner (if published).
- [ ] Point miners at **[https://adtao.io/sn21/](https://adtao.io/sn21/)** for **Leaderboard** and **Changelog** (and note the site is being updated for daily stream this week — mirrored docs there may briefly show weekly-era pages).
- [ ] When the site daily cutover lands: update quickstart / README links so score/rank instructions match the new leaderboard UX (not weekly epoch scores only).
- [ ] Interim: if a metric is not yet on the site, document the accuracy feed / Discord fallback and ETA — do not leave “see scoring doc” as the only answer.

---

## P1 — Training data cookbook

- [ ] Quickstart (or new `docs/SN21_TRAINING.md`) with a single path:
  1. Load `data/episodes/` + `data/outcomes/` (link existing READMEs).
  2. Join to `(input, outcome)` pairs.
  3. Train / export weights.
  4. Bake into container.
  5. Local NDJSON → `docker run` smoke test.
  6. Optional: local score vs outcomes (gate-like or `score_predictions` — only if accurate for daily formula).
- [ ] State clearly what today’s public data **is** (mostly weekly `WR-*`, t7/t14) vs what the live contract needs (daily `BD-*`, 28d).
- [ ] Document **where the 4 Aug training bundle** is fetched (URL, Discord, release asset) — currently promised in transition plan, not linked from repo.
- [ ] Resolve or document **payload shape**: nested export `input` vs reference_model flattened fields vs “episode payload v2.0” in MINER_MODEL_SPEC.
- [ ] Point `train_example_model.py` usage at “pipeline check” vs “competitive daily model” honestly.

---

## P1 — Consistency passes

- [ ] Horizons: all miner-facing docs say **7 / 14 / 28** (except archived weekly material).
- [ ] Schema version: one story (v1.9 exports vs v2.0 contract) — migrate, dual-support note, or explicit mapping table.
- [ ] Stake ladder: docs (`150→…→1000`) vs `hope/scoring/collateral_floor.py` `ALPHA_LADDER` — align code **or** document calendar dates as sole authority and that code lag is known.
- [ ] Scoring formula: document whether production settle uses full 50/20/15/15 or simplified gate pair; match [SN21_SCORING.md](./SN21_SCORING.md) to reality.
- [ ] Language: “you predict each day” → “your admitted container is run each day” everywhere miner-facing (except archived).
- [ ] Burn: keep “indicative / may change to protect alpha” everywhere burn numbers appear (including README if reintroduced).

---

## P2 — Operator / secondary docs (label or update)

- [ ] [validator_setup.md](./validator_setup.md) — weekly windows / hope-miner examples: update or banner “pre-daily / operator historical”.
- [ ] [validator_daemon.md](./validator_daemon.md) / [validator_operational.md](./validator_operational.md) — note daily_loop / shadow path vs weekly scoring timer.
- [ ] [deploy/validator_scoring/](../deploy/validator_scoring/) READMEs — weekly Monday timer vs daily stream; don’t leave as the implied miner truth.
- [ ] [CONTRIBUTING.md](../CONTRIBUTING.md) — stop requiring updates to obsolete epoch/reward specs as the primary path.

---

## P2 — Quickstart polish

- [ ] Add “Reading order” at top: Direction → Transition → Quickstart → Scoring → Rewards → Staking → Model spec.
- [ ] Keep registration section intact (already good); only add cross-links.
- [ ] Add troubleshooting for: gate fail, digest mismatch, zero predictions, stake below floor, bridge miss decay.
- [ ] Remove or relocate any remaining weekly-only leftovers if found in later edits.

---

## P2 — Website sync ([adtao.io/sn21](https://adtao.io/sn21/))

Site is a **derivative mirror** of repo + chain; update in parallel with repo docs this week.

- [ ] Replace mirrored links currently pointing at weekly **epoch structure** / **reward mechanism** with daily suite (scoring, rewards, staking, transition, model spec, quickstart) — or retire those mirrors.
- [ ] Leaderboard UX: daily standing / rank / weights (not only weekly epoch scores).
- [ ] Changelog: cutover + daily-stream notes as they ship.
- [ ] After site update: confirm README + miner_quickstart “where to check score/rank” match live `/sn21/` navigation.
- [ ] Keep the site disclaimer: chain + [github.com/ippcteam/SN21-adtao](https://github.com/ippcteam/SN21-adtao) win on disagreement.

---

## Done when (acceptance)

A new miner can answer from docs alone:

1. Why we moved to daily.  
2. How to register.  
3. How to build, push, and commit a model.  
4. How to know admission and daily runs succeeded.  
5. How to train from published data (and what its limits are).  
6. How to read score / rank / weight.  
7. What stake and indicative burn apply on which dates.  
8. Which old weekly docs to ignore.

---

## Out of scope for this checklist

- Implementing `SN21_DAILY_STREAM_WEIGHTS` production cutover (code/ops).
- Changing Bittensor emissions methodology.
- ~~Inventing a daily cut-off time before the product decision lands.~~ → **Decided: midnight EST daily.**

---

## Progress log

| Date | Item | Notes |
| :---- | :---- | :---- |
| 2026-08-04 | Checklist created | From miner-eye review; no doc rewrites in that pass beyond prior suite |
| 2026-08-04 | Website note added | Leaderboard/changelog at https://adtao.io/sn21/ — site updating this week for daily stream |
| 2026-08-04 | Daily wall clock | Miner submissions: **midnight EST** daily — quickstart, model spec, scoring, transition, direction |
