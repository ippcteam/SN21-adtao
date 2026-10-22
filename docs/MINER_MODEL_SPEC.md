# SN21 — Miner model specification (container contract)

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners |
| **Status** | Authoritative for the daily stream |
| **Last updated** | 2026-08-20 |
| **Related** | [miner_quickstart.md](./miner_quickstart.md) · [SN21_SCORING.md](./SN21_SCORING.md) · [SN21_TRAINING.md](./SN21_TRAINING.md) · [SN21_MODEL_PRIVACY.md](./SN21_MODEL_PRIVACY.md) |

This is the contract between your model and the subnet: what you ship, how it
is executed, and what it must emit. Scoring is in
[SN21_SCORING.md](./SN21_SCORING.md); emissions are in
[SN21_REWARDS.md](./SN21_REWARDS.md). How your image is pulled today (it
must be public) and the planned closed-while-earning rule are in
[SN21_MODEL_PRIVACY.md](./SN21_MODEL_PRIVACY.md).

---

## 1. What you submit

A container image (OCI / Docker). You commit its **digest** on chain:

```
sn21-model:v1:<repo>@sha256:<64hex>
```

The digest pins the exact bits the subnet will run. Updating your model means
pushing a new image, committing the new digest, and re-entering the admission
gate.

## 2. Execution contract

Your entrypoint reads **one episode payload per line on stdin** and writes
**one prediction per line on stdout**:

```json
{"episode_id": "...", "horizons": {"7": {...}, "14": {...}, "28": {...}}}
```

A line counts as a prediction for an episode only when `horizons` carries a
non-empty block for **every** horizon listed in that episode's
`episode_metadata.outcome_horizons_days`. A missing, empty or null block makes
the line an abstention for that episode: it produces no scored entry, and the
episode is uncovered under the absence rule. Extra horizons are ignored.

From the basket of 2026-10-19 (run on 20 October) every episode lists
`[7, 14, 28]` (rewards amendment of 2026-10-13): return all three. Before that date episodes list
`[7, 14]`, and returning 28 as well is harmless.

Each horizon carries monotone `p10` / `p50` / `p90` for `cost_delta_pct`,
`conversions_delta_pct` and `efficiency_delta_pct`, plus `goal_miss_probability`
and `instability_risk` in `[0, 1]`.

> `goal_miss_probability` and `instability_risk` are **accepted but not
> scored** — no ground truth exists for them. Spend your model's capacity on
> the delta metrics.

**Runtime limits:**

| Limit | Value |
| :---- | :---- |
| Network | **None** — the sandbox runs `--network=none`; everything you need ships in the image |
| Memory | **1 GB** |
| CPU time | **15 minutes** per daily basket (~250 episodes) |
| Filesystem | Read-only root |

Exceeding a limit aborts that day's run: no scores for the day.

> **Amended 2026-08-24 — the absence penalty.** This document previously
> said a missed day "costs you evidence rather than incurring a separate
> penalty." That is no longer true, and here is why, openly: under that
> rule a miner absent on hard days kept a spotless average built on easy
> days and held first place while every participant's score moved. From
> the published effective date, every episode of a subnet-run day you do
> not return a scoreable prediction for enters your **standing** as a
> zero (floor **0.00**) carried at the **full episode weight** — the same
> standing mass a covered episode contributes — so covering an episode at
> any honest score always leaves a standing at least as high as dropping
> it. (Corrected same-day from an initially published 0.30 floor, before
> any charge was ever applied; see SN21_REWARDS for the open correction.)
> There is no coverage threshold to duck under and no exit: you are
> charged for every uncovered day as long as you hold a board position,
> so going quiet bleeds your standing until you return or your entries
> age out of the 35-day window. Days the **subnet** fails to run charge
> nobody. Every applied penalty is published beside the receipts
> (`/v1/daily/absence-penalties`), so your standing stays reproducible
> from public documents, penalties included. Full details in
> [SN21_REWARDS.md](./SN21_REWARDS.md).

