# SN21 launch — gap checklist (HOPE platform / data repo)

**Purpose:** Handoff list for the **HOPE application** and data pipeline that own governance, episode generation, and the **package API**. This work is **out of scope** for `tao-discovery` (subnet); the subnet only consumes `GET /internal/bittensor/v1/releases/<release_key>/package`.

**Primary references:** `SN21_LAUNCH_DEVELOPER_SPEC.md` (authoritative for implementation), `SN21_LAUNCH_RECONCILIATION_SUMMARY.md` (decisions), plus referenced docs: `data_sharding_and_dataset_control_strategy.md`, `miner_data_delivery_structure.md`, `SN21_EPOCH_STRUCTURE.md`, `SN21_REWARD_MECHANISM.md` (this repo, `docs/`).

**Companion:** Subnet-side gaps: [GAP_CHECKLIST_TAO_DISCOVERY.md](./GAP_CHECKLIST_TAO_DISCOVERY.md).

Use as a clicklist for the HOPE repo; check off when merged to staging/production.

---

## 0. Preconditions (spec §1, §2)

| # | Requirement | Action |
|---|-------------|--------|
| 0.1 | Earliest `archetype_run_results.created_at` known (lower bound for enriched `action_window_start`) | Run audit; document cutoff date |
| 0.2 | Eight governance tables from `data_sharding_and_dataset_control_strategy.md` "Minimal Implementation" | Verify exist and populated; stop if not |
| 0.3 | Do not modify listed frozen tables/behaviour beyond additive migrations (spec §2) | Review every migration for compliance |

---

## 1. Constants (spec §3)

| # | Item | Action |
|---|------|--------|
| 1.1 | Authoritative constants in strategy doc + Python: `LAUNCH_OUTCOME_HORIZONS_DAYS`, `LAUNCH_PRIMARY_HORIZON_DAYS`, `LAUNCH_CONTAMINATION_BUFFER_DAYS`, `LAUNCH_MATURITY_CLOCK_DAYS` (17), `MAX_ACTION_WINDOW_HOURS`, release sizes 150/300/500, `LAUNCH_CAMPAIGN_TYPES`, `LAUNCH_ACTION_SCOPES`, `LAUNCH_ACTION_TYPES` | Edit shared constants; mark launch vs post-launch restore |
| 1.2 | Keep existing `MIN_PERIOD_*` and velocity thresholds unchanged | No change unless separate decision |

---

## 2. Database (spec §4, §8 migration order)

| # | Item | Action |
|---|------|--------|
| 2.1 | New table `bittensor_episode_candidates` (full column list, indexes, constraints) | Alembic #1 |
| 2.2 | `bittensor_release_registry` columns: `phase_number`, `epoch_number`, `scope_filter`, `outcome_horizons_days`, `schema_version` | Alembic #2; backfill `WR-2026-W16-PUB` as v0.1 governance-only |
| 2.3 | `bittensor_episode_registry` columns: `episode_candidate_id`, `coverage_status`, `schema_version`, `payload_built_at`, `payload_size_bytes` | Alembic #2 |
| 2.4 | Reference table `changelog_event_classification_rules` + seed from §5.2 | Alembic #3 |
| 2.5 | `bittensor_episode_outcomes` (or extension of registry) for measured deltas | Alembic #4 |

---

## 3. Services (spec §5, §7)

| # | Service | Path (spec) | Key behaviours |
|---|---------|-------------|----------------|
| 3.1 | `EpisodeClassificationService` | `app/services/bittensor/episode_classification_service.py` | `classify_account_period`; 72h clustering; campaign type filter SEARCH; idempotency; writes candidates; acceptance tests §5.7 |
| 3.2 | `EpisodePayloadBuilderService` | `app/services/bittensor/episode_payload_builder_service.py` | `build_payload(episode_candidate_id, schema_version='v1.9')`; joins per §7.2; cache key; target ≤15 KB compressed |
| 3.3 | `ReleaseBuilderService` (extend) | existing | `build_release(..., phase, epoch, scope_filter, target_episode_count)`; query candidates with filters; `SKIP LOCKED`; stratify TRUST/baseline; public bucket only for launch |
| 3.4 | `ValidatorOutcomeMeasurementService` | `app/services/bittensor/validator_outcome_measurement_service.py` | `measure_outcomes(episode_candidate_id)`; t7/t14 from `campaign_daily_performance`; no t28 at launch |
| 3.5 | `ContaminationGuardService` | existing | `assert_no_hidden_in_public_release` must pass for public training release |

