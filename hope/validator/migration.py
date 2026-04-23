"""HTTP → on-chain migration utilities.

The legacy SN21 validator collects predictions via HTTP into
`EpochManager.predictions: dict[miner_hotkey, dict[episode_id, Prediction]]`.
The on-chain pipeline expects a list of `MinerOnChainInputs` keyed by
chain-anchored fields (TLE'd K, Sha256(AES_ct), self_archive_url, …).

This module bridges the two so a validator transitioning from HTTP-only to
chain mode can:

  1. Re-package each miner's HTTP predictions as the equivalent Layer 9.B
     payload (same canonical CBOR schema the chain path uses).
  2. Encrypt each payload with a freshly-generated K (deterministic per
     miner per epoch — see `_deterministic_test_key` for the seed).
  3. Stash the ciphertext in a local archive (`InMemoryStore` for unit
     tests; `FilesystemStore` for production pre-flight rehearsals).
  4. Synthesize `MinerOnChainInputs` records that point at those local
     ciphertexts — the validator can then run `run_epoch_scoring(...)`
     against them and verify the orchestration produces the same scoring
     results as the HTTP path.

This is NOT a way to forge real on-chain commits; the synthesized inputs
have `chain_block_at_k_commit=None` and revealed_k filled in directly. It's
a debug + dual-mode aid: a validator running BOTH paths can compare the
chain pipeline's score map against the HTTP scorer's, gaining confidence
before flipping the cutover switch.

Cutover guidance
----------------
We recommend running BOTH paths for at least one full epoch:

  - HTTP path scores miners via `EpochScorer.score_epoch` as today.
  - Chain path scores via `run_epoch_scoring(scorer=...)` against the
    inputs synthesized here.
  - Compare `score_map` from both. Disagreement at this stage means either
    the scorer adapter (`make_scorer`) needs tuning or the scoring inputs
    differ — run the diff before going live.

Once both paths agree, flip `--mode http` → `--mode onchain` and remove the
HTTP collection endpoints.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.prediction_payload import (
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.miner.runner import MinerRunner
from hope.protocol.prediction import Prediction
from hope.validator.onchain_runner import MinerOnChainInputs

logger = logging.getLogger(__name__)


def _deterministic_aes_key(epoch_id: str, miner_hotkey: bytes) -> bytes:
    """Deterministic K for replay — NOT for production use.

    Production miners generate K via `os.urandom`. For dual-mode rehearsal we
    need a stable K so re-running the migration on the same inputs produces
    byte-identical ciphertexts (so the synthesized AES_ct can be diff'd).
    The seed is `"sn21-migration-v1:" + epoch_id + miner_hotkey` hashed.
    """
    seed = b"sn21-migration-v1:" + epoch_id.encode("utf-8") + miner_hotkey
    return hashlib.blake2b(seed, digest_size=32).digest()


def _deterministic_nonce(epoch_id: str, miner_hotkey: bytes) -> bytes:
    """Deterministic 12-byte nonce derived from a separate domain-seed.

    Same caveat as `_deterministic_aes_key` — only for migration replay.
    """
    seed = b"sn21-migration-nonce-v1:" + epoch_id.encode("utf-8") + miner_hotkey
    return hashlib.blake2b(seed, digest_size=12).digest()


def replay_http_predictions_as_chain_inputs(
    *,
    epoch_id: str,
    http_predictions: dict[bytes, list[Prediction]],
    miner_signing_keys: dict[bytes, Ed25519PrivateKey],
    starting_uid: int = 0,
    starting_block: int = 0,
    starting_round: int = 12345700,
) -> tuple[list[MinerOnChainInputs], dict[bytes, bytes]]:
    """Convert HTTP-collected predictions into chain-shaped inputs.

    Args:
        epoch_id: the operator release_key.
        http_predictions: per-miner list of Prediction (per-episode). The
            converter aggregates them into per-horizon CBOR entries via the
            same `MinerRunner._predictions_to_horizon_entries` the chain
            miner uses, so miners who only ever submitted via HTTP score the
            same way they would have via chain.
        miner_signing_keys: per-miner ed25519 private key for inner_sig.
            Test/migration setups generate these locally; production
            cutover would source them from the miners' registration commits
           .
        starting_uid / starting_block / starting_round: synthesized chain
            metadata — these go into the IMT leaf values so re-runs against
            the same inputs produce the same `miner_commits_root`.

    Returns:
        (miner_inputs, ct_by_sha256) where:
          - miner_inputs is a list of MinerOnChainInputs ready to pass to
            `run_epoch_scoring(...)`;
          - ct_by_sha256 maps SHA-256(AES_ct) → AES_ct bytes, suitable for
            stashing in an in-memory or filesystem archive.

    Raises:
        ValueError: if a miner_hotkey lacks a signing key or has 0 predictions.
    """
    miner_inputs: list[MinerOnChainInputs] = []
    ct_by_sha256: dict[bytes, bytes] = {}

    for i, (miner_hotkey, preds) in enumerate(sorted(http_predictions.items())):
        if not preds:
            logger.info("skipping miner %s: 0 predictions", miner_hotkey.hex()[:16])
            continue
        sk = miner_signing_keys.get(miner_hotkey)
        if sk is None:
            raise ValueError(
                f"missing signing key for miner {miner_hotkey.hex()[:16]}..."
            )

        horizons = MinerRunner._predictions_to_horizon_entries(preds)
        if not horizons:
            logger.info(
                "miner %s produced no horizon entries; skipping",
                miner_hotkey.hex()[:16],
            )
            continue

        plain = build_prediction_plaintext(
            epoch_id=epoch_id,
            miner_hotkey=miner_hotkey,
            submitted_round=starting_round + i,
            horizons=horizons,
            miner_signing_key=sk,
        )
        aes_key = _deterministic_aes_key(epoch_id, miner_hotkey)
        nonce = _deterministic_nonce(epoch_id, miner_hotkey)
        enc = encrypt_prediction(
            plain, epoch_id=epoch_id, aes_key=aes_key, nonce=nonce
        )
        sha_ct = hashlib.sha256(enc.aes_ct).digest()
        ct_by_sha256[sha_ct] = enc.aes_ct

        miner_inputs.append(MinerOnChainInputs(
            miner_uid=starting_uid + i,
            miner_hotkey=miner_hotkey,
            revealed_k=enc.aes_key,
            sha256_ct_commit=sha_ct,
            self_archive_url=f"local://migration/{miner_hotkey.hex()}",
            chain_block_at_k_commit=starting_block + i,
            k_reveal_round=starting_round + i,
        ))

    return miner_inputs, ct_by_sha256


def stash_into_archive(
    archive_client_or_store: object,
    *,
    epoch_id: str,
    ct_by_sha256: dict[bytes, bytes],
    miner_inputs: Iterable[MinerOnChainInputs],
) -> None:
    """Push synthesized AES_ct into a local store or archive endpoint.

    Two flavours are supported:
      - An object exposing `.put(epoch_id=..., miner_identity=..., sha256=..., data=...)`
        (i.e., an `ArchiveStore` like `InMemoryStore` or `FilesystemStore`).
      - An object exposing `.upload(endpoint, ...)` (an `ArchiveClient`).
        For this flavour the caller must also supply endpoints via a wrapping
        helper; we only handle the store-shape here for simplicity.

    Args:
        archive_client_or_store: target store.
        epoch_id, ct_by_sha256, miner_inputs: outputs of
            `replay_http_predictions_as_chain_inputs`.
    """
    has_put = hasattr(archive_client_or_store, "put")
    if not has_put:
        raise TypeError(
            "stash_into_archive expects an ArchiveStore (InMemoryStore / "
            "FilesystemStore); ArchiveClient.upload is not supported here"
        )
    for inp in miner_inputs:
        sha = inp.sha256_ct_commit
        if sha is None or sha not in ct_by_sha256:
            continue
        archive_client_or_store.put(
            epoch_id=epoch_id,
            miner_identity=inp.miner_hotkey.hex(),
            sha256=sha,
            data=ct_by_sha256[sha],
        )
