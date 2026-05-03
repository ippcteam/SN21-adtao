"""Layer 9.E — HOPE Shadow Validator.

The HOPE Foundation runs an independent validator that:

  1. Reads the SAME on-chain miner commits any other validator sees.
  2. Runs the SAME scoreability rule + scoring algorithm.
  3. Submits its OWN 9.C.1 / 9.C.2 / 9.C.3 / (optionally) 9.C.6 commits
     to its OWN hotkey on the SAME (netuid, account) → different storage slot.

The rubber-stamp attack (SH-5) is the threat: a malicious primary validator
publishes 9.C.1/9.C.2 ciphertexts, a colluding "shadow" copies the same
ciphertext bytes verbatim and posts them under its own hotkey. The chain
storage account differs — but a naive auditor that only checks "are there
two independent reveals?" would be fooled.

The defence is `inner_sig.add_inner_sig(plaintext, key, "validator_hotkey")`:
the plaintext carries the validator's own 32-byte ed25519 public key and a
signature over canonical-CBOR(plaintext sans inner_sig). When the shadow
runs `read_miner_for_epoch` etc. and emits its OWN plaintext, the
validator_hotkey field is its own; signed with its own key. Rubber-stamping
primary's bytes would result in:

  - The shadow's storage slot containing a plaintext whose validator_hotkey
    field equals the PRIMARY's hotkey, not the shadow's.
  - `verify_inner_sig(plaintext, shadow_hotkey)` fails because the field
    inside doesn't match the storage account.

A verifier checking BOTH primary and shadow notices the divergence
immediately — primary slot's plaintext.validator_hotkey == primary, but
shadow slot's plaintext.validator_hotkey is also == primary, which is
impossible if shadow ran honestly.

This module is otherwise a thin wrapper around `run_epoch_scoring`: same
inputs, same scorer, different signing key + chain account.
"""

from __future__ import annotations

import logging
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveClient, ArchiveEndpoint
from hope.commitment.scoreability import TimingBounds
from hope.validator.onchain_runner import (
    EpochScoringOutcome,
    MinerOnChainInputs,
    ScorerFn,
    run_epoch_scoring,
)

logger = logging.getLogger(__name__)


def run_shadow_epoch(
    *,
    subtensor,
    shadow_wallet,
    shadow_hotkey: bytes,
    shadow_signing_key: Ed25519PrivateKey,
    netuid: int,
    epoch_id: str,
    epoch_idx: int,
    miner_inputs: list[MinerOnChainInputs],
    archive_endpoints: list[ArchiveEndpoint],
    archive_client: Optional[ArchiveClient],
    timing: TimingBounds,
    outcomes_release_round: int,
    outcomes_fetched_at_round: int,
    scoring_inputs_hash: bytes,
    scorer: ScorerFn,
    blocks_until_pre_scoring_reveal: int,
    blocks_until_post_scoring_reveal: int,
    blocks_until_weights_reveal: int,
) -> EpochScoringOutcome:
    """Run one epoch as the HOPE shadow validator.

    The signature is the SAME as `run_epoch_scoring(...)` — there's nothing
    structurally different from a primary validator's run. The only behavioural
    difference is which wallet signs the chain extrinsics and which ed25519
    private key signs the inner_sig of the 9.C.1/9.C.2 plaintexts.

    A SHADOW MUST NOT take its 9.C.1 / 9.C.2 plaintexts from primary's chain
    reveals — by construction the shadow signs its own copy. If primary's
    miner_commits_root or final_score_root differs, the verifier sees BOTH
    the primary slot AND the shadow slot under their respective hotkeys; an
    attentive third party can spot any 1-of-N validator dishonesty by
    comparing roots across slots.

    Args:
        shadow_wallet: HOPE's Bittensor `Wallet` (different from primary).
        shadow_hotkey: 32-byte raw ed25519 public key of the shadow validator.
        shadow_signing_key: ed25519 private key matching `shadow_hotkey`.
        (other args identical to `run_epoch_scoring`)

    Returns:
        Same `EpochScoringOutcome` as `run_epoch_scoring`.
    """
    logger.info(
        "shadow validator starting epoch=%s shadow_hotkey=%s n_miners=%d",
        epoch_id, shadow_hotkey.hex()[:16], len(miner_inputs),
    )
    return run_epoch_scoring(
        subtensor=subtensor,
        validator_wallet=shadow_wallet,
        netuid=netuid,
        epoch_id=epoch_id,
        epoch_idx=epoch_idx,
        validator_hotkey=shadow_hotkey,
        validator_signing_key=shadow_signing_key,
        miner_inputs=miner_inputs,
        archive_endpoints=archive_endpoints,
        archive_client=archive_client,
        timing=timing,
        outcomes_release_round=outcomes_release_round,
        outcomes_fetched_at_round=outcomes_fetched_at_round,
        scoring_inputs_hash=scoring_inputs_hash,
        scorer=scorer,
        blocks_until_pre_scoring_reveal=blocks_until_pre_scoring_reveal,
        blocks_until_post_scoring_reveal=blocks_until_post_scoring_reveal,
        blocks_until_weights_reveal=blocks_until_weights_reveal,
    )