---

## 4. Package API (spec §7.5)

| # | Item | Action |
|---|------|--------|
| 4.1 | `GET /internal/bittensor/v1/releases/<release_key>/package` returns full v1.9 structure: `schema_version`, `release`, `episodes[]` with `episode_id`, `payload`, `validator_only_outcomes` | Replace metadata-only response |
| 4.2 | `integrity`: `package_hash`, `episode_count`, `trust_enriched_count`, `baseline_count` | Implement; **document exact hash input** (canonicalisation) for subnet `HopeDataClient.verify_package_hash` |
| 4.3 | `validator_only_outcomes.outcomes.t7` / `t14` null until measured | Per spec |
| 4.4 | `X-API-Key` auth unchanged | — |
| 4.5 | Rollback: old response behind feature flag (spec §8 step 9, §13) | Feature flag in config |

---

## 5. Classification rules (spec §5.2)

| # | Item | Action |
|---|------|--------|
| 5.1 | Whitelist rows map changelog → `BUDGET_CHANGE`, `CAMPAIGN_PAUSE`, `CAMPAIGN_ENABLE`, `BID_STRATEGY_CHANGE` at campaign scope; exclude PMax/Shopping/etc. | Seed table + service logic |
| 5.2 | Multi-type cluster → highest blast-radius order: `BID_STRATEGY_CHANGE > BUDGET_CHANGE > CAMPAIGN_PAUSE > CAMPAIGN_ENABLE` | Implement |
| 5.3 | Cross-campaign cluster → split per campaign | Implement |
| 5.4 | `coverage_status` + 7-day archetype recency (§5.5) | Implement |

---

## 6. Deployment sequence (spec §8)

| # | Step | |
|---|------|--|
| 6.1 | Migrations 1–4 | |
| 6.2 | Deploy classification; backfill candidates | |
| 6.3 | Deploy payload builder; 20-sample manual review | |
| 6.4 | Deploy outcome measurement; 20-sample review | |
| 6.5 | Deploy release builder extension | |
| 6.6 | Package endpoint with flag | |
| 6.7 | Dry-run `WR-2026-W18-PUB` (300 target), do not distribute | |
| 6.8 | Live: mark distributed on launch date | |

---

## 7. Launch acceptance tests (spec §10)

| # | Test | |
|---|------|--|
| 7.1 | Classification: ≥500 candidates total; ≥200 with `matures_at <= launch` | |
| 7.2 | Launch filter: ≥150 rows SEARCH + CAMPAIGN + eligible | |
| 7.3 | TRUST: ≥10% of pool enriched (comm issue if below, not always blocker) | |
| 7.4 | 50 random launch-scope payloads: 100% JSON schema valid | |
| 7.5 | P95 compressed payload ≤ 20 KB | |
| 7.6 | 20 mature candidates: t7 and t14 non-null | |
| 7.7 | Two concurrent release builders, 100 each: zero overlap | |
| 7.8 | Public release rejects hidden bucket accounts | |
| 7.9 | HopeDataClient-style fetch: parseable, `schema_version == v1.9`, field completeness | |
| 7.10 | Contamination guard passes for release key | |

---

## 8. Open product decisions (spec §11 — do not “guess” in code)

| # | Topic |
|---|--------|
| 8.1 | Scoring weight multiplier `BASELINE` vs `TRUST_ENRICHED` (sync with `SN21_REWARD_MECHANISM.md` and subnet) |
| 8.2 | Pre-window 60 vs 90 days |
| 8.3 | `CAMPAIGN_ENABLE` in or out of launch |
| 8.4 | tCPA/tROAS as separate type vs `BID_STRATEGY_CHANGE` |
| 8.5 | Cap at 500 episodes vs expand / roll week |

---

## 9. Post-launch (spec §12) and rollback (spec §13)

- Restore buffer 7d, optional 28d horizon, entity sections in later phases — track as separate epics.  
- Rollback steps: feature flag to governance-only package; close release; reservations reversible; comms per §13.

---

## Handoff note

**Subnet repo** will align `HopeDataClient`, Pydantic models, and hash verification **once** this document’s §4.2 (package hash definition) and §4.1 (response shape) are fixed in production. Coordinate a single test release key for joint validation.

---

## Document history

| Version | Date | Notes |
|---------|------|--------|
| 1.0 | 2026-04-27 | Initial clicklist from developer spec |
