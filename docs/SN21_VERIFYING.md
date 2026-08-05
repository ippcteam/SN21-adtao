# SN21 — Verifying your score

| | |
| :---- | :---- |
| **Audience** | Miners, and anyone who wants to audit the subnet |
| **Status** | Authoritative for the daily stream |
| **Last updated** | 2026-08-05 |

You do not have to trust our scoring. Every day the validator publishes a
**receipt** — the settled outcomes it used, every miner's predictions exactly
as their container produced them, each score's components, and the formula
that ran — and you can recompute your own scores from it and check they match.

Nothing here is privileged. The receipt is public, signed, and the whole feed
is anchored on chain. If our numbers were wrong, this is how you would prove
it, and the output names the exact entry that disagrees.

---

## The short version

```bash
python scripts/verify_day.py --url https://validator.adtao.io --day 2026-08-18
```

`"ok": true` means your scores reproduce. Anything else prints which check
failed and why.

To close the loop all the way to the chain, pass the root you read yourself:

```bash
python scripts/verify_day.py --url https://validator.adtao.io \
    --day 2026-08-18 --expect-anchor <root you read from chain>
```

---

## What gets checked, and what each one proves

| Check | Proves |
|---|---|
| `attestation` | The document is intact and signed by the validator's key — the bytes you received are the bytes it published |
| `chain` | This day links to the previous day's receipt; the feed has no gaps or rewrites |
| `anchor_linkage` | The day's anchored summary names this exact receipt |
| `feed_root` | This day is inside the Merkle root the validator commits **on chain** |
| `score_reproduction` | Every score recomputes from the published outcomes and your published predictions |

Each returns its own PASS/FAIL. A failure tells you *which* link broke, which
matters: a signature failure and a score mismatch mean very different things.

---

## Doing it by hand

**1. Get the receipt.**

```bash
curl https://validator.adtao.io/v1/daily/2026-08-18/receipt > receipt.json
```

It contains `outcomes` (what actually happened), `entries` (per episode,
horizon and miner: the prediction, the four components, the final score) and
`formula` (which version scored it and its weights).

**2. Find your entries.**

```bash
curl "https://validator.adtao.io/v1/daily/2026-08-18/miner/<your-hotkey>"
```

Compare the `prediction` field against what your container actually emitted
that day. This is the "what I submitted is what was scored" check — and it is
worth doing once by hand even if you trust the tooling.

**3. Recompute.** The formula is in
[`hope/scoring/settle_day_flow.py`](../hope/scoring/settle_day_flow.py)
(`score_entry_v2`) and documented in [SN21_SCORING](./SN21_SCORING.md). The
receipt carries the weights that ran, so you apply what was used rather than
what you assume.

**4. Check it is the history the chain committed to.**

```bash
curl https://validator.adtao.io/v1/daily/2026-08-18/proof   # inclusion proof
curl https://validator.adtao.io/v1/daily/root               # current root
```

Read the validator hotkey's commitment from chain yourself and compare it to
`feed_root`. If they differ, this server is serving a different history than
it anchored, and every proof under it is worthless — that is the single most
important thing you can check, and it takes one RPC call.

> **Why a rolling root and not a per-day hash.** The chain stores ONE
> commitment per hotkey and each new one overwrites the last. If we anchored
> each day's own hash, yesterday's anchor would be gone today and old receipts
> would be unverifiable without an archive node. Instead the anchor is a
> Merkle root over **every day published so far**, so the newest commitment
> still covers your day, however old it is, and the proof is a few dozen bytes.

---

## When a check fails

**`score_reproduction` FAIL** — the output includes a `diffs` array naming the
episode, horizon, miner, published score and recomputed score. Post that diff
in the Discord miner channel. It is a small, complete, checkable claim, and we
would rather find out from you than not at all.

**`feed_root` FAIL with `chain_root_matches: false`** — the served root differs
from the chain. Report immediately; this is the serious one.

**`attestation` FAIL** — the document does not match its signature. Re-fetch
first (a truncated download looks identical to tampering), then report.

**`chain` FAIL** — a gap or rewrite in the feed. Report with the day.

**404 with a reason** — normal. `pre_maturity` means nothing has settled yet
(nothing does before ~18 August); `no_scored_results` means that day scored
nothing; `out_of_range` means the feed does not cover that date.

---

## What is not covered

- **The image you ran.** That is the digest commitment: you commit
  `sn21-model:v1:<repo>@sha256:<digest>` on chain, intake pulls by that digest
  and verifies the registry served exactly those bits. Chain-checked, separate
  from the receipt. See [quickstart §5b](./miner_quickstart.md).
- **Censored horizons.** Episodes whose accounts left or stopped spending are
  dropped from scoring, never scored as zeros. They are excluded from
  `outcomes`, and the receipt's `censored` field states how many and why — so
  a short episode count has a published explanation rather than looking like
  data loss. See [SN21_SCORING](./SN21_SCORING.md).
- **Whether a basket was the basket you received.** The cross-check between a
  receipt and its basket manifest is not published yet. Tracked; not claimed.
