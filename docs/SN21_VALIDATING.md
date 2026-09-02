# Validating on SN21

How to run a validator on subnet 21: what to set up, where the daily
weight vector comes from, and — most importantly — the four behaviours
that make a correctly working validator look broken.

## What an SN21 validator does

SN21 scores miners' predictions against real advertising outcomes. The
scoring runs in the operator's pipeline and publishes a daily weight
vector; a validator's job is to read that vector, verify it if it wants
to (every input is published, see [SN21_VERIFYING.md](./SN21_VERIFYING.md)),
and set it on chain. Consensus then does what consensus does.

## Setup, in order

1. **Register your hotkey on netuid 21** and stake it. Weight-setting
   only counts toward consensus while your hotkey holds a **validator
   permit** — the permit set is the top validators by stake, so a
   registered hotkey without enough stake can submit weights all day and
   change nothing. Check your permit before debugging anything else:
   `btcli` shows it per-uid, as does the metagraph.

2. **Request an API key** for the daily weight feed by messaging the
   operator team (the same channel the miners use). Keys are issued per
   team, read-only, and rotated on request or on exposure.

3. **Read the vector.** One GET with the key in the header:

   ```
   curl -H "X-API-Key: <your key>" \
     "https://hope-ads-backend.onrender.com/internal/bittensor/v1/daily/weights"
   ```

   The response is `hotkey -> weight`, the same vector the operator's own
   validator commits. Add `?day=YYYY-MM-DD` for a specific day; without
   it you get the latest published vector. A new vector normally
   publishes after the daily pipeline settles (late morning UTC).

4. **Set the weights** from your validator loop: map hotkeys to UIDs
   through the metagraph, normalise, and call `set_weights` with the
   subnet's current `version_key` (the `weights_version` hyperparameter).
   A complete reference loop ships in this repository —
   `scripts/run_partner_validator.py` — configured entirely through
   environment variables; run it as-is or use it as the starting point
   for your own.

## The four things that look broken but are not

**Commit-reveal.** Netuid 21 runs commit-reveal weights. A successful
`set_weights` is committed encrypted and reveals roughly one tempo
(about 72 minutes) later. If you read the chain right after setting, you
will see the PREVIOUS vector — that is the mechanism working, not your
call failing. Judge success by the extrinsic result, then check the
chain again after the reveal window.

**The rate limit.** The subnet enforces a minimum number of blocks
between two weight-settings from the same hotkey. A set attempted too
soon returns unsuccessful with no other error. Wait and retry; a loop
that ticks every few minutes and tolerates this result is correct.

**The version key.** A `version_key` that does not match the subnet's
current `weights_version` is rejected quietly. If your sets never land
and nothing else explains it, compare your version key against the
hyperparameter first.

**The permit.** Weights from a hotkey without a validator permit are
accepted by the chain and ignored by consensus. If your extrinsics
succeed, the reveal shows your vector, and consensus still does not
move, check the permit — it is stake-ranked and can be lost when
stake shifts.

## What you should see when it works

Your loop fetches the vector without errors; `set_weights` returns
success (or the rate-limit result, which your loop absorbs); one tempo
later the chain shows your vector; and your hotkey's validator trust is
non-zero in the metagraph. If any one of those four is missing, the
section above says where to look.

Everything the vector is computed from is public: per-day receipts,
accuracy feeds, penalties and proofs at
`https://hope-bittensor-api.onrender.com/v1/daily/...`, reproducible
with `scripts/verify_day.py` from this repository.
