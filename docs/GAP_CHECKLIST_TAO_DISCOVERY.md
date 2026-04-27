# SN21 launch — gap checklist (tao-discovery / subnet repo)

**Scope:** Miners, validators, protocol, scoring, and `HopeDataClient` as implemented in **this** repository (`hope-sn21`).  
**References:** `SN21_LAUNCH_DEVELOPER_SPEC.md` §6 (v1.9 payload), §6.7 (validator-only outcomes), §7.5 (package JSON); `SN21_LAUNCH_RECONCILIATION_SUMMARY.md`.

**Not in scope here:** Database, classification, payload builder, release builder, HOPE package server, contamination guard — see [GAP_CHECKLIST_HOPE_BACKEND.md](./GAP_CHECKLIST_HOPE_BACKEND.md).

Use this as a clicklist: check off when fixed or explicitly accepted as permanent difference.

---

## 1. Constants and published launch parameters

| # | Spec / reconciliation | Repo today | Action |
|---|-------------------------|------------|--------|
| 1.1 | `LAUNCH_ACTION_TYPES` includes `BUDGET_CHANGE`, `BID_STRATEGY_CHANGE`, `CAMPAIGN_PAUSE`, `CAMPAIGN_ENABLE` (spec §3) | `hope/constants.py` lists `TARGET_VALUE_CHANGE`, omits `CAMPAIGN_ENABLE` | Align `LAUNCH_ACTION_TYPES` with launch list; map any separate tCPA/tROAS handling to doc (spec folds into `BID_STRATEGY_CHANGE`) or keep a named alias with comment |
| 1.2 | Launch-only constants: `LAUNCH_CONTAMINATION_BUFFER_DAYS`, `LAUNCH_MATURITY_CLOCK_DAYS`, `LAUNCH_RELEASE_MIN_EPISODES`, `LAUNCH_RELEASE_TARGET_EPISODES`, `LAUNCH_RELEASE_MAX_EPISODES`, `LAUNCH_CAMPAIGN_TYPES`, `LAUNCH_ACTION_SCOPES`, `MAX_ACTION_WINDOW_HOURS` (spec §3) | Not present in `hope/constants.py` | Add optional **documentation mirror** constants (or single `LaunchConfig` dataclass) so subnet code and docs cite the same numbers; no functional need if HOPE enforces |
| 1.3 | Pre-window length 60 days (reconciliation + spec §6.4) | `Episode` / `date_index` unparsed length in Pydantic | Add **optional** validation helper or test that `len(date_index) == 60` when `schema_version == v1.9` launch |

---

## 2. Protocol models vs §6 (v1.9 episode JSON)

| # | Spec | Repo (`hope/protocol/episode.py`) | Action |
|---|------|-------------------------------------|--------|
| 2.1 | `action_bundle.actions[].guardrails` / portfolio in spec examples | `Action` model: no `guardrails` on action (spec sometimes nested) | Confirm against `miner_data_delivery_structure.md`; align model if HOPE emits extra fields (use `model_config` extra allow or explicit fields) |
| 2.2 | `bundle_summary` fields in spec §6.5 | `BundleSummary` missing some keys from spec example (e.g. `dominant_scope` casing, full `source_mix`) | Diff against final HOPE JSON; extend `BundleSummary` or document "forward compatible" parsing |
| 2.3 | `account_state` TRUST fields: `guardrails` in spec shows `impact` | `Guardrail` has `active` not `impact` | Align with HOPE payload or add optional `impact` |

---

## 3. Validator-only outcomes vs §6.7

| # | Spec | Repo (`hope/protocol/outcomes.py`, `data_client._parse_outcome`) | Action |
|---|------|------------------------------------------------------------------|--------|
| 3.1 | `outcomes.t7` / `t14` include `conversion_value_delta_pct`, `measured_at` | `HorizonOutcome` only has `cost_delta_pct`, `conversions_delta_pct`, `efficiency_delta_pct`, `goal_miss` | Add optional fields; thread into scoring if reward mechanism uses value deltas |
| 3.2 | `validator_only_outcomes.system_estimate` for future baseline | Not parsed; `ScoringMetadata.baseline_type` forced to `predict_zero` in client | Parse `system_estimate`; set `baseline_type` to `system_estimate` when non-null |
| 3.3 | `t28` absent at launch | No `t28` on model (correct) | None — confirm in docs |

