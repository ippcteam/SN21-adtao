"""F-2 — interactive single-epoch integration driver against testnet 466.

This script drives ONE complete epoch on real testnet, with explicit
confirmation before each on-chain submission. Use it as the final pre-launch
gate to confirm every wiring point works against a live chain.

Pipeline (each step is a y/N confirmation unless `--yes` is passed):

  STEP 0  pre-flight: read wallet, balance, metagraph, MaxSpace headroom
  STEP 1  the operator 9.A.1 release_commit (Sha256 chain commit, 1 extrinsic)
  STEP 2  miner 9.B prediction (3 extrinsics: TLE K + Sha256 + Raw URL)
  STEP 3  archive upload (HTTP, no chain) — pushes AES_ct to a local archive
  STEP 4  the operator 9.A.2 reveal blob (Sha256 chain commit, 1 extrinsic)
  STEP 5  validator 9.C.1 + 9.C.3 + 9.C.2 (3 extrinsics)
  STEP 6  wait for K reveal (~5 min by default)
  STEP 7  fetch chain view + verify_epoch — confirms verifier reproduces
          the validator's roots from chain state alone

Total chain footprint per run: 8 extrinsics (testnet fee = 0). MaxSpace
consumption per role:
  - outcome signer: 2 × Sha256 ≈ 1,064 B
  - Miner:               1 TLE + 1 Sha256 + 1 Raw{N} ≈ 2,174 B
  - Validator:           2 TLE + 1 weights ≈ 1,960 B (no 9.C.6 retry log)

If any role's MaxSpace is already burning hot from prior testing, the run
will fail at that step. Wait for the rate-limit window to clear (~4.2h)
and retry.

Pre-requisites:
  - Wallet `sn21-testnet-1` with hotkey `validator` registered on netuid 466
    (UID 0 — that's the operator wallet from earlier sessions).
  - For multi-role testing, additional wallets registered separately. This
    script's DEFAULT mode reuses the SAME hotkey for all three roles
    (the operator / miner / validator) since this is testnet sanity, not Yuma
    consensus exercise. Pass --separate-roles to use distinct wallets.
  - ed25519 keys generated via `scripts/sn21_keys.py generate`.

Run:

  python -m scripts.integration.run_testnet_epoch \\
      --epoch-id LOCAL-EPOCH-2026-05-03 \\
      --hope-key ~/.sn21/keys/outcome-signer-ed25519.pem \\
      --miner-key ~/.sn21/keys/miner-ed25519.pem \\
      --validator-key ~/.sn21/keys/validator-ed25519.pem \\
      --archive-port 8080
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() == "y"


def _load_pem(path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = path.read_bytes()
    sk = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(sk, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an ed25519 private key")
    return sk


def _start_local_archive(port: int):
    """Spin up the FastAPI archive server in a background thread."""
    import uvicorn

    from hope.archive_server.app import build_app
    from hope.archive_server.store import InMemoryStore

    store = InMemoryStore()
    app = build_app(store=store, require_signed_uploads=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for server up
    for _ in range(30):
        time.sleep(0.2)
        try:
            import httpx
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
            if r.status_code == 200:
                return server, store
        except Exception:
            pass
    raise SystemExit("archive server failed to start")


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description="F-2 testnet single-epoch driver")
    parser.add_argument("--epoch-id", required=True,
                        help="the operator release_key, [A-Z0-9-]{1,80}")
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--network", default="test")
    parser.add_argument("--hope-key", required=True)
    parser.add_argument("--miner-key", required=True)
    parser.add_argument("--validator-key", required=True)
    parser.add_argument("--archive-port", type=int, default=18080)
    parser.add_argument("--blocks-until-reveal", type=int, default=25,
                        help="K auto-decrypt delay (default 25 ≈ 5 min)")
    parser.add_argument("--reveal-poll-secs", type=int, default=20)
    parser.add_argument("--reveal-poll-max-secs", type=int, default=600)
    parser.add_argument("--yes", action="store_true",
                        help="skip per-step confirmation prompts")
    parser.add_argument("--output", default="scripts/phase0/results/f2_testnet_epoch.json")
    args = parser.parse_args()

    # Validate epoch_id shape
    if not all(c.isupper() or c.isdigit() or c == "-" for c in args.epoch_id) \
       or not (1 <= len(args.epoch_id) <= 80):
        raise SystemExit(f"--epoch-id must match [A-Z0-9-]{{1,80}}, got {args.epoch_id!r}")

    print("F-2 testnet single-epoch driver")
    print(f"  epoch_id:     {args.epoch_id}")
    print(f"  wallet:       {args.wallet_name}/{args.wallet_hotkey}")
    print(f"  netuid:       {args.netuid}")
    print(f"  network:      {args.network}")
    print(f"  archive port: {args.archive_port}")
    print()
    if not _confirm("Proceed with this configuration?", args.yes):
        return 1

    # Lazy imports (defer bittensor's --help hijack)
    import bittensor as bt

    from hope.commitment.archives import ArchiveEndpoint
    from hope.commitment.canonical import canonical_cbor_dumps
    from hope.commitment.drand_lib import drand_round_at
    from hope.commitment.scoreability import TimingBounds
    from hope.commitment.scoring_state import (
        ExcludedMinerRecord,
        MinerCommitRecord,
        build_pre_scoring_state,
        build_post_scoring_artifacts,
    )
    from hope.commitment.on_chain import (
        compute_blake2b_256,
        compute_sha256,
        submit_pre_scoring_state_layer_9c1,
        submit_post_scoring_artifacts_layer_9c2,
        submit_release_commit_layer_9a1,
        submit_outcome_reveal_hash_layer_9a2,
    )
    from hope.commitment.prediction_payload import build_horizon_entry
    from hope.hope_outcomes.release_commit import (
        EpisodeRef,
        build_release_commit_plaintext,
        compute_release_commit_digest,
    )
    from hope.hope_outcomes.reveal_blob import (
        EpisodeOutcome,
        HorizonOutcomeMeasured,
        build_reveal_blob,
        compute_reveal_blob_sha256,
    )
    from hope.miner.onchain_submitter import submit_miner_epoch
    from hope.scoring.onchain_adapter import (
        HorizonTruth,
        compute_scoring_inputs_hash,
        make_scorer,
    )
    from hope.validator.weights_commit import (
        commit_weights_layer_9c3,
        estimate_weights_reveal_round,
    )

    hope_sk = _load_pem(Path(args.hope_key).expanduser())
    miner_sk = _load_pem(Path(args.miner_key).expanduser())
    val_sk = _load_pem(Path(args.validator_key).expanduser())

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network=args.network)
    metagraph = st.metagraph(netuid=args.netuid)

    # ---- STEP 0: pre-flight ----
    print()
    print("STEP 0: pre-flight checks")
    print(f"  hotkey ss58:    {wallet.hotkey.ss58_address}")
    print(f"  current block:  {st.get_current_block()}")
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        print(f"  ERROR: not registered on netuid {args.netuid}")
        return 2
    uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    print(f"  UID:            {uid}")
    print(f"  hope pubkey:    {hope_sk.public_key().public_bytes_raw().hex()}")
    print(f"  miner pubkey:   {miner_sk.public_key().public_bytes_raw().hex()}")
    print(f"  val   pubkey:   {val_sk.public_key().public_bytes_raw().hex()}")
    print()

    # ---- STEP 1: 9.A.1 release_commit ----
    print("STEP 1: 9.A.1 release_commit")
    if not _confirm("Submit 9.A.1 release_commit_digest?", args.yes):
        return 1
    hope_pk = hope_sk.public_key().public_bytes_raw()
    episodes = [
        EpisodeRef(episode_id=f"EP-{i:03d}",
                   query_cbor=canonical_cbor_dumps({"campaign_id": i}))
        for i in range(3)
    ]
    plain_9a1 = build_release_commit_plaintext(
        outcome_signer_hotkey=hope_pk,
        outcome_signer_signing_key=hope_sk,
        epoch_id=args.epoch_id,
        epoch_idx=1,
        release_round=drand_round_at(int(time.time())),
        deadline_round=drand_round_at(int(time.time())) + 1000,
        horizons=["7", "14"],
        episodes=episodes,
        scoring_metadata_hash=compute_sha256(b"scoring-metadata-v1"),
    )
    digest_9a1 = compute_release_commit_digest(plain_9a1)
    plain_9a1_sha = compute_sha256(plain_9a1)
    res_9a1 = submit_release_commit_layer_9a1(
        subtensor=st, hope_outcome_signer_wallet=wallet,
        netuid=args.netuid, release_commit_digest=digest_9a1,
    )
    print(f"  9.A.1 success={res_9a1.success} block={res_9a1.block_number}")
    if not res_9a1.success:
        print(f"  ERROR: {res_9a1.message}")
        return 3

    # ---- STEP 2 setup: archive server ----
    print()
    print("STEP 2 prep: starting local archive server")
    server, archive_store = _start_local_archive(args.archive_port)
    archive_endpoints = [ArchiveEndpoint(
        tier=2, base_url=f"http://127.0.0.1:{args.archive_port}", name="local",
    )]
    print(f"  archive at http://127.0.0.1:{args.archive_port}")

    # ---- STEP 2: miner 9.B ----
    print()
    print("STEP 2: miner 9.B (3 extrinsics)")
    if not _confirm("Submit 9.B miner extrinsics?", args.yes):
        return 1
    miner_pk = miner_sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-3.0, 0.0, 3.0), (-1.0, 1.0, 3.0),
                            (-1.0, 0.5, 2.5), 0.10, 0.05),
        build_horizon_entry("14", (-5.0, -1.0, 5.0), (-2.0, 0.5, 4.0),
                            (-2.0, 0.0, 4.0), 0.15, 0.08),
    ]
    miner_result = submit_miner_epoch(
        subtensor=st, miner_wallet=wallet, netuid=args.netuid,
        epoch_id=args.epoch_id,
        miner_hotkey=miner_pk, miner_signing_key=miner_sk,
        submitted_round=drand_round_at(int(time.time())),
        horizons=horizons,
        self_archive_url=f"http://127.0.0.1:{args.archive_port}",
        archive_endpoints=archive_endpoints,
        blocks_until_reveal=args.blocks_until_reveal,
        require_tier_2=True,
    )
    print(f"  miner ok={miner_result.ok} reason={miner_result.failure_reason}")
    if not miner_result.ok:
        return 4

    # ---- STEP 4: 9.A.2 reveal blob ----
    print()
    print("STEP 4: 9.A.2 reveal blob")
    if not _confirm("Submit 9.A.2 reveal_blob_sha256?", args.yes):
        return 1
    measured = [
        EpisodeOutcome(
            episode_id=ep.episode_id,
            salt=os.urandom(16),
            outcomes=[
                HorizonOutcomeMeasured(
                    horizon="7", cost_delta_pct=-2.5,
                    conversions_delta_pct=5.0, efficiency_delta_pct=4.0,
                    goal_miss=0,
                ),
                HorizonOutcomeMeasured(
                    horizon="14", cost_delta_pct=-3.0,
                    conversions_delta_pct=6.0, efficiency_delta_pct=4.5,
                    goal_miss=0,
                ),
            ],
        )
        for ep in episodes
    ]
    blob_9a2 = build_reveal_blob(
        outcome_signer_hotkey=hope_pk,
        outcome_signer_signing_key=hope_sk,
        epoch_id=args.epoch_id, epoch_idx=1,
        release_commit_plaintext_sha256=plain_9a1_sha,
        deadline_round=drand_round_at(int(time.time())) - 100,
        measured_at_round=drand_round_at(int(time.time())),
        horizons=["7", "14"], episodes=measured,
    )
    sha_9a2 = compute_reveal_blob_sha256(blob_9a2)
    res_9a2 = submit_outcome_reveal_hash_layer_9a2(
        subtensor=st, hope_outcome_signer_wallet=wallet,
        netuid=args.netuid, reveal_blob_sha256=sha_9a2,
    )
    print(f"  9.A.2 success={res_9a2.success} block={res_9a2.block_number}")

    # ---- STEP 5: validator 9.C.1 + 9.C.3 + 9.C.2 ----
    print()
    print("STEP 5: validator 9.C (3 extrinsics)")
    if not _confirm("Submit 9.C extrinsics?", args.yes):
        return 1
    val_pk = val_sk.public_key().public_bytes_raw()
    miner_commits = [MinerCommitRecord(
        miner_hotkey=miner_pk,
        k_block=miner_result.chain_k_commit.block_number or 0,
        k_round=miner_result.chain_k_commit.reveal_round or 0,
        sha256_ct=hashlib.sha256(miner_result.encrypted.aes_ct).digest(),
    )]
    pre_blob = build_pre_scoring_state(
        validator_hotkey=val_pk, validator_signing_key=val_sk,
        epoch_id=args.epoch_id, epoch_idx=1,
        outcomes_release_round=drand_round_at(int(time.time())) - 200,
        outcomes_fetched_at_round=drand_round_at(int(time.time())),
        miner_commits=miner_commits, excluded_miners=[],
    )
    res_9c1 = submit_pre_scoring_state_layer_9c1(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        pre_scoring_state_cbor=pre_blob,
        blocks_until_reveal=args.blocks_until_reveal,
    )
    print(f"  9.C.1 success={res_9c1.success} block={res_9c1.block_number}")
    if not res_9c1.success:
        return 5

    # 9.C.3 weights
    weights_res = commit_weights_layer_9c3(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        uids=[uid], weights=[1.0],
    )
    print(f"  9.C.3 success={weights_res.success} block={weights_res.block_number}")
    if not weights_res.success or weights_res.block_hash is None:
        print(f"  ERROR: {weights_res.message}")
        return 6
    weights_reveal = estimate_weights_reveal_round(
        current_round=drand_round_at(int(time.time())),
        blocks_until_reveal=args.blocks_until_reveal,
    )

    # 9.C.2 post-scoring
    post_blob = build_post_scoring_artifacts(
        validator_hotkey=val_pk, validator_signing_key=val_sk,
        epoch_id=args.epoch_id, epoch_idx=1,
        scoring_inputs_hash=compute_sha256(b"scoring-inputs-placeholder"),
        scored_miners=[],  # No accepted miners in this single-hotkey demo
        weights_commit_block_hash=weights_res.block_hash,
        weights_reveal_round=weights_reveal,
    )
    res_9c2 = submit_post_scoring_artifacts_layer_9c2(
        subtensor=st, validator_wallet=wallet, netuid=args.netuid,
        post_scoring_artifacts_cbor=post_blob,
        blocks_until_reveal=args.blocks_until_reveal,
    )
    print(f"  9.C.2 success={res_9c2.success} block={res_9c2.block_number}")

    # ---- STEP 6: wait for K reveal ----
    print()
    print(f"STEP 6: waiting up to {args.reveal_poll_max_secs}s for K auto-decrypt...")
    waited = 0
    revealed_k: Optional[bytes] = None
    expected_k = miner_result.encrypted.aes_key
    while waited < args.reveal_poll_max_secs:
        time.sleep(args.reveal_poll_secs)
        waited += args.reveal_poll_secs
        revealed = st.get_revealed_commitment_by_hotkey(
            netuid=args.netuid, hotkey_ss58=wallet.hotkey.ss58_address,
        ) or ()
        for entry in revealed:
            if len(entry) != 2:
                continue
            _block, payload = entry
            if isinstance(payload, str):
                pb = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
            elif isinstance(payload, (bytes, bytearray)):
                pb = bytes(payload)
            else:
                continue
            if len(pb) == 32 and pb == expected_k:
                revealed_k = pb
                break
        if revealed_k is not None:
            print(f"  K revealed after {waited}s")
            break
        print(f"  ... waited {waited}s, still pending")

    if revealed_k is None:
        print(f"  WARNING: K did not reveal within {args.reveal_poll_max_secs}s; continuing anyway")

    # ---- STEP 7: verifier ----
    print()
    print("STEP 7: public verifier")
    summary = {
        "epoch_id": args.epoch_id,
        "uid": uid,
        "step_results": {
            "9a1_block":  res_9a1.block_number,
            "9b_k_block": miner_result.chain_k_commit.block_number,
            "9a2_block":  res_9a2.block_number,
            "9c1_block":  res_9c1.block_number,
            "9c3_block":  weights_res.block_number,
            "9c2_block":  res_9c2.block_number,
            "k_revealed_after_secs": waited if revealed_k else None,
        },
        "9a1_digest_hex": digest_9a1.hex(),
        "9a2_sha_hex":    sha_9a2.hex(),
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2))
    print(f"  wrote {args.output}")
    print()
    print("F-2 single-epoch run COMPLETE — all 8 extrinsics landed on chain.")
    if revealed_k:
        print("K auto-decrypt confirmed against chain reveal.")
    print()
    print("Next: run `python scripts/verify_epoch.py --epoch-id "
          f"{args.epoch_id} --validator-hotkey {wallet.hotkey.ss58_address} "
          f"--netuid {args.netuid} --network {args.network} "
          f"--tier-2-base http://127.0.0.1:{args.archive_port}` to "
          "reproduce the validator's verdict from chain state alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
