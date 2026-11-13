# SN21 — Model privacy (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.3 |
| **Audience** | Miners, validators, auditors |
| **Status** | How model images are pulled **today**; released models copied and listed **from 2026-11-16**; closed access while earning still **planned** |
| **Last updated** | 2026-11-09 |
| **Related** | [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_VERIFYING.md](./SN21_VERIFYING.md) · [miner_quickstart.md](./miner_quickstart.md) |

> **From the run of 2026-11-16, released models are copied and listed**
> (rule amendment, published 2026-11-09). The release part of the rule below
> is in force from that run; the closed part is not built yet, so images are
> still pulled anonymously and must stay public. See
> [Released models: copied and listed](#released-models-copied-and-listed-from-2026-11-16).
>
> **Updated 2026-10-19.** Validators do not run models: the subnet's executor
> runs every admitted model and publishes signed receipts, and validators set
> weights from those. So the planned rule no longer gives validators a
> download. While a model earns, only the executor pulls it; once released, it
> is public and the operator keeps a public copy. The release rules are
> unchanged.
>
> **Corrected 2026-10-11.** Earlier versions of this page described
> validator-only downloads of earning models as the rule in force. That is
> **not built**. Today every committed image is pulled anonymously, so it has
> to be public. The validator-only rule below is planned work, in our backlog,
> with no date yet. Nothing about scoring or emissions changes.

---

## How it works today

- The subnet pulls your image **anonymously**, by the digest you committed on
  chain, from a public registry. It never sends credentials.
- So your image **must stay public and pullable** for as long as you want it
  to be run and scored. A private image simply fails to pull; the intake tries
  again later, and a pull failure is never a permanent verdict.
- Anyone can pull a public image. Commit order protects your claim to a model,
  not its bytes: to protect a new model, commit its digest **while the image is
  still private**, then make it public
  ([quickstart, "Protect your model"](./miner_quickstart.md)).
- Copying a public model does not earn twice: the one-payer and lineage rules
  in [SN21_REWARDS.md](./SN21_REWARDS.md) decide who is paid.

---

## Released models: copied and listed (from 2026-11-16)

Rule amendment, published 2026-11-09, in force from the run of **2026-11-16**.

Each day, after weights are set, the run works out which models are
**released**, using the three triggers in
[When a digest is released](#when-a-digest-is-released): 5 consecutive run
days out of the earning set, dethroned as champion, or 45 days after
`closed_since`. Then:

- **A copy is kept.** The released image is copied, by its digest, to
  `ghcr.io/adtaoai/sn21-released`, so it stays pullable even if the original
  image is deleted. A copy that fails is retried the next day; the release
  stands either way.
- **Before release, the copy is private.** The operator also keeps its own
  copy of each model it runs, by digest, in a private repository. It is used
  only to check a removed model is not committed again (SN21_REWARDS.md,
  "Models unchanged for 40 days") and as the source of the release copy, and
  nothing in it is published before the model is released.
- **It is listed publicly** in
  [github.com/adtaoai/sn21_models](https://github.com/adtaoai/sn21_models):
  `released.json` and a table in its README, one row per released digest
  (`digest`, `hotkey`, `released_on`, `reason`, `closed_since` and the copy).
  Only released models are listed.

Counting starts with the run of 2026-11-16; nothing before it counts. Because
closed access is not built yet, `closed_since` is the first day from
2026-11-16 that the digest is in the earning set. A new digest starts its own
clock, and a released digest stays released.

What does not change: scoring, pay, the earning set and how images are pulled.
Your image is already public, so there is nothing to do; keep it pullable by
its digest, as today. The miner obligations and the closed-access sections
below are still planned and not in force.

---

## Planned: closed while earning, public on release (not in force)

Everything from here down is the **target rule**, kept so miners can see where
this is going. **None of it is in force**, except the release triggers, the
release ledger and the operator copy, which apply from the run of 2026-11-16
as described above. When the rest is built, the date it starts will be
announced in advance, with notice, as for any rule change.

## In one line (planned)

While your digest is in the **earning set**, its image bytes stay **closed**.
They are released on the first of: you are out of the earning set for **5
consecutive days**, you are **dethroned** as champion, or **45 days** have
elapsed since the digest first earned under closed access — whichever comes
first. Released digests stay released forever.

---

## What stays public either way

Privacy applies to **image / weight bytes**, not to the scoring audit trail.

| Always public | Closed while earning (then released) |
| :---- | :---- |
| On-chain digest commitment | OCI / Docker image layers and weights |
| Locked predictions, outcomes, score receipts | Registry pull of those bytes |
| Gate results and formula versions | — |

`scripts/verify_day.py` continues to recompute scores from the public receipt.
Full container replay by the public is available **after release**.

---

## Access states

| State | Who may pull the digest-pinned image |
| :---- | :---- |
| **Closed** | Only the subnet's executor, through a read-only pull account you grant on your registry. Validators do not need the image: they verify from the public receipts |
| **Released** | Anyone; the operator also keeps a public copy under the same digest, so release cannot be undone by deleting the miner's repo |

The trust anchor does not move: intake always pulls **by committed digest** and
verifies local `RepoDigests`. Auth only gates *who* may fetch; it never allows
substituted bytes.

---

## When a digest is closed

A digest is **closed** on any day it is in the **earning set** under the
published weight curve ([SN21_REWARDS.md](./SN21_REWARDS.md)) — after all
eligibility filters that feed the curve that day (placement, stake / alpha
hold, liveness eviction, and any published anti-copy collapse).

- **Non-earners** are not required to stay closed under this rule.
- **`closed_since`** for a digest is the **first UTC calendar day** on which
  that digest was both closed and in the earning set. Updating to a new digest
  starts a new clock for the new digest.

---

## When a digest is released

Release is irreversible. The **first** matching trigger wins.

| # | Trigger | Rule |
| -: | :---- | :---- |
| 1 | **Leave earning set** | The hotkey that runs this digest is not in the earning set for **5 consecutive days** → digest **released**. A single day out does not release anything. |
| 2 | **Dethrone** | Under the champion promotion rule ([SN21_REWARDS.md](./SN21_REWARDS.md)), a new champion is seated → the **outgoing champion's digest is released immediately**, even if that hotkey remains an earner (e.g. rank 2). |
| 3 | **Max embargo** | **45 days** after `closed_since` → digest **released**, even if the hotkey is still champion and still earning. |

Worked consequences:

- A long-reigning champion goes public on day **45** of that digest's closed
  earning window; the seat does not extend privacy past the cap.
- A dethroned champion that stays in the top 20 still loses closed access for
  the digest that held the seat — that is the disclosure cost of having run
  live.
- Leaving the earning set releases you even if fewer than 45 days have passed
  — but only after 5 consecutive days out. One bad day does not cost you your
  model. A crashed container, a thin day, or slipping one place and recovering
  leaves your bytes closed.
- **Anti-copy suppression never triggers release.** A day withheld under the
  one-payer rule does not count toward the 5, and a grouping decision cannot
  by itself make anyone's model public. Disclosure is irreversible and
  grouping is a judgement; the two must not be wired together.

Each release is written to a public **release ledger**
(`digest`, `hotkey`, `released_on`, `reason` ∈
`left_earning_set` | `dethroned` | `max_embargo`) and the operator mirror is
updated the same day.

---

## Miner obligations

1. **Bytes must remain available** to the subnet pull path for the whole closed
   period and at the moment of release (so the operator can mirror).
2. **Deleting, rotating credentials, or otherwise withholding** a closed digest
   so the executor cannot pull, or so the release copy fails, is a **miner
   fault**: published strike / forfeiture of future closed access for that
   coldkey, as announced with enforcement. Scores already locked stay facts;
   you do not get to erase the audit trail by deleting the image.
3. Serving different bytes than the committed digest remains impossible under
   digest-pin; a pull that does not verify is a failed intake, not a scored run.

---

## What this rule does not do

- It does **not** hide predictions or scores.
- It does **not** replace anti-copy / one-payer controls on emissions. Closed
  pull slows casual cloning; it does not stop distillation from public
  predictions. Emission uniqueness is a
  separate rule.
- It does **not** require discretionary judgment. Closed vs released is a
  function of earning-set membership, D8 promotion events, and the 45-day
  clock.

---

## Parameters (review-set)

| Parameter | Value |
| :---- | :---- |
| Max embargo | **45 days** from `closed_since` |
| Release on absence | **5 consecutive days** out of the earning set |
| Absence caused by anti-copy suppression | **does not count** toward the 5 |
| Closed population | Digests in the **earning set** that day |
| Dethrone release | **Yes** — outgoing champion digest, even if still earning |
| Re-close after release | **No** |

Numeric parameters follow the same four-weekly review cadence as other daily
stream rules. Changes are announced in advance.

---

## Rollout

The release ledger, the operator's copy and the public list are in force from
the run of 2026-11-16 (above). Closed access is not: the executor's pull
account is built and switched off. Until that part is announced with a start
date, every committed image is pulled anonymously and must stay public
(see "How it works today" above). The announcement will name the first day the
rule applies and the commit prefix (e.g. `sn21-model:v2`) needed for closed
access.

---

## Related

- Container contract: [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)
- Champion vs earner: [SN21_REWARDS.md](./SN21_REWARDS.md)
- Score verification (unchanged): [SN21_VERIFYING.md](./SN21_VERIFYING.md)