---

## 4. HopeDataClient and package contract (§7.5)

| # | Spec | Repo (`hope/validator/data_client.py`) | Action |
|---|------|------------------------------------------|--------|
| 4.1 | Top-level `release: { release_key, phase, epoch, scope_filter, ... }` | `EpochData` does not expose `release` object | Add optional `release: dict` on `EpochData` (or typed model) if validators need scope_filter for logging/commitments |
| 4.2 | `integrity.package_hash` algorithm | `verify_package_hash` uses `sha256(json.dumps(episodes, sort_keys=True))` | **Confirm** with HOPE implementation: canonical JSON (separators, key order, nested structure) must match or verification always fails in production |
| 4.3 | Episodes without `payload` skipped | `continue` when no payload — episodes drop silently from count | Log warning; align with validator expectations |

---

## 5. Scoring vs reconciliation / reward mechanism

| # | Spec | Repo (`hope/scoring/scorer.py`) | Action |
|---|------|----------------------------------|--------|
| 5.1 | TRUST-enriched episodes score higher; **exact multiplier** is a product decision (spec §11 Q1) | Hardcoded `base *= 1.2` for `trust_enriched` | Replace with constant from published reward doc; document source in `constants` or `ScoringWeights` |
| 5.2 | Optional: weight `7d` vs `14d` noise (reconciliation risk section) | `HORIZON_WEIGHTS` by resolution only | Confirm final weights with `SN21_REWARD_MECHANISM.md`; adjust if doc says 7d is directional-only |

---

## 6. Validator runtime (reference implementation)

| # | Spec expectation | Repo | Action |
|---|------------------|------|--------|
| 6.1 | End-to-end fetch package → parse → score | `validator/runner.py`, `epoch_manager.py` present | Ensure live test against real `WR-*-PUB` when HOPE ships v1.9 package |
| 6.2 | Commitment / reveal flow | Implemented in epoch manager + API | Audit against Bittensor synapse flow in `hope/protocol/synapse.py` for mainnet checklist |

---

## 7. Miner SDK

| # | Spec | Repo | Action |
|---|------|------|--------|
| 7.1 | All launch action types including `CAMPAIGN_ENABLE` | `baseline.py` handles `CAMPAIGN_ENABLE`; `LAUNCH_ACTION_TYPES` inconsistent | Fix constants + ensure `BaselineModel` branches match launch enum |
| 7.2 | Magnitude shapes §6.5.1 | Documented in `docs/miner_quickstart.md` | Keep in sync when HOPE finalises magnitude JSON |

---

## 8. Scripts and CLI entry points

| # | Spec / `pyproject.toml` | Repo | Action |
|---|-------------------------|------|--------|
| 8.1 | `hope-verify = scripts.verify_commitment:main` | **`scripts/verify_commitment.py` missing** — console script broken | Implement `verify_commitment.py` or remove entry from `pyproject.toml` |
| 8.2 | Offline scoring | `scripts/score_predictions.py` exists | Keep aligned with `HorizonOutcome` fields once extended |

---

## 9. Tests

| # | Gap | Action |
|---|-----|--------|
| 9.1 | Integration test assumes `verify_package_hash` passes | When HOPE hash algorithm is fixed, update test vectors |
| 9.2 | No contract test for full §6.7 `validator_only_outcomes` shape | Add fixture JSON from HOPE and assert parse |

---

## Summary counts (subnet-only)

| Category | Open items (this list) |
|----------|-------------------------|
| Constants / launch alignment | 3 |
| Protocol / episode | 3 |
| Outcomes | 3 |
| HopeDataClient / integrity | 3 |
| Scoring | 2 |
| Validator / miner | 2 |
| Scripts | 2 |
| Tests | 2 |

**Priority:** **8.1** (broken `hope-verify` entry point) and **4.2** (package hash mismatch risk) are the highest operational risks for validators.

---

## Document history

| Version | Date | Notes |
|---------|------|--------|
| 1.0 | 2026-04-27 | Initial clicklist from spec comparison |
