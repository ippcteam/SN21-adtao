"""F-2 SLIMMED — validator-only chain submission for testnet sanity.

Runs ONLY the validator-side Layer 9.C extrinsics:
  - 9.C.1 pre-scoring state (TLE)
  - 9.C.3 weights commit (commit_timelocked_weights, separate pallet)
  - 9.C.2 post-scoring artifacts (TLE)

Skips HOPE 9.A.1/9.A.2 + miner 9.B because the testnet wallet has only
ONE hotkey (UID 0 on netuid 466) and MaxSpace is 3,100 B per RateLimit
window per (netuid, account). Running all 8 extrinsics from one hotkey
would exceed the cap. The slimmed run uses ~1,960 B + the 9.C.3 weights
extrinsic which goes through a different pallet.

Deferred: HOPE outcome commit-reveal + miner 9.B will be exercised on
mainnet (or in a multi-wallet testnet rerun) where each role has its
own hotkey.

Usage:
    python -m scripts.integration.run_validator_only --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="F-2 slimmed: validator-only")
    parser.add_argument("--epoch-id", default="LOCAL-EPOCH-2026-05-03-T1")
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--network", default="test")
    parser.add_argument("--validator-key",
                        default=str(Path("~/.sn21/keys/validator-ed25519.pem").expanduser()))
    parser.add_argument("--blocks-until-reveal", type=int, default=25)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output", default="scripts/phase0/results/f2_validator_only.json")
    args = parser.parse_args()

    if not args.yes:
        sys.stderr.write("Submit 3 chain extrinsics from validator wallet? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() != "y":
            return 1

    import bittensor as bt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from hope.commitment.canonical import canonical_cbor_dumps
    from hope.commitment.drand_lib import drand_round_at
    from hope.commitment.on_chain import (
        compute_sha256,
        submit_pre_scoring_state_layer_9c1,
        submit_post_scoring_artifacts_layer_9c2,
    )
    from hope.commitment.scoring_state import (
        MinerCommitRecord,
        build_pre_scoring_state,
        build_post_scoring_artifacts,
    )
    from hope.validator.weights_commit import (
        commit_weights_layer_9c3,
        estimate_weights_reveal_round,
    )

    # Load validator's ed25519 key
    sk = serialization.load_pem_private_key(
        Path(args.validator_key).expanduser().read_bytes(), password=None,
    )
    if not isinstance(sk, Ed25519PrivateKey):
        raise SystemExit(f"{args.validator_key} is not an ed25519 private key")
    val_pk = sk.public_key().public_bytes_raw()

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network=args.network)
    metagraph = st.metagraph(netuid=args.netuid)
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        raise SystemExit(f"hotkey not registered on netuid {args.netuid}")
    uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)

    print(f"validator: {wallet.hotkey.ss58_address} (UID {uid})")
    print(f"val ed25519 pk: {val_pk.hex()}")
    print(f"current block: {st.get_current_block()}")
    print()

    summary = {
        "epoch_id": args.epoch_id,
        "uid": uid,
        "validator_hotkey_ss58": wallet.hotkey.ss58_address,
        "validator_ed25519_pk": val_pk.hex(),
    }

    # ---- 9.C.1 pre-scoring state ----
    print("9.C.1 pre-scoring state (TLE) ...")
    # Synthesize one miner record (UID 0 itself, for testnet sanity).
    miner_records = [MinerCommitRecord(
        miner_hotkey=val_pk,  # same hotkey for testnet sanity
        k_block=st.get_current_block(),
        k_round=drand_round_at(int(time.time())),
        sha256_ct=compute_sha256(b"placeholder-aes-ct"),
    )]
    pre_blob = build_pre_scoring_state(
        validator_hotkey=val_pk, validator_signing_key=sk,
        epoch_id=args.epoch_id, epoch_idx=1,
        outcomes_release_round=drand_round_at(int(time.time())) - 200,
        outcomes_fetched_at_round=drand_round_at(int(time.time())),
        miner_commits=miner_records, excluded_miners=[],
    )
    print(f"  9.C.1 plaintext: {len(pre_blob)}B")
    res_9c1 = submit_pre_scoring_state_layer_9c1(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        pre_scoring_state_cbor=pre_blob,
        blocks_until_reveal=args.blocks_until_reveal,
    )
    print(f"  9.C.1 success={res_9c1.success} block={res_9c1.block_number} reveal_round={res_9c1.reveal_round}")
    summary["9c1"] = {
        "success": res_9c1.success,
        "block": res_9c1.block_number,
        "extrinsic_hash": res_9c1.extrinsic_hash,
        "reveal_round": res_9c1.reveal_round,
        "plaintext_bytes": len(pre_blob),
        "message": res_9c1.message,
    }
    if not res_9c1.success:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=2))
        print(f"\nABORTED at 9.C.1: {res_9c1.message}")
        print(f"wrote {args.output}")
        return 5

    # ---- 9.C.3 weights ----
    print()
    print("9.C.3 weights commit (commit_timelocked_weights) ...")
    weights_res = commit_weights_layer_9c3(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        uids=[uid], weights=[1.0],
    )
    print(f"  9.C.3 success={weights_res.success} block={weights_res.block_number}")
    summary["9c3"] = {
        "success": weights_res.success,
        "block": weights_res.block_number,
        "block_hash": weights_res.block_hash.hex() if weights_res.block_hash else None,
        "extrinsic_hash": weights_res.extrinsic_hash,
        "message": weights_res.message,
    }
    if not weights_res.success or weights_res.block_hash is None:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=2))
        print(f"\nABORTED at 9.C.3: {weights_res.message}")
        print(f"wrote {args.output}")
        return 6
    weights_reveal = estimate_weights_reveal_round(
        current_round=drand_round_at(int(time.time())),
        blocks_until_reveal=args.blocks_until_reveal,
    )

    # ---- 9.C.2 post-scoring artifacts ----
    print()
    print("9.C.2 post-scoring artifacts (TLE) ...")
    post_blob = build_post_scoring_artifacts(
        validator_hotkey=val_pk, validator_signing_key=sk,
        epoch_id=args.epoch_id, epoch_idx=1,
        scoring_inputs_hash=compute_sha256(b"scoring-inputs-placeholder"),
        scored_miners=[],
        weights_commit_block_hash=weights_res.block_hash,
        weights_reveal_round=weights_reveal,
    )
    print(f"  9.C.2 plaintext: {len(post_blob)}B")
    res_9c2 = submit_post_scoring_artifacts_layer_9c2(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        post_scoring_artifacts_cbor=post_blob,
        blocks_until_reveal=args.blocks_until_reveal,
    )
    print(f"  9.C.2 success={res_9c2.success} block={res_9c2.block_number} reveal_round={res_9c2.reveal_round}")
    summary["9c2"] = {
        "success": res_9c2.success,
        "block": res_9c2.block_number,
        "extrinsic_hash": res_9c2.extrinsic_hash,
        "reveal_round": res_9c2.reveal_round,
        "plaintext_bytes": len(post_blob),
        "message": res_9c2.message,
    }

    # Final summary
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2))
    print()
    all_ok = res_9c1.success and weights_res.success and res_9c2.success
    print(f"VERDICT: {'ALL 3 OK' if all_ok else 'PARTIAL FAILURE'}")
    print(f"wrote {args.output}")
    return 0 if all_ok else 7


if __name__ == "__main__":
    sys.exit(main())
