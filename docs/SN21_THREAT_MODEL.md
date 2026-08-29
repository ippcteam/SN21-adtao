# SN21 — Threat model and anti-gaming controls

| | |
| :---- | :---- |
| **Version** | 1.0 |
| **Audience** | Miners, validators, auditors |
| **Status** | Living document — controls listed here are the ones that decide payment |
| **Last updated** | 2026-08-07 |

This document states, plainly, how someone could try to earn more than their
model deserves, and what the subnet does about each one. It exists because a
control nobody can read is a control nobody can check.

Two commitments govern everything below:

1. **No parameter that moves money gets a value we did not calibrate
   ourselves.** Thresholds are published, versioned, and changed only on the
   review calendar. We do not adopt numbers proposed by any participant,
   however well argued — including proposals we agreed with and acted on.
2. **Every grouping or penalty is recomputable from published data.** If the
   subnet withholds payment from a hotkey, the evidence for that decision is
   in the day's receipt and anyone can recheck it. An accusation nobody can
   verify is not evidence.

---

## The design principle

    one unit of predictive behaviour
      -> at most one earning seat
      -> paid to one on-chain principal

Models must be public and runnable — validators pull and execute them — so
nobody can promise that a model cannot be read. What the subnet defends is
**emission uniqueness**: copying a model must not multiply what it earns.

---

## 1. Copying and identity

### 1.1 Pull a model, re-tag it, run it on many hotkeys

Every commitment is a public `repo@digest`. Anyone can pull anyone's image.

**Control.** Identical behaviour earns once. Predictions from every model that
runs are compared, and hotkeys producing the same behaviour form one group
paying a single principal. Standings are untouched and the container keeps
running — the exclusion lapses the day a hotkey runs a model of its own.

### 1.2 Copy a model and perturb the output slightly

Grouping used to be exact: a hash of the prediction set. Two identical models
needed only to disagree in the last decimal to be counted as separate payees.
**This was reported by a miner on 2026-08-07 and it was correct.**

**Control.** Grouping is now behavioural, not byte-exact. A pair counts as one
lineage only when *all* of these agree: correlation, sign agreement, scaled
mean distance, and the share of rows that disagree by more than a set amount.
Requiring all four raises the cost of evasion, because noise cheap enough to
break one signal tends to break another or to wreck the score being protected.

### 1.3 Sit just outside a published threshold

A single distance measure has a single boundary, and a boundary is a target.

**Control.** The multi-signal test above has no one number to hug, and the
parameters are set from our own calibration against known copies and known
independent models. We publish the mechanism and the parameter version; we
expect the boundary to be probed, which is why an adversarial test suite must
show "one lineage, one earner" on every probe — exact copies, last-decimal
noise, chains of small steps, and cross-coldkey farms alike.

**In force from 2026-08-29 at parameter version `lineage-v1`.** The version in
force is recorded in every day's allocation audit next to the groups it
produced, so a grouping can always be checked against the calibration that
made it. A parameter change takes a new version and applies forward only.

The behavioural test does not run alone. An exact test on point estimates —
no thresholds, nothing to sit outside — runs beside it, and where the two
disagree the exact one is the one anyone can recompute from the published
receipt without knowing any parameter at all.

### 1.4 Build a chain of near-copies so groups merge or split on demand

**Control.** Group membership requires similarity to the cluster's centre, not
merely to one neighbour, so a ladder of small steps cannot drag unrelated
models into one group or walk copies out of one.

### 1.5 Run many hotkeys from one coldkey

**Control.** One coldkey may hold at most one earning seat. The highest
standing keeps it; ties break on the earlier model commitment. Any cap above
one would simply publish the farm size worth building.

### 1.6 Spread the same model across many coldkeys

Identity separation does not help: lineage is measured on behaviour, which does
not care which coldkey signed it.

### 1.7 Claim seniority with early empty commitments

**Control.** Precedence follows the *model*, not the hotkey, and is taken from
the earliest commitment that actually produced the behaviour in question,
supported by the published record of which hotkey was first observed producing
it. Rebuilding your own image does not reset your seniority. Commit your digest
while the image is still private if you want to protect a new model before it
is public.