### Determinism is required, and it is checked

Your container must give the **same answer to the same question every time**.
This is not a recommendation: at admission the same sample of episodes is put
to your image twice, and **any disagreement rejects the submission** with the
reason `nondeterministic`.

The rule exists because the subnet publishes that anyone can replay a scored
day and reproduce the result. A model that answers differently on a replay
makes that promise false for every day it ran, and makes two validators
disagree about what you predicted.

**This is the rejection most likely to surprise you**, because a
non-deterministic model usually passes local testing. The common causes:

- Python hash randomisation changing set or dict iteration order
- iterating a `set` where order reaches the output
- an unseeded random number generator
- timestamps, elapsed time, or anything derived from the clock
- floating-point reduction order that varies with thread count — this one
  passes every single-threaded test and fails on a different machine
- GPU non-determinism from atomics or auto-tuned kernel selection

Check before you submit. Run your image three times over the same basket and
compare the output byte for byte:

```bash
for i in 1 2 3; do
  docker run --rm -i --network=none --memory=1g --read-only \
    <your-image> < basket.jsonl > run$i.jsonl
done
cmp run1.jsonl run2.jsonl && cmp run2.jsonl run3.jsonl && echo "deterministic"
```

Abstaining is always allowed — declining an episode is not the same as
contradicting yourself, and a re-run that fails to start is treated as a
liveness matter, not as non-determinism.

### Schema versions

Several surfaces, one schema:

| Surface | Shape |
| :---- | :---- |
| Historical exports under `data/episodes/` | Weekly-era export — episode fields at the top level |
| Weekly-era training bundle | `{"episode_id", "input": {...}, "labels": {...}}` |
| **Rich training bundle v2** | `{"episode_id", "input": {"payload": {...}, "transition_key"}, "labels": {...}}` — first line is a `_manifest` |
| **Live daily contract** | **Episode payload v2.0** — id, metadata, account state, pre-window series, action bundle with magnitudes |

Field names and meanings are identical where they overlap. Training code
should read `input.payload` when present, else `input`, else the top level.
The reference model's flattened field list is a convenience view of the same
schema, not a third format. The v2 bundle and its change types are in
[SN21_TRAINING.md](./SN21_TRAINING.md).

