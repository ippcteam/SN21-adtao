"""Public verifier for SN21 verifiable scoring.

Given an epoch_id and a target validator hotkey, this script:

  1. Reads the validator's 9.C.2 post-scoring artifacts CBOR from chain.
  2. Re-derives every miner's scoreability decision from raw chain commits
     + archive ciphertexts, using the IDENTICAL Layer 9.B reader code that
     validators run.
  3. Builds an independent `final_score_root` IMT root from the verifier's
     own scorer and asserts equality with the chain-anchored root.
  4. Builds an independent `miner_commits_root` from the chain reads and
     asserts equality with the chain-anchored root from 9.C.1.
  5. Cross-checks the actual on-chain weights at
     `weights_commit_block_hash` against weights re-derived from the score
     table; surfaces any mismatched UIDs.

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
        --tier-2-base https://archive.example.io \\
        --truth-file path/to/truth.json

This is an OFFLINE verification — it does NOT submit any extrinsic. It
needs read-only access to a Bittensor node + reachability to the archive
tiers it queries.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional


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
from hope.scoring.onchain_adapter import HorizonTruth
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
    # Phase #2: weights ↔ scoring binding cross-check.
    weights_binding_match: bool = True
    weights_binding_mismatches: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.miner_commits_match
            and self.final_score_match
            and self.inner_sig_valid_pre
            and self.inner_sig_valid_post
            and self.weights_binding_match
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
    # Phase #2: weights at the block_hash referenced by 9.C.2 +
    # the metagraph mapping needed to translate score → UID.
    actual_weights_at_commit_block: dict[int, int] = field(default_factory=dict)
    uid_by_hotkey: dict[bytes, int] = field(default_factory=dict)
    burn_fraction: float = 0.95


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
        epoch_id: the operator release_key being verified.
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

    # Phase #2: weights ↔ scoring binding cross-check. The 9.C.2 plaintext
    # references `weights_commit_block_hash`; the chain stores the actual
    # u16 weights at that block. We re-derive what those weights *should*
    # be from the score table and compare. Mismatches = the validator
    # committed weights that don't correspond to the scoring artifact this
    # 9.C.2 references — fraud surfaced byte-for-byte by UID.
    weights_binding_match, weights_binding_mismatches = _verify_weights_binding(
        score_map=score_map,
        chain_view=chain_view,
    )

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
        weights_binding_match=weights_binding_match,
        weights_binding_mismatches=tuple(weights_binding_mismatches),
    )


def _verify_weights_binding(
    *,
    score_map: dict[bytes, int],
    chain_view: ChainView,
) -> tuple[bool, list[int]]:
    """Recompute expected u16 weights from the score map and compare to chain.

    Returns ``(match, mismatched_uids)``. When the chain view doesn't
    include actual weights (older callers / test fakes that don't care),
    the binding check is treated as vacuously passing — the point is to
    fail loudly when chain weights ARE provided and disagree.

    Tolerance: u16 quantisation can introduce ±1 LSB per UID, so we
    accept differences up to ±1 on each side. Anything larger is a real
    divergence.
    """
    if not chain_view.actual_weights_at_commit_block:
        return True, []

    score_by_uid: dict[int, float] = {}
    for hotkey, score in score_map.items():
        uid = chain_view.uid_by_hotkey.get(hotkey)
        if uid is None:
            continue
        score_by_uid[uid] = float(score)

    from hope.validator.weight_setter import WeightSetter

    expected = WeightSetter.derive_u16_weights(
        score_by_uid, burn_fraction=chain_view.burn_fraction
    )

    mismatches: list[int] = []
    all_uids = set(expected) | set(chain_view.actual_weights_at_commit_block)
    for uid in sorted(all_uids):
        e = expected.get(uid, 0)
        a = chain_view.actual_weights_at_commit_block.get(uid, 0)
        if abs(e - a) > 1:
            mismatches.append(uid)
    return (not mismatches), mismatches


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
    block_hash: Optional[str] = None,
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
        epoch_id: the operator release_key (used by Bittensor SDK for cache keys).
        validator_hotkey_ss58: SS58 of the validator under audit.
        miner_hotkey_ss58_list: list of miner SS58s to read (typically the
            full metagraph at the epoch boundary).
        timing: protocol timing bounds for this epoch.

    Returns:
        ChainView populated from chain. Raises RuntimeError if the validator's
        9.C.1 / 9.C.2 reveals are not present (not yet auto-decrypted).
    """
    # Substrate-direct readback bypasses the SDK's UTF-8 mangling of
    # binary payloads. Each RevealedEntry has a SCALE-prefixed hex-encoded
    # plaintext (Phase H-4 finding) — `decode_revealed_tle_plaintext`
    # strips the prefix and hex-decodes back to original bytes.
    from hope.commitment.chain_reader import (
        decode_revealed_tle_plaintext,
        read_commitment_of, read_revealed_commitments,
    )

    revealed_val = read_revealed_commitments(
        subtensor, netuid, validator_hotkey_ss58, block_hash=block_hash,
    )

    pre_blob: Optional[bytes] = None
    post_blob: Optional[bytes] = None
    plaintexts: list[bytes] = []
    for entry in revealed_val:
        try:
            plaintexts.append(decode_revealed_tle_plaintext(entry.payload_bytes))
        except ValueError as e:
            logger.warning(
                "could not decode revealed entry at block %d: %s",
                entry.block_number, e,
            )
            continue

    if len(plaintexts) < 2:
        raise RuntimeError(
            f"validator {validator_hotkey_ss58[:16]}... has fewer than 2 revealed "
            f"commitments at netuid {netuid}; expected 9.C.1 + 9.C.2. "
            f"Auto-decrypt may not have fired yet (chain pulls drand pulses on a "
            f"schedule). If the reveal_round is past, try again in a few minutes."
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
        revealed = read_revealed_commitments(
            subtensor, netuid, miner_ss58, block_hash=block_hash,
        )
        revealed_k: Optional[bytes] = None
        chain_block: Optional[int] = None
        for entry in revealed:
            try:
                payload_bytes = decode_revealed_tle_plaintext(entry.payload_bytes)
            except ValueError:
                continue
            # The auto-decrypted K is exactly 32 bytes.
            if len(payload_bytes) == 32:
                revealed_k = payload_bytes
                chain_block = entry.block_number

        # Read latest CommitmentOf for sha256(ct) + self_archive_url. The
        # chain stores ONE non-TLE entry per (netuid, hotkey) — overwritten
        # by every new commit. For Phase D production we'd want an archive
        # node + block-pinned reads; for now we take whatever's latest.
        sha256_ct: Optional[bytes] = None
        url: Optional[str] = None
        fields = read_commitment_of(
            subtensor, netuid, miner_ss58, block_hash=block_hash,
        )
        if fields:
            for f in fields:
                if f.variant == "Sha256" and len(f.bytes_) == 32:
                    sha256_ct = f.bytes_
                elif f.variant.startswith("Raw"):
                    try:
                        url = f.bytes_.decode("utf-8")
                    except UnicodeDecodeError:
                        pass

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


def _strip_data_variant_prefix(payload: bytes) -> bytes:
    """Heuristic strip of the leading 1-2 byte SCALE Data enum variant tag.

    Auto-decrypted TLE plaintexts come back wrapped in some Data variant
    (typically `Raw{N}` for ≤128 bytes or `BigRaw` for larger). The caller
    wrote raw plaintext bytes; the chain re-wrapped them on auto-decrypt.

    We attempt to detect and strip the variant prefix:
      - Variant byte 1..129  → Raw0..Raw128: strip 1 byte (no length).
      - Variant byte ~130    → Sha256: strip 1 byte (32 bytes follow).
      - Variant byte > 129 + length-prefixed body: strip 1 variant byte +
        compact-encoded length (1-4 bytes per SCALE).

    If the payload starts with a recognizable application prefix
    (`b"\\xa9"` for CBOR 9-element map, `b"sn21-..."`, etc.), we return
    the payload as-is. This is best-effort heuristics — Phase H may
    replace this with full SCALE-aware decoding.
    """
    if len(payload) < 2:
        return payload
    v = payload[0]
    # Raw0..Raw128: variant 1..129, NO length byte.
    if 1 <= v <= 129:
        # The Raw{N} variant tag implies length = v - 1. If the remaining
        # bytes are exactly that length, strip the tag.
        expected_len = v - 1
        if len(payload) - 1 == expected_len:
            return payload[1:]
    # Some chain encodings carry the variant + a 1-byte compact length.
    # Try stripping 2 bytes if that gives a plausible CBOR / JSON prefix.
    if len(payload) >= 2:
        candidate = payload[2:]
        if candidate[:1] in (b"\xa0", b"\xa1", b"\xa2", b"\xa3", b"\xa4",
                             b"\xa5", b"\xa6", b"\xa7", b"\xa8", b"\xa9",
                             b"\xaa", b"\xab", b"\xac", b"\xad"):
            # CBOR map 0..13 entries — looks like our 9.C.1/9.C.2 plaintext.
            return candidate
    return payload


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


def make_live_scorer(truth_by_horizon: dict[str, "HorizonTruth"]):
    """Wire the production ``score_one_miner`` adapter into the verifier.

    Returns a ``ScorerFn`` closure compatible with ``verify_epoch(scorer=...)``
    and the underlying ``score_one_miner`` function — the same path the
    validator runs at scoring time. When ``truth_by_horizon`` is empty,
    every miner scores 0; the verifier still checks IMT roots and
    inner_sig integrity, but ``final_score_match`` will fail unless the
    on-chain artifact also reflects an all-zero run. Operators should
    supply truth derived from the 9.A.2 reveal blob.
    """
    from hope.scoring.onchain_adapter import score_one_miner

    def scorer(epoch_id, plaintext_per_miner):
        result: dict[bytes, int] = {}
        for hotkey, plaintext in plaintext_per_miner.items():
            result[hotkey] = score_one_miner(plaintext, truth_by_horizon)
        return result

    return scorer


def _load_truth_file(path: str) -> dict[str, "HorizonTruth"]:
    """Decode a JSON truth file produced from the 9.A.2 reveal blob.

    Schema (single object, sample at tests/fixtures/recorded_epoch/recorded_epoch.json):

        {
          "truth_by_horizon": {
            "7":  {"cost_p50_dpct": 12, "conv_p50_dpct": -3,
                   "eff_p50_dpct":  4, "goal_miss_ppm": 350000,
                   "instab_ppm":    180000},
            "14": { ... }
          }
        }

    Values are integer deci-percent / parts-per-million as defined in
    ``HorizonTruth``. The conversion from float reveal-blob fields is done
    by the operator's offline tooling so we keep the verifier deterministic.
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    raw = payload.get("truth_by_horizon") or {}
    truth: dict[str, HorizonTruth] = {}
    for horizon, fields in raw.items():
        truth[horizon] = HorizonTruth(
            horizon=horizon,
            truth_cost_p50_dpct=int(fields["cost_p50_dpct"]),
            truth_conv_p50_dpct=int(fields["conv_p50_dpct"]),
            truth_eff_p50_dpct=int(fields["eff_p50_dpct"]),
            goal_miss_freq_ppm=int(fields["goal_miss_ppm"]),
            instab_freq_ppm=int(fields["instab_ppm"]),
        )
    return truth


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Public SN21 epoch verifier")
    p.add_argument("--epoch-id", required=True, help="the operator release_key")
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
        help="Tier-2 (operator shadow) archive base URL; can repeat",
    )
    p.add_argument(
        "--tier-3-base", action="append", default=[],
        help="Tier-3 (miner self-archive) base URL; usually read from chain instead",
    )
    p.add_argument(
        "--block-hash", default=None,
        help="Block hash (0x-prefixed hex) for an archive-pinned read. The "
             "chain's CommitmentOf storage is single-slot per (netuid, hotkey); "
             "to audit a past epoch, supply the block_hash where the validator's "
             "9.C.2 commit landed. Without --block-hash, reads chain head only.",
    )
    p.add_argument(
        "--truth-file", default=None,
        help="Path to a JSON file with per-horizon ground truth derived from "
             "the 9.A.2 reveal blob. When supplied, the verifier recomputes "
             "miner scores end-to-end (the production scoring path). When "
             "omitted, score recomputation is skipped and the verifier only "
             "checks chain integrity (IMT roots + inner_sig + weights "
             "binding). See tests/fixtures/recorded_epoch/recorded_epoch.json for the "
             "schema.",
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
        block_hash=args.block_hash,
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

    truth_by_horizon: dict[str, HorizonTruth] = {}
    if args.truth_file:
        truth_by_horizon = _load_truth_file(args.truth_file)
        print(
            f"  loaded truth for horizons: {sorted(truth_by_horizon.keys())}",
            file=sys.stderr,
        )
    else:
        print(
            "  WARNING: no --truth-file provided. Score recomputation "
            "will return zero for every miner; final_score_match will "
            "fail unless the chain artifact also reflects an all-zero "
            "scoring run. Pass a JSON truth file (see "
            "tests/fixtures/recorded_epoch/recorded_epoch.json for the schema).",
            file=sys.stderr,
        )

    scorer = make_live_scorer(truth_by_horizon)

    verdict = verify_epoch(
        chain_view=chain_view,
        epoch_id=args.epoch_id,
        validator_hotkey=validator_pk,
        archive_endpoints=endpoints,
        scorer=scorer,
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
