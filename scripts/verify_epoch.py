"""Public verifier for SN21 verifiable scoring (Phase B Layer 9 protocol).

Given an epoch_id and a target validator hotkey, this script:

  1. Reads the validator's 9.C.2 post-scoring artifacts CBOR from chain.
  2. Re-derives every miner's scoreability decision from raw chain commits
     + archive ciphertexts, using the IDENTICAL Layer 9.B reader code that
     validators run.
  3. Builds an independent `final_score_root` IMT root from the verifier's
     own scorer and asserts equality with the chain-anchored root.
  4. Builds an independent `miner_commits_root` from the chain reads and
     asserts equality with the chain-anchored root from 9.C.1.

If any equality check fails, the script exits non-zero with a structured
diff between the chain claim and the verifier's view. Any third party
running this command on a working node should reach the same verdict — if
not, exactly one party (validator or verifier) is at fault, and the
deterministic re-derivation makes the fault attributable.

Usage:
    python scripts/verify_epoch.py \\
        --epoch-id EPOCH-2026-05-02-XYZ \\
        --validator-hotkey 5GxVLdpRGZN... \\
        --netuid 21 \\
        --network finney \\
        --tier-2-base https://archive.hope.example

This is an OFFLINE verification — it does NOT submit any extrinsic. It
needs read-only access to a Bittensor node + reachability to the archive
tiers it queries.

In Phase B this is intentionally a scaffold: the chain-read functions and
the per-miner score-recompute logic are integration points that need real
Bittensor SDK calls + the project's scoring module wired up. The structure
here pins the algorithm and CLI surface so a third party can audit it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hope.commitment.archives import ArchiveClient, ArchiveEndpoint
from hope.commitment.canonical import canonical_cbor_loads
from hope.commitment.inner_sig import verify_inner_sig
from hope.commitment.scoreability import TimingBounds
from hope.commitment.scoring_state import (
    MinerCommitRecord,
    ScoredMinerRecord,
    compute_final_score_root,
    compute_miner_commits_root,
)
from hope.validator.onchain_reader import (
    assemble_chain_commits,
    read_miner_for_epoch,
)


logger = logging.getLogger("verify_epoch")


@dataclass(frozen=True)
class VerifierVerdict:
    """Outcome of a full epoch verification run.

    A pass requires BOTH roots to match. A fail records the discrete
    discrepancy so an operator can attribute fault.
    """

    epoch_id: str
    validator_hotkey: bytes
    chain_miner_commits_root: bytes
    derived_miner_commits_root: bytes
    chain_final_score_root: bytes
    derived_final_score_root: bytes
    miner_commits_match: bool
    final_score_match: bool
    inner_sig_valid_pre: bool
    inner_sig_valid_post: bool
    n_miners_chain: int
    n_miners_derived: int
    excluded_count: int

    @property
    def ok(self) -> bool:
        return (
            self.miner_commits_match
            and self.final_score_match
            and self.inner_sig_valid_pre
            and self.inner_sig_valid_post
        )

    def as_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "epoch_id": self.epoch_id,
                "validator_hotkey": self.validator_hotkey.hex(),
                "chain_miner_commits_root": self.chain_miner_commits_root.hex(),
                "derived_miner_commits_root": self.derived_miner_commits_root.hex(),
                "chain_final_score_root": self.chain_final_score_root.hex(),
                "derived_final_score_root": self.derived_final_score_root.hex(),
                "miner_commits_match": self.miner_commits_match,
                "final_score_match": self.final_score_match,
                "inner_sig_valid_pre": self.inner_sig_valid_pre,
                "inner_sig_valid_post": self.inner_sig_valid_post,
                "n_miners_chain": self.n_miners_chain,
                "n_miners_derived": self.n_miners_derived,
                "excluded_count": self.excluded_count,
            },
            sort_keys=True,
            indent=2,
        )


@dataclass(frozen=True)
class ChainView:
    """The chain side of the picture, as a third party reads it.

    `pre_scoring_state_cbor` and `post_scoring_artifacts_cbor` are the bytes
    auto-decrypted from the validator's 9.C.1 / 9.C.2 commits. `miner_states`
    is the per-miner triple of (timelock_k_revealed, sha256_ct_commit,
    self_archive_url, chain_block_at_k_commit) keyed by miner hotkey.
    """

    pre_scoring_state_cbor: bytes
    post_scoring_artifacts_cbor: bytes
    miner_states: dict[bytes, "ChainMinerState"]
    timing: TimingBounds


@dataclass(frozen=True)
class ChainMinerState:
    miner_uid: int
    timelock_k_revealed: Optional[bytes]
    sha256_ct_commit: Optional[bytes]
    self_archive_url: Optional[str]
    chain_block_at_k_commit: Optional[int]
    k_reveal_round: int = 0  # drand round encoded in K commit; 0 if not known


def verify_epoch(
    *,
    chain_view: ChainView,
    epoch_id: str,
    validator_hotkey: bytes,
    archive_endpoints: list[ArchiveEndpoint],
    archive_client: Optional[ArchiveClient] = None,
    scorer,
) -> VerifierVerdict:
    """Run the public verification end-to-end.

    Args:
        chain_view: pre-fetched chain state. The CLI fills this from a real
            Bittensor node; tests fill it with deterministic fakes.
        epoch_id: HOPE release_key being verified.
        validator_hotkey: 32-byte hotkey of the validator we're auditing.
        archive_endpoints: ordered list of archive tiers to consult.
        archive_client: shared httpx client, or default.
        scorer: callable taking
            (epoch_id, plaintext_dict_per_miner: dict[bytes, dict])
            and returning {miner_hotkey_bytes: score_micro_uint}. The
            verifier supplies the project's `EpochScorer`; tests supply a
            stub that emits a deterministic mapping.

    Returns:
        VerifierVerdict — `ok` is True iff every chain-anchored root matches.
    """
    pre = canonical_cbor_loads(chain_view.pre_scoring_state_cbor)
    post = canonical_cbor_loads(chain_view.post_scoring_artifacts_cbor)
    if not isinstance(pre, dict) or not isinstance(post, dict):
        raise ValueError("9.C.1 / 9.C.2 plaintexts must be CBOR maps")

    inner_sig_valid_pre = verify_inner_sig(
        pre, validator_hotkey, hotkey_field="validator_hotkey"
    )
    inner_sig_valid_post = verify_inner_sig(
        post, validator_hotkey, hotkey_field="validator_hotkey"
    )

    chain_miner_commits_root = pre.get("miner_commits_root", b"")
    chain_final_score_root = post.get("final_score_root", b"")

    if archive_client is None:
        archive_client = ArchiveClient()

    # Re-derive per-miner reads + scoreability + scoring.
    derived_commits: list[MinerCommitRecord] = []
    score_inputs: dict[bytes, dict] = {}
    excluded = 0
    for miner_hotkey, ms in chain_view.miner_states.items():
        cc = assemble_chain_commits(
            revealed_k_plaintext=ms.timelock_k_revealed,
            sha256_ct_commit=ms.sha256_ct_commit,
            self_archive_url=ms.self_archive_url,
            chain_block_at_k_commit=ms.chain_block_at_k_commit,
            miner_hotkey=miner_hotkey,
        )
        result = read_miner_for_epoch(
            chain_commits=cc,
            archive_client=archive_client,
            archive_endpoints=archive_endpoints,
            epoch_id=epoch_id,
            timing=chain_view.timing,
            miner_uid=ms.miner_uid,
            miner_identity_for_archive=miner_hotkey.hex(),
        )
        # Build IMT inputs even for excluded miners — the chain's
        # miner_commits_root covers EVERY miner whose K + Sha256 commits
        # landed in-window, regardless of whether scoring accepted them.
        if ms.timelock_k_revealed is not None and ms.sha256_ct_commit is not None:
            from hope.commitment.drand_lib import drand_round_at  # type: ignore

            derived_commits.append(MinerCommitRecord(
                miner_hotkey=miner_hotkey,
                k_block=ms.chain_block_at_k_commit or 0,
                k_round=_extract_k_round(ms),
                sha256_ct=ms.sha256_ct_commit,
            ))
        if result.ok and result.plaintext is not None:
            score_inputs[miner_hotkey] = result.plaintext
        else:
            excluded += 1

    derived_miner_commits_root = compute_miner_commits_root(derived_commits)

    score_map: dict[bytes, int] = scorer(epoch_id, score_inputs)
    derived_records = [
        ScoredMinerRecord(miner_hotkey=hk, score_micro=v) for hk, v in score_map.items()
    ]
    derived_final_score_root = compute_final_score_root(derived_records)

    return VerifierVerdict(
        epoch_id=epoch_id,
        validator_hotkey=validator_hotkey,
        chain_miner_commits_root=chain_miner_commits_root,
        derived_miner_commits_root=derived_miner_commits_root,
        chain_final_score_root=chain_final_score_root,
        derived_final_score_root=derived_final_score_root,
        miner_commits_match=(
            chain_miner_commits_root == derived_miner_commits_root
        ),
        final_score_match=(
            chain_final_score_root == derived_final_score_root
        ),
        inner_sig_valid_pre=inner_sig_valid_pre,
        inner_sig_valid_post=inner_sig_valid_post,
        n_miners_chain=int(pre.get("n_miners", 0)),
        n_miners_derived=len(derived_commits),
        excluded_count=excluded,
    )


def _extract_k_round(ms: ChainMinerState) -> int:
    """Return the drand round encoded in the K commit.

    Phase D: the caller fills `ChainMinerState.k_reveal_round` when reading
    chain state. If not provided (legacy callers), 0 is used and the IMT
    root will diverge from a validator that embedded a real round — that
    divergence is by design: it's how the verifier surfaces a mismatched
    reveal_round.
    """
    return int(ms.k_reveal_round or 0)


def fetch_chain_view(
    *,
    subtensor,
    netuid: int,
    epoch_id: str,
    validator_hotkey_ss58: str,
    miner_hotkey_ss58_list: list[str],
    timing: TimingBounds,
) -> ChainView:
    """Read pre/post scoring CBOR + per-miner triples from a live chain.

    The validator's 9.C.1 / 9.C.2 commits are TLE'd; once the reveal_round
    has passed, the chain auto-decrypts and `get_revealed_commitment_by_hotkey`
    returns the plaintext bytes for that hotkey. We fetch the most-recent
    revealed plaintext per validator/miner.

    Per-miner:
      - K plaintext: from `get_revealed_commitment_by_hotkey`.
      - Sha256(ct) commit: from `subtensor.query` against the Commitments pallet.
        Falls back to `get_commitment` for the most recent value as the chain
        retains the latest non-timelock commit per (netuid, account).
      - self_archive_url: same source as Sha256.

    NOTE: a real validator/miner publishes THREE chain commits per epoch
    (Sha256, TimelockEncrypted, RawN). The chain stores only the latest
    non-timelock entry per (netuid, account); reading historical ones
    requires an archive node + block-pinned reads. For Phase C we retrieve
    the latest stored values; verifying historical epochs needs an archive
    node and is left for Phase D.

    Args:
        subtensor: Bittensor `Subtensor` instance.
        netuid: subnet ID.
        epoch_id: HOPE release_key (used by Bittensor SDK for cache keys).
        validator_hotkey_ss58: SS58 of the validator under audit.
        miner_hotkey_ss58_list: list of miner SS58s to read (typically the
            full metagraph at the epoch boundary).
        timing: protocol timing bounds for this epoch.

    Returns:
        ChainView populated from chain. Raises RuntimeError if the validator's
        9.C.1 / 9.C.2 reveals are not present (not yet auto-decrypted).
    """
    revealed_val = subtensor.get_revealed_commitment_by_hotkey(
        netuid=netuid, hotkey_ss58=validator_hotkey_ss58
    ) or ()

    pre_blob: Optional[bytes] = None
    post_blob: Optional[bytes] = None
    # Reveals come back as ((block, hex_or_bytes), ...) ordered oldest-first.
    # The validator commits 9.C.1 BEFORE 9.C.2, so the older reveal is 9.C.1.
    plaintexts: list[bytes] = []
    for entry in revealed_val:
        if len(entry) != 2:
            continue
        _block, payload = entry
        if isinstance(payload, str):
            payload = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
        elif isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        else:
            continue
        plaintexts.append(payload)

    if len(plaintexts) < 2:
        raise RuntimeError(
            f"validator {validator_hotkey_ss58[:16]}... has fewer than 2 revealed "
            f"commitments at netuid {netuid}; expected 9.C.1 + 9.C.2"
        )
    pre_blob = plaintexts[-2]
    post_blob = plaintexts[-1]

    miner_states: dict[bytes, ChainMinerState] = {}
    for i, miner_ss58 in enumerate(miner_hotkey_ss58_list):
        try:
            miner_pk = _ss58_to_raw_ed25519(miner_ss58)
        except Exception:
            logger.warning("could not decode miner SS58 %s; skipping", miner_ss58)
            continue
        revealed = subtensor.get_revealed_commitment_by_hotkey(
            netuid=netuid, hotkey_ss58=miner_ss58
        ) or ()
        revealed_k: Optional[bytes] = None
        chain_block: Optional[int] = None
        for entry in revealed:
            if len(entry) != 2:
                continue
            block, payload = entry
            if isinstance(payload, str):
                pb = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
            elif isinstance(payload, (bytes, bytearray)):
                pb = bytes(payload)
            else:
                continue
            # The auto-decrypted K is exactly 32 bytes; if the latest reveal
            # is something else (e.g., a previous epoch's pre-scoring blob),
            # ignore it.
            if len(pb) == 32:
                revealed_k = pb
                chain_block = int(block)

        # Sha256(ct) commit + self_archive_url from `get_commitment`. The
        # chain returns the LATEST `Sha256` and the latest `Raw{N}` separately;
        # we read the latter via the same API (it returns whichever Data
        # variant was last written, hex-encoded for Raw).
        latest = subtensor.get_commitment(netuid=netuid, uid=i)
        sha256_ct: Optional[bytes] = None
        url: Optional[str] = None
        if latest is not None:
            try:
                # If the latest is hex of 32 bytes, treat as Sha256 commit.
                raw = bytes.fromhex(latest[2:] if latest.startswith("0x") else latest)
                if len(raw) == 32:
                    sha256_ct = raw
                else:
                    # Otherwise treat as Raw{N} URL.
                    url = raw.decode("utf-8", errors="replace")
            except ValueError:
                # Fallback: treat as already-decoded UTF-8 URL string.
                url = latest

        miner_states[miner_pk] = ChainMinerState(
            miner_uid=i,
            timelock_k_revealed=revealed_k,
            sha256_ct_commit=sha256_ct,
            self_archive_url=url,
            chain_block_at_k_commit=chain_block,
        )

    return ChainView(
        pre_scoring_state_cbor=pre_blob,
        post_scoring_artifacts_cbor=post_blob,
        miner_states=miner_states,
        timing=timing,
    )


def _ss58_to_raw_ed25519(ss58_address: str) -> bytes:
    """Decode an SS58 address to its 32-byte raw public key.

    Bittensor's `Keypair` exposes the underlying public key bytes; we use
    that path so callers don't need to import substrate-specific tooling.
    """
    try:
        from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
    except Exception:
        try:
            from substrateinterface import Keypair  # type: ignore
        except Exception as e:
            raise RuntimeError(f"no Keypair available for SS58 decode: {e}") from e
    kp = Keypair(ss58_address=ss58_address)
    pk = getattr(kp, "public_key", None)
    if pk is None:
        raise RuntimeError("Keypair has no public_key attribute")
    if isinstance(pk, str):
        pk = bytes.fromhex(pk[2:] if pk.startswith("0x") else pk)
    if not isinstance(pk, (bytes, bytearray)) or len(pk) != 32:
        raise RuntimeError(f"unexpected public_key shape: {type(pk).__name__}")
    return bytes(pk)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Public SN21 epoch verifier")
    p.add_argument("--epoch-id", required=True, help="HOPE release_key")
    p.add_argument(
        "--validator-hotkey", required=True,
        help="SS58 of the validator hotkey to audit (mainnet) "
             "or hex 32-byte raw key",
    )
    p.add_argument("--netuid", type=int, default=21, help="Subnet ID (21 mainnet)")
    p.add_argument(
        "--network", default="finney",
        help="Bittensor network name (finney, test, archive)",
    )
    p.add_argument(
        "--tier-1-base", action="append", default=[],
        help="Tier-1 (validator) archive base URL; can repeat for multiple validators",
    )
    p.add_argument(
        "--tier-2-base", action="append", default=[],
        help="Tier-2 (HOPE shadow) archive base URL; can repeat",
    )
    p.add_argument(
        "--tier-3-base", action="append", default=[],
        help="Tier-3 (miner self-archive) base URL; usually read from chain instead",
    )
    p.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="Verdict output format",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_argparser().parse_args(argv)

    print("verify_epoch.py — SN21 public verifier", file=sys.stderr)
    print(f"  epoch_id={args.epoch_id}", file=sys.stderr)
    print(f"  netuid={args.netuid} network={args.network}", file=sys.stderr)

    try:
        import bittensor as bt  # type: ignore
    except ImportError:
        print("ERROR: bittensor not installed; cannot run live verification.", file=sys.stderr)
        return 2

    subtensor = bt.Subtensor(network=args.network)
    metagraph = subtensor.metagraph(netuid=args.netuid)
    miner_ss58s = list(metagraph.hotkeys)
    timing = TimingBounds(
        epoch_open_round=0,
        miner_deadline_round=2**63 - 1,  # accept anything; placeholder for Phase D
        chain_window_min_block=0,
        chain_window_max_block=2**63 - 1,
    )
    chain_view = fetch_chain_view(
        subtensor=subtensor,
        netuid=args.netuid,
        epoch_id=args.epoch_id,
        validator_hotkey_ss58=args.validator_hotkey,
        miner_hotkey_ss58_list=miner_ss58s,
        timing=timing,
    )

    endpoints: list[ArchiveEndpoint] = []
    for url in args.tier_1_base:
        endpoints.append(ArchiveEndpoint(tier=1, base_url=url, name="tier-1"))
    for url in args.tier_2_base:
        endpoints.append(ArchiveEndpoint(tier=2, base_url=url, name="tier-2"))
    for url in args.tier_3_base:
        endpoints.append(ArchiveEndpoint(tier=3, base_url=url, name="tier-3"))
    if not endpoints:
        print(
            "ERROR: at least one --tier-1-base / --tier-2-base / --tier-3-base required.",
            file=sys.stderr,
        )
        return 2

    # Decode the validator hotkey SS58 → raw bytes for inner_sig verification.
    validator_pk = _ss58_to_raw_ed25519(args.validator_hotkey)

    def _placeholder_scorer(epoch_id, plaintext_per_miner):
        # Phase C: a real scorer must be wired in here that mirrors what the
        # validator ran. For now we return an empty map so the verifier
        # focuses on the IMT root + inner_sig integrity checks; the score
        # mismatch is expected in this scaffold and the output documents it.
        return {}

    verdict = verify_epoch(
        chain_view=chain_view,
        epoch_id=args.epoch_id,
        validator_hotkey=validator_pk,
        archive_endpoints=endpoints,
        scorer=_placeholder_scorer,
    )
    if args.output == "json":
        print(verdict.as_json())
    else:
        print(f"OK: {verdict.ok}")
        print(f"  miner_commits_match: {verdict.miner_commits_match}")
        print(f"  final_score_match:   {verdict.final_score_match}")
        print(f"  inner_sig (pre):     {verdict.inner_sig_valid_pre}")
        print(f"  inner_sig (post):    {verdict.inner_sig_valid_post}")
        print(f"  miners (chain/derived): {verdict.n_miners_chain}/{verdict.n_miners_derived}")
        print(f"  excluded:            {verdict.excluded_count}")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