New per-action fields in v2 payloads, worth modelling: `from_value` /
`to_value` on budget and target changes (the change's actual size),
`client_type` (Google's record of who made the change), and a
`source_mix` summary on the bundle (user = web/mobile interface,
system = scripts, API, rules). `transition_key` is low-cardinality by
design — families like `BUDGET:up_large`, `NEGATIVE_KEYWORD_ADD`, and
`COMPOSITE:<dominant>+<n>` for mixed 72-hour windows, where the
prediction is the net effect of the listed actions together.

### Shared negative keyword lists (rule amendment, published 2026-10-23, effective 2026-10-30)

A shared negative keyword list is one list attached to several campaigns,
often by a manager account. From the run of 2026-10-30, keywords added to or removed
from a shared list become episodes too, one for each campaign the list is
attached to:

| `transition_key` | Meaning |
| :---- | :---- |
| `SHARED_NEGATIVE_KEYWORD_ADD:<match>:<count>` | keywords added to a shared list; `<match>` is `exact`, `phrase`, `broad`, `mixed` or `unknown`, `<count>` is `1`, `2-10` or `11+` (the same shape as `NEGATIVE_KEYWORD_ADD`) |
| `SHARED_NEGATIVE_KEYWORD_REMOVE` | keywords removed from a shared list |

Each such action carries a `list_change` block:

```json
"list_change": {
  "op": "ADD | REMOVE",
  "list_id_hash": "…",
  "list_owner_level": "account | manager",
  "keyword_count": 12,
  "match_types": {"EXACT": 10, "PHRASE": 2},
  "window_start": "2026-10-22T05:10:04+00:00",
  "time_source": "…"
}
```

- **No keyword text**, as with every other change type: the count and the
  match-type mix only. The list id is hashed like every other id.
- **When it happened.** Google does not timestamp changes inside a shared
  list. We check every list's keyword count each morning and read only the
  lists whose count changed, so a change is dated **between the previous
  check and this one** (`window_start` to the episode's window end). A list
  edited twice in one day shows as one change.
- **Who made it.** Google records no actor for these edits, so the action's
  source is `unknown` (never `user` or `system`).
- **Scoring** is unchanged: the same horizons, metrics and settle days as
  every other change. These episodes are not in the published training data
  yet; treat them like `NEGATIVE_KEYWORD_ADD` / `REMOVE` until they are.

### Change descriptions and account context (rule amendment, published 2026-10-23, effective 2026-10-30)

Until now an episode said a keyword was added to a campaign, and how big the
campaign is, but not what kind of keyword it was, how it sits among what the
account already had, or what an ad edit changed. From the run of
**2026-10-30**, every episode also describes the change and the ad group and
account around it. The v5 training slice carries the same fields, built the
same way.

| Field | What it says |
| :---- | :---- |
| `action_bundle.actions[].descriptor` | `kind` (`criterion`, `ad`, `other`). For criteria: `criterion_kind`, `level` (`ad_group`, `campaign`, `shared_list`, `unknown`), `negative`, `match_type`, `status_from` / `status_to`, `match_type_changed`, `word_count_band` (`1`, `2`, `3`, `4-5`, `6+`), `brand_context` (`own_brand`, `competitor`, `generic`, `unknown`), `topic` (industry path in the account's own tree, general themes such as location, urgency, price). Keywords add `relation`: duplicate in the ad group, same words in a wider or narrower match, the campaign's keyword count band, and whether a campaign or ad-group negative already blocks it. Negatives add `footprint`: the share of the campaign's impressions, clicks, cost and conversions the negative would have blocked, and whether it conflicts with keywords (and their cost share). Ad edits: headline and description counts before and after, added and removed, pins, final URL and path changes, the ad's impression share in its ad group before the change, and how many ads in the ad group were serving. Budget, bid, campaign-setting and asset changes carry `{"kind": "other"}` only. |
| `action_bundle.bundle_summary.change_descriptor` | `actions`, `described_share`, `by_match_type`, `by_brand_context` |
| `action_bundle.change_context.coverage` | `full`, `no_ad_group_data`, `no_campaign` or `error`; with `touched_ad_groups_band` and `campaign_ad_groups_band` |
| `change_context.touched_ad_group` | The ad group that changed: status; its share of the campaign's cost, conversions and impressions; eight weekly points of cost, clicks and conversions as an index around their own average (capped at 5); keyword count band; match-type and topic mix; up to 100 keyword rows (`id` code, match type, topic, cost share, conversion rate against the ad group, quality-score bucket `low` / `mid` / `high` / `unknown`), the rest summed in `keywords_other` |
| `change_context.siblings` | Up to 50 other ad groups in the campaign (cost share, conversion rate against the campaign, keyword count band, main topic), the rest in `siblings_other` |
| `change_context.account` | Up to 50 campaigns in the account (`id` code, whether it is the one that changed, campaign type, cost and conversion share, ad-group and keyword count bands, main topic, eight-week cost index), the rest in `campaigns_other`; plus the account's topic mix |
| `change_context.history` | `version` `h2`. The changed campaign's changes in the 30 and 90 days before, by family (`budget`, `campaign_setting`, `ad_group`, `keyword_or_ad_group_target`, `campaign_target`, `shared_list`, `account_negative`, `ad`, `asset`, `conversion`, `other`), with days since the last one (`0-1`, `2-7`, `8-30`, `31-90`, `none_in_90d`). A budget change counts under its campaign when that budget serves only that campaign; a shared budget's changes are not assigned to a campaign. The account's changes in 30 and 90 days, and who made them as shares (`people_web`, `editor`, `api`, `script`, `rule`, `adtao`, `unattributed`). Changes elsewhere in the account in the week before and during the window (`concurrent`). `prior_outcomes`: up to 3 of the account's results that had settled before the window opened, most recent first, each with its change type, horizon and the measured cost, conversions and efficiency changes as bands (`<-50%`, `-50..-20%`, `-20..-5%`, `-5..+5%`, `+5..+20%`, `+20..+50%`, `+50%+`, `unknown`). Upload jobs and IP exclusions are not counted. |
| `change_context.seasonality` | `version` `s1`. The changed campaign's last 12 whole months of cost and conversions as an index around their own average (point 0 is the same month last year; a month before we hold the account's data is `null`, not zero), the same and next month last year, campaign age (`new`, `<6m`, `6-12m`, `12-24m`, `24m+`, `unknown`), month and week of year. Built from Search keyword data only (`coverage` says `no_keyword_data` otherwise). |
| `change_context.cohort` | `version` `c1`. Where the account sits among active accounts in its industry (at least 100 clicks over the three whole months before): CTR, conversion rate and CPA as deciles (`p0-10` … `p90-100`), with `cohort_depth` and `cohort_size` (`20-50`, `51-200`, `200+`). A cohort needs at least 20 accounts; CPA is ranked only against accounts in the same currency, and only when at least 20 share it. Never the industry's name. |
| `change_context.geo` | `version` `g1`. The changed campaign's area type (`major_metro`, `metro`, `suburban`, `small_town`, `rural`, `mixed`, `unknown`), the urbanity mix as shares, and how much results depend on location (`none` … `high`) with a confidence. If any campaign-level targeting change happened since the window, the location may have changed, and `coverage` says `changed_since` instead. |

**What these fields never contain.** No keyword, negative, headline or URL
text. No names (brand, competitor, list, account). No money amounts: shares
and counts are bands (descriptor and neighbourhood shares `0`, `<1%`, `1-5%`,
`5-20%`, `20-50%`, `50%+`; history source shares and urbanity mix `0`,
`<10%`, `10-50%`, `50-90%`, `90%+`), and trends are an index around the
item's own average. An industry or topic path appears only if at least 10
accounts share it; otherwise it is cut back to a broader path that 10 accounts
share, or left out.

**"Unknown" is not "no".** A field that cannot be read says `unknown` (or is
`null`) instead of a value that looks like an answer. "Already blocked by a
negative" is either `true` or `unknown`: negatives removed since, and shared
negative lists, cannot be seen for past days.

**Nothing from after the change.** Every field is built from data dated
before the change window: daily rows before the window, months that ended
before it, and results that had settled before it opened. The area type is
today's, and ships only where the campaign's location targeting cannot have
changed since the window.

**Training slices.** Before a slice is published, we count inside it how
many accounts share each combination of industry, currency, spend band and
area type. Where the area type would narrow a group to fewer than 10
accounts, it is withheld in that slice and `geo.coverage` says
`withheld_k_anonymity`.

**Industry path one level up.** From the same run,
`account_profile.vertical.anchor_industry_path` and `lineage` stop at the
immediate parent of the account's industry (the last level is dropped), and
`hierarchy_id` is no longer sent. v5 is built the same way.

**What does not change.** Scoring, horizons, settle days, the admission gate
and every published limit stay as they are. A model that ignores the new
fields keeps working; models must ignore fields they do not use.

### `account_state.goal_basis` — which efficiency metric you are scored on

Each episode is scored on the account's own optimization goal, resolved and
**frozen at reveal** so it cannot move after you predict. `account_state`
carries it:

```json
"goal_basis": {
  "basis": "cpa | conversion_value",
  "source": "configured | taxonomy | default",
  "guarded": false,
  "frozen_at": "2026-08-22T14:05:13.896676+00:00"
}
```

- **basis** — which measured column supplies the efficiency delta the scorer
  compares your prediction against: `cpa` (cost-per-acquisition) or
  `conversion_value` (a ROAS / value goal).
- **source** — which rung resolved the basis: `configured` (the account has
  an explicit goal metric, e.g. Target CPA or Target ROAS — this wins
  outright), `taxonomy` (no explicit goal, but the account's vertical is a
  value vertical → `conversion_value`), or `default` (neither → `cpa`).
- **guarded** — `true` means the account *intended* `conversion_value` but had
  no conversion-value baseline in the pre-window, so the basis was vetoed down
  to `cpa`; `source` still records the intended rung.

The basis is **per episode, not a fixed account attribute**: if an advertiser
changes a campaign's optimization goal, later episodes flip basis. Model on
the resolved `goal_basis` in each payload, not on a per-campaign assumption.

Published daily receipts also record the resolved basis per outcome under the
field name `efficiency_basis`. Two basis maps are published, both keyed by
`episode_id` (= `candidate_key[:16]`, the same id the bundle and receipts use)
and both carrying `{episode_id, efficiency_basis, source, guarded}`:

| Map | Route | Covers |
| :---- | :---- | :---- |
| Daily | `/v1/daily/episode-basis-map` | Episodes that have appeared in a settled **daily receipt**. |
| Training | `/v1/daily/training-basis-map` | Every episode in the published **rich training-v2** corpus. |

The two populations do not overlap: the daily map is recent settled receipts,
the training map is the reconstructed historical windows in the bundle. If you
train on the bundle, use the **training** map. Fetch either from the same host
as the receipts, e.g.
`https://hope-bittensor-api.onrender.com/v1/daily/training-basis-map`. From the
**next** training-bundle refresh, the resolved basis also ships inline on every
record as `account_state.goal_basis`, so no join is needed.

## 3. Admission — the backtest gate

A newly committed digest runs against a **held-out historical corpus** of
episodes with settled outcomes. Admission requires **beating the published
naive baseline** (persistence: zero-change medians with corpus-calibrated
spreads) on the gate metric — quantile pinball blended with direction
accuracy, 70/30 — and reaching at least 90% of the reference model's
coverage. Every model update re-runs the gate, and gate results are published.
The corpus is not published; each published verdict names the corpus it was
judged on (its key and the sha256 of the document), so two verdicts can be
compared only when they name the same corpus.

## 4. The daily cycle once admitted

**Wall clock:** a **midnight EST** cut-off each day is the day boundary for
the basket and prediction-lock cycle.

| Day | What happens |
| :---- | :---- |
| Day 0 | Account changes occur; they become that day's basket |
| Day 1 | The subnet executes your container against the basket in the sandbox; its output is locked as your predictions |
| Day 15 / 22 / 36 | Each (episode, horizon) is scored exactly once at 7 / 14 / 28 days plus a 7-day settling window |

Scores fold into your episode-age-weighted standing, weights follow the
published curve, and the champion seat changes only under the promotion rule
in [SN21_REWARDS.md](./SN21_REWARDS.md).

Every scored day is published as a signed receipt you can recompute — see
[SN21_VERIFYING.md](./SN21_VERIFYING.md).

## 5. Liveness and chronic failure

A crash, timeout or budget breach means no scores that day. Sustained failure
is bounded by a published policy:

| Rule | Value |
| :---- | :---- |
| Strike | A day your container was run and delivered nothing usable, where the fault is yours |
| Eviction | **5 strikes within a rolling 14 days** |
| Return | After **7 days**, on a clean run |

Three things are explicitly **not** strikes:

- A weak, wrong or missing **prediction**. Accuracy is priced in your
  standing; liveness is a separate axis and stays one.
- A day the **subnet did not run**, or ran an empty basket.
- A failure on the **operator's** side. Those days count neither against you
  nor for you.

The policy is published ahead of enforcement, and any change to how it
affects weights is announced through the miner channels before it takes
effect.
