"""Cryptographic commitment primitives for SN21 verifiable scoring.

This package implements Foundation modules per the protocol spec:
- canonical: RFC 8949 §4.2.1 deterministic CBOR encoding + AES-GCM AAD construction
- imt: Indexed Merkle Tree (sorted-leaf with low/high pointers; non-inclusion proofs)
- inner_sig: 4-step ed25519 inner-signature procedure for validator commits (CL-9)
- drand_lib: drand quicknet TLE round computation + encryption helpers

Used by Layer 9.A (outcome commit-reveal), 9.B (miner predictions),
9.C (validator scoring commits), and the public verifier.
"""

from hope.commitment.canonical import (
    canonical_cbor_dumps,
    canonical_cbor_loads,
    aes_gcm_aad,
)
from hope.commitment.drand_lib import (
    QUICKNET_CHAIN_HASH,
    QUICKNET_GENESIS_UNIX,
    QUICKNET_PERIOD_SECS,
    QUICKNET_PUBLIC_KEY_HEX,
    drand_round_at,
    drand_round_to_unix,
)
from hope.commitment.imt import (
    IMTLeaf,
    InclusionProof,
    NonInclusionProof,
    imt_leaf_hash,
    imt_node_hash,
    imt_root,
    imt_proof_inclusion,
    imt_proof_non_inclusion,
    make_leaves_with_pointers,
    verify_inclusion,
    verify_non_inclusion,
)
from hope.commitment.inner_sig import add_inner_sig, verify_inner_sig
from hope.commitment.on_chain import (
    MAX_TLE_PLAINTEXT_BYTES,
    RAW_FIELD_MAX_BYTES,
    CommitResult,
    RevealedCommit,
    compute_blake2b_256,
    compute_sha256,
    # Note: `read_revealed_commitments` from on_chain (SDK-based) is
    # superseded by chain_reader.read_revealed_commitments (substrate-direct,
    # binary-safe). The chain_reader version is the canonical export below.
    submit_layer_9b_multi_field,
    submit_miner_prediction_layer_9b,
    submit_outcome_reveal_hash_layer_9a2,
    submit_post_scoring_artifacts_layer_9c2,
    submit_pre_scoring_state_layer_9c1,
    submit_raw_url_commit_layer_9b,
    submit_release_commit_layer_9a1,
    submit_retry_log_attestation_layer_9c6,
    submit_sha256_commit,
    submit_timelock_commit,
)
from hope.commitment.prediction_payload import (
    AES_KEY_BYTES,
    AES_NONCE_BYTES,
    AES_TAG_BYTES,
    ALLOWED_HORIZONS,
    PREDICTION_PAYLOAD_VERSION,
    PROBABILITY_SCALE,
    QUANTILE_SCALE,
    EncryptedPrediction,
    build_horizon_entry,
    build_prediction_plaintext,
    decrypt_prediction,
    encrypt_prediction,
    verify_prediction_inner_sig,
)
from hope.commitment.archives import (
    ArchiveClient,
    ArchiveEndpoint,
    FetchAggregate,
    FetchResult,
    UploadResult,
)
from hope.commitment.scoreability import (
    OnChainCommitTriple,
    ScoreabilityFailure,
    ScoreabilityResult,
    TimingBounds,
    evaluate_scoreability,
)
from hope.commitment.scoring_state import (
    SCORING_STATE_VERSION,
    ExcludedMinerRecord,
    MinerCommitRecord,
    ScoredMinerRecord,
    build_post_scoring_artifacts,
    build_pre_scoring_state,
    compute_excluded_miners_hash,
    compute_final_score_root,
    compute_miner_commits_root,
)
from hope.commitment.retry_log import (
    RETRY_LOG_VERSION,
    RetryLogAttempt,
    RetryLogMinerEntry,
    attempt_from_fetch_result,
    build_retry_log_blob,
    compute_retry_log_sha256,
)
from hope.commitment.registration import (
    REG_V1_PREFIX,
    RegistrationPayload,
    RegistrationRole,
    build_registration_payload,
    parse_registration_payload,
    submit_registration_commit,
    verify_registration,
)
from hope.commitment.chain_reader import (
    CommitEvent,
    RawCommitField,
    RevealedEntry,
    decode_revealed_tle_plaintext,
    read_commitment_of,
    read_events_at_block,
    read_revealed_commitments,
)
from hope.commitment.episode_artifacts import (
    EPISODE_BUNDLE_VERSION,
    PerEpisodeEntry,
    build_per_episode_bundle,
    build_per_episode_entry,
    compute_episodes_imt_root,
    parse_per_episode_bundle,
    prove_episode_inclusion,
    prove_episode_non_inclusion,
    verify_bundle_inner_sig,
    verify_bundle_root,
    verify_episode_inclusion,
    verify_episode_non_inclusion,
)

