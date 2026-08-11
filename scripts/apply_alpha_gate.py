"""Apply the published alpha hold to the validator's live weight vector.

    python3 -m scripts.apply_alpha_gate [--dry-run] [--floor 150]

WHY THIS EXISTS AS A ONE-SHOT

    SN21_TRANSITION_PLAN says that from 10 August carried-over weights are
    paid only to miners meeting the hold, and "Fail the hold -> not paid".
    Nothing enforced it, so the live vector pays miners below the floor.

    This reads the vector the validator ALREADY published on chain, drops the
    hotkeys below the floor, redistributes their share among those who
    qualified, and re-commits. It deliberately does NOT re-score anything: a
    re-run of the scorer is how the wrong vector got published on 10 August,
    and the standings are not in dispute here — only who is eligible to be
    paid from them.

SAFETY

    Every check that could prevent a bad commit runs BEFORE the commit, and
    any failure aborts without touching the chain:
      - the gate must report applied=True (it refuses to empty a vector)
      - the burn destination must come through untouched
      - the vector must still sum to ~1.0
      - at least MIN_EARNERS miners must survive

    The wallet is materialised from the environment, the same way the daemon
    does at boot, because the keys are not on disk.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.collateral_floor import active_floor  # noqa: E402
from hope.scoring.collateral_gate import (  # noqa: E402
    apply_hold,
    metagraph_alpha_reader,
)

# Below this, something is wrong with our read rather than with the miners.
MIN_EARNERS = 10

# How far the post-gate vector may drift from the pre-gate total.
SUM_TOLERANCE = 1e-6


def materialise_wallet():
    """Write the wallet files from the environment, then open it.

    The service holds coldkeypub/hotkey as base64 in env and writes them at
    boot; a process that replaces the boot script therefore starts with no
    keys on disk. Recreating them here keeps this script runnable as a
    standalone start command.
    """
    import bittensor as bt

    name = os.environ.get("WALLET_NAME", "")
    hotkey = os.environ.get("HOTKEY_NAME", "default")
    if not name:
        raise SystemExit("WALLET_NAME is not set — refusing to guess a wallet")

    base = os.path.expanduser(f"~/.bittensor/wallets/{name}")
    os.makedirs(os.path.join(base, "hotkeys"), exist_ok=True)

    pairs = [
        ("COLDKEYPUB_B64", os.path.join(base, "coldkeypub.txt")),
        ("HOTKEY_B64", os.path.join(base, "hotkeys", hotkey)),
    ]
    for env_key, path in pairs:
        blob = os.environ.get(env_key, "")
        if not blob:
            raise SystemExit(f"{env_key} is not set — cannot materialise the wallet")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(blob))
            os.chmod(path, 0o600)
    # `bt.Wallet` on current SDKs, `bt.wallet` on older ones. Resolve rather
    # than pin: this script runs on whatever the validator host has installed.
    wallet_cls = getattr(bt, "Wallet", None) or getattr(bt, "wallet", None)
    if wallet_cls is None:
        raise SystemExit("bittensor exposes neither Wallet nor wallet")
    wallet = wallet_cls(name=name, hotkey=hotkey)
    # Touch the hotkey so a malformed blob fails here, loudly, and not
    # halfway through a commit.
    _ = wallet.hotkey.ss58_address
    return wallet


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--floor", type=float, default=None,
                   help="override the ladder floor for this run")
    p.add_argument("--netuid", type=int,
                   default=int(os.environ.get("NETUID", "21")))
    p.add_argument("--network", default=os.environ.get("BT_NETWORK", "finney"))
    p.add_argument("--burn-uid", type=int,
                   default=int(os.environ.get("SN21_OVERRIDE_WEIGHT_UID", "135")))
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    from datetime import date, timezone

    import bittensor as bt

    print("===ALPHA-GATE-START===", flush=True)
    wallet = materialise_wallet()
    print(f"[gate] wallet {wallet.hotkey.ss58_address}", flush=True)

    subtensor = bt.Subtensor(network=args.network)
    metagraph = subtensor.metagraph(netuid=args.netuid)

    try:
        uid = list(metagraph.hotkeys).index(wallet.hotkey.ss58_address)
    except ValueError:
        raise SystemExit("this hotkey is not registered on the subnet")
    print(f"[gate] validator uid {uid}", flush=True)

    on_chain = dict(subtensor.weights(netuid=args.netuid)).get(uid) or []
    if not on_chain:
        raise SystemExit("no prior weights on chain — nothing to gate")
    total_u16 = sum(w for _, w in on_chain) or 1
    vector = {int(u): w / total_u16 for u, w in on_chain if w > 0}

    day = date.today() if not hasattr(date, "today_utc") else date.today()
    floor = (args.floor if args.floor is not None
             else active_floor(day, os.environ, None))
    burn_before = vector.get(args.burn_uid, 0.0)
    miners_before = len([k for k, v in vector.items()
                         if v > 0 and k != args.burn_uid])
    print(f"[gate] floor {floor:.0f} alpha · miners {miners_before} · "
          f"burn {burn_before:.1%}", flush=True)

    result = apply_hold(vector, floor, metagraph_alpha_reader(metagraph),
                        protected={args.burn_uid}, force=True)

    if not result.applied:
        raise SystemExit(f"[gate] REFUSED: {result.refused_reason}")

    gated = result.weights
    burn_after = gated.get(args.burn_uid, 0.0)
    survivors = [k for k, v in gated.items() if v > 0 and k != args.burn_uid]

    print(f"[gate] excluded {len(result.excluded)} · kept-unreadable "
          f"{len(result.unreadable)} · survivors {len(survivors)}", flush=True)
    for key, reason in sorted(result.excluded.items(),
                              key=lambda kv: -vector[kv[0]])[:40]:
        print(f"[gate]   drop uid {key}: had {vector[key]:.3%} · {reason}",
              flush=True)

    # ---- pre-commit safety, all before anything is signed ----
    if abs(sum(gated.values()) - sum(vector.values())) > SUM_TOLERANCE:
        raise SystemExit("[gate] ABORT: gated vector does not preserve the total")
    if abs(burn_after - burn_before) > SUM_TOLERANCE:
        raise SystemExit(f"[gate] ABORT: burn moved {burn_before:.4%} -> "
                         f"{burn_after:.4%}")
    if len(survivors) < MIN_EARNERS:
        raise SystemExit(f"[gate] ABORT: only {len(survivors)} survivors "
                         f"(min {MIN_EARNERS}) — suspect a bad read")
    print(f"[gate] checks passed · burn {burn_after:.1%} unchanged · "
          f"sum {sum(gated.values()):.6f}", flush=True)

    if args.dry_run:
        print("[gate] DRY RUN — nothing committed", flush=True)
        print("===ALPHA-GATE-EXIT=0===", flush=True)
        return 0

    from hope.validator.weights_commit import commit_weights_layer_9c3

    items = sorted((k, v) for k, v in gated.items() if v > 0)
    commit = commit_weights_layer_9c3(
        subtensor=subtensor,
        validator_wallet=wallet,
        netuid=args.netuid,
        uids=[int(k) for k, _ in items],
        weights=[float(v) for _, v in items],
        verify_applied=False,
    )
    print(f"[gate] commit success={commit.success} block={commit.block_number} "
          f"msg={commit.message!r}", flush=True)
    print(f"===ALPHA-GATE-EXIT={0 if commit.success else 1}===", flush=True)
    return 0 if commit.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