---

## 2. Gaming the score without copying anyone

These attacks need no second hotkey and no copying, and none of the controls in
section 1 can see them — a miner doing this is running a genuinely independent
model.

### 2.1 Answer only the episodes you are confident about

A skipped episode produces no ledger entry, so it cannot pull an average down.
Answering only the easiest part of each basket would make a standing the mean
of a best third rather than the mean of the work.

**Control — the participation gate.** Earning requires covering a published
fraction of each day's bundle. Fall below it and weight decays; stay below it
and it reaches zero. Coverage is measured as predictions actually delivered,
not as "the container exited cleanly" — a model that prints nothing usable
exits successfully and must not be paid for it.

Two protections in the other direction, because a liveness rule must never
punish somebody for our failures:

- **A day the subnet did not run is not a miss.** If no bundle ships, nobody
  could have answered it, and that day is dropped from the calculation
  entirely rather than counted against anyone.
- **A day with nothing to predict is not a miss.** Thin days do not punish.

Only *consecutive* shortfalls at the tail matter. A miner who misses once and
then shows up is participating; scoring is where history lives.

### 2.2 Split a basket across two hotkeys to avoid comparison

Behavioural comparison needs rows two miners both answered. Two hotkeys running
one model could answer disjoint halves and never be compared.

**Control.** The coverage floor makes this self-defeating: each half falls below
the floor, so both hotkeys lose weight. No separate detector is needed.

### 2.3 Predict near-zero, or with meaningless intervals

**Control.** A prediction that is both near-zero and narrow-interval is
low-information and is penalised on a graduated ramp. Separately, buying
interval coverage by predicting an enormous range costs more than it earns —
the coverage component carries a convex width penalty.

---

## 3. The admission gate

### 3.1 Submit a model that behaves one way at admission and another once live

**Control.** A digest is admitted on a held-out corpus it has never seen, and
admitted digests remain subject to re-checking against gate episodes.
Divergence between admission behaviour and live behaviour is treated as a
fault, not a curiosity.

### 3.2 Non-deterministic output

A model that answers differently on identical input cannot be independently
reproduced, which breaks the subnet's core published promise that a rerun
reproduces the score.

**Control.** Determinism is checked at admission: the same sample of episodes
is put to the image twice and any disagreement rejects the submission. See
[MINER_MODEL_SPEC.md](./MINER_MODEL_SPEC.md) for the causes that most often
trigger it.

---

## 4. What the sandbox already prevents

Models execute with **no network access**, capped memory, CPU and process
count, a read-only filesystem, and no privilege escalation.

This is load-bearing for everything above. It means a running model cannot
phone home, cannot coordinate with another hotkey mid-run, and cannot look up
the outcome it is being asked to predict. **Any collusion must therefore happen
at build time — which is exactly where behavioural comparison can see it.**

---

## 5. What we do not claim

- **We cannot stop anyone reading a public model.** Validators must pull and
  run these images. What we defend is that copies do not multiply emissions.
- **We cannot stop distillation.** Receipts publish predictions, so a copier
  can train on published outputs without ever pulling an image. This is
  priced in rather than prevented: predictions publish well after the basket
  they answered, so a student always chases a stale target, and a student that
  converges on its teacher's behaviour is grouped with it and does not earn
  twice.
- **Mechanisms and parameters are published; operational posture is not.** You
  can read how every control works and recompute any decision that affected
  you. What we will not supply is a running account of our own configuration —
  probing for weaknesses is a miner's job, and doing that job for anyone is
  not part of the contract.

---

## 6. How to contest a decision

Every payment-affecting grouping is published with the numbers behind it in the
day's receipt: which hotkeys were grouped, the pairwise measurements, which
principal was paid, and the parameter version used. Recompute it. If it is
wrong, you will have the evidence to show it, and that is the point.

Reports of weaknesses in these controls are welcome and have already changed
the subnet more than once. The fastest route is a concrete mechanism and, if
you have it, a measurement.