__all__ = [
    # canonical
    "canonical_cbor_dumps",
    "canonical_cbor_loads",
    "aes_gcm_aad",
    # drand
    "QUICKNET_CHAIN_HASH",
    "QUICKNET_GENESIS_UNIX",
    "QUICKNET_PERIOD_SECS",
    "QUICKNET_PUBLIC_KEY_HEX",
    "drand_round_at",
    "drand_round_to_unix",
    # IMT
    "IMTLeaf",
    "InclusionProof",
    "NonInclusionProof",
    "imt_leaf_hash",
    "imt_node_hash",
    "imt_root",
    "imt_proof_inclusion",
    "imt_proof_non_inclusion",
    "make_leaves_with_pointers",
    "verify_inclusion",
    "verify_non_inclusion",
    # inner_sig
    "add_inner_sig",
    "verify_inner_sig",
    # on_chain
    "MAX_TLE_PLAINTEXT_BYTES",
    "RAW_FIELD_MAX_BYTES",
    "CommitResult",
    "RevealedCommit",
    "compute_blake2b_256",
    "compute_sha256",
    "submit_layer_9b_multi_field",
    "submit_miner_prediction_layer_9b",
    "submit_outcome_reveal_hash_layer_9a2",
    "submit_post_scoring_artifacts_layer_9c2",
    "submit_pre_scoring_state_layer_9c1",
    "submit_raw_url_commit_layer_9b",
    "submit_release_commit_layer_9a1",
    "submit_retry_log_attestation_layer_9c6",
    "submit_sha256_commit",
    "submit_timelock_commit",
    # prediction_payload
    "AES_KEY_BYTES",
    "AES_NONCE_BYTES",
    "AES_TAG_BYTES",
    "ALLOWED_HORIZONS",
    "PREDICTION_PAYLOAD_VERSION",
    "PROBABILITY_SCALE",
    "QUANTILE_SCALE",
    "EncryptedPrediction",
    "build_horizon_entry",
    "build_prediction_plaintext",
    "decrypt_prediction",
    "encrypt_prediction",
    "verify_prediction_inner_sig",
    # archives
    "ArchiveClient",
    "ArchiveEndpoint",
    "FetchAggregate",
    "FetchResult",
    "UploadResult",
    # scoreability
    "OnChainCommitTriple",
    "ScoreabilityFailure",
    "ScoreabilityResult",
    "TimingBounds",
    "evaluate_scoreability",
    # scoring_state
    "SCORING_STATE_VERSION",
    "ExcludedMinerRecord",
    "MinerCommitRecord",
    "ScoredMinerRecord",
    "build_post_scoring_artifacts",
    "build_pre_scoring_state",
    "compute_excluded_miners_hash",
    "compute_final_score_root",
    "compute_miner_commits_root",
    # retry_log
    "RETRY_LOG_VERSION",
    "RetryLogAttempt",
    "RetryLogMinerEntry",
    "attempt_from_fetch_result",
    "build_retry_log_blob",
    "compute_retry_log_sha256",
    # chain_reader (substrate-direct readback bypassing SDK UTF-8 mangling)
    "CommitEvent",
    "RawCommitField",
    "RevealedEntry",
    "decode_revealed_tle_plaintext",
    "read_commitment_of",
    "read_events_at_block",
    "read_revealed_commitments",
    # registration
    "REG_V1_PREFIX",
    "RegistrationPayload",
    "RegistrationRole",
    "build_registration_payload",
    "parse_registration_payload",
    "submit_registration_commit",
    "verify_registration",
    # episode_artifacts
    "EPISODE_BUNDLE_VERSION",
    "PerEpisodeEntry",
    "build_per_episode_bundle",
    "build_per_episode_entry",
    "compute_episodes_imt_root",
    "parse_per_episode_bundle",
    "prove_episode_inclusion",
    "prove_episode_non_inclusion",
    "verify_bundle_inner_sig",
    "verify_bundle_root",
    "verify_episode_inclusion",
    "verify_episode_non_inclusion",
]
