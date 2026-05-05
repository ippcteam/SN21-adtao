"""Verify the aggregated plaintext binds episodes_root + bundle SHA together.

Phase E binding: when a miner ships per-episode entries, the on-chain
TLE plaintext carries `episodes_root` (IMT root over the entries) AND
`episodes_bundle_sha256` (SHA-256 of the off-chain bundle bytes).
Tampering with either side breaks `verify_inner_sig` or
`verify_bundle_root`.
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.episode_artifacts import (
    PerEpisodeEntry,
    build_per_episode_bundle,
    build_per_episode_entry,
    compute_episodes_imt_root,
    parse_per_episode_bundle,
    verify_bundle_inner_sig,
    verify_bundle_root,
)
from hope.commitment.inner_sig import verify_inner_sig
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
)


def _make_signing_key():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    return sk, pk


def _make_horizon_entry(label: str = "7"):
    return build_horizon_entry(
        horizon=label,
        cost_q=(-5.0, 0.0, 5.0),
        conv_q=(-3.0, 0.0, 3.0),
        eff_q=(-2.0, 0.0, 2.0),
        goal_miss_probability=0.5,
        instability_risk=0.2,
    )


def _make_entries():
    return [
        PerEpisodeEntry(
            episode_id="EP-001",
            horizon="7",
            cost_q=(-5.0, 0.0, 5.0),
            conv_q=(-3.0, 0.0, 3.0),
            eff_q=(-2.0, 0.0, 2.0),
            goal_miss_probability=0.4,
            instability_risk=0.2,
        ),
        PerEpisodeEntry(
            episode_id="EP-002",
            horizon="14",
            cost_q=(-7.0, -1.0, 5.0),
            conv_q=(-4.0, 1.0, 6.0),
            eff_q=(-1.0, 0.5, 2.0),
            goal_miss_probability=0.6,
            instability_risk=0.3,
        ),
    ]


def test_plaintext_carries_root_and_bundle_sha():
    sk, pk = _make_signing_key()
    entries = _make_entries()
    encoded = [build_per_episode_entry(e) for e in entries]
    root = compute_episodes_imt_root(encoded)
    bundle = build_per_episode_bundle(
        epoch_id="WR-2026-W18-PUB-E1",
        miner_hotkey=pk,
        submitted_round=1234567,
        entries=entries,
        miner_signing_key=sk,
    )
    bundle_sha = hashlib.sha256(bundle).digest()

    plaintext = build_prediction_plaintext(
        epoch_id="WR-2026-W18-PUB-E1",
        miner_hotkey=pk,
        submitted_round=1234567,
        horizons=[_make_horizon_entry("7"), _make_horizon_entry("14")],
        miner_signing_key=sk,
        episodes_root=root,
        episodes_bundle_sha256=bundle_sha,
    )

    assert plaintext["episodes_root"] == root
    assert plaintext["episodes_bundle_sha256"] == bundle_sha
    # inner_sig binds both fields.
    assert verify_inner_sig(plaintext, pk, hotkey_field="miner_hotkey")


def test_bundle_root_matches_plaintext_root():
    sk, pk = _make_signing_key()
    entries = _make_entries()
    bundle_bytes = build_per_episode_bundle(
        epoch_id="WR-2026-W18-PUB-E1",
        miner_hotkey=pk,
        submitted_round=1234567,
        entries=entries,
        miner_signing_key=sk,
    )
    bundle = parse_per_episode_bundle(bundle_bytes)

    encoded = [build_per_episode_entry(e) for e in entries]
    expected_root = compute_episodes_imt_root(encoded)

    assert verify_bundle_inner_sig(bundle, pk)
    assert verify_bundle_root(bundle, expected_root=expected_root)


def test_bundle_sha_mismatch_signals_tamper():
    """If the archive serves a tampered bundle, its SHA changes — the chain-
    anchored bundle SHA in the plaintext lets the verifier reject it."""
    sk, pk = _make_signing_key()
    entries = _make_entries()
    bundle = build_per_episode_bundle(
        epoch_id="WR-2026-W18-PUB-E1",
        miner_hotkey=pk,
        submitted_round=1234567,
        entries=entries,
        miner_signing_key=sk,
    )
    real_sha = hashlib.sha256(bundle).digest()

    tampered = bytearray(bundle)
    tampered[0] ^= 0x01
    tampered_sha = hashlib.sha256(bytes(tampered)).digest()

    assert real_sha != tampered_sha


def test_episodes_bundle_sha_requires_root():
    """Defensive: passing bundle_sha without episodes_root must error."""
    import pytest

    sk, pk = _make_signing_key()
    with pytest.raises(ValueError, match="episodes_root"):
        build_prediction_plaintext(
            epoch_id="WR-2026-W18-PUB-E1",
            miner_hotkey=pk,
            submitted_round=1234567,
            horizons=[_make_horizon_entry("7")],
            miner_signing_key=sk,
            episodes_root=None,
            episodes_bundle_sha256=b"\x00" * 32,
        )


def test_plaintext_inner_sig_rejects_root_tamper():
    """Flipping a bit in episodes_root after signing breaks inner_sig."""
    sk, pk = _make_signing_key()
    entries = _make_entries()
    encoded = [build_per_episode_entry(e) for e in entries]
    root = compute_episodes_imt_root(encoded)
    plaintext = build_prediction_plaintext(
        epoch_id="WR-2026-W18-PUB-E1",
        miner_hotkey=pk,
        submitted_round=1234567,
        horizons=[_make_horizon_entry("7")],
        miner_signing_key=sk,
        episodes_root=root,
    )

    # Round-trip via canonical CBOR to simulate post-signing storage.
    encoded_plaintext = canonical_cbor_loads(canonical_cbor_dumps(plaintext))
    encoded_plaintext["episodes_root"] = bytes(b ^ 0xFF for b in root)

    assert not verify_inner_sig(encoded_plaintext, pk, hotkey_field="miner_hotkey")
