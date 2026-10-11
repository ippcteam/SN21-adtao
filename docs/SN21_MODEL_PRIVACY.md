# SN21 — Model privacy (daily stream)

| | |
| :---- | :---- |
| **Version** | 1.1 |
| **Audience** | Miners, validators, auditors |
| **Status** | How model images are pulled **today**, and the **planned** validator-only rule (not built yet) |
| **Last updated** | 2026-10-11 |
| **Related** | [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md) · [SN21_REWARDS.md](./SN21_REWARDS.md) · [SN21_VERIFYING.md](./SN21_VERIFYING.md) · [miner_quickstart.md](./miner_quickstart.md) |

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

## Planned: validator-only downloads (backlog, not built)

Everything from here down is the **target rule**, kept so miners can see where
this is going. **None of it is in force.** When it is built, the date it
starts will be announced in advance, with notice, as for any rule change.

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
| **Closed** | Registered validators that pass the published auth check (hotkey + stake floor), via the subnet pull path |
| **Released** | Anyone; the operator also mirrors the digest so release cannot be undone by deleting the miner's repo |

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
   so validators cannot pull, or so release mirroring fails, is a **miner
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
  predictions or a compromised validator pull. Emission uniqueness is a
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

Not started. Until validator-only pull and the release ledger are built and
announced, every committed image is pulled anonymously and must stay public
(see "How it works today" above). The announcement will name the first day the
rule applies and the commit prefix (e.g. `sn21-model:v2`) needed for closed
access.

---

## Related

- Container contract: [MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md)
- Champion vs earner: [SN21_REWARDS.md](./SN21_REWARDS.md)
- Score verification (unchanged): [SN21_VERIFYING.md](./SN21_VERIFYING.md)
