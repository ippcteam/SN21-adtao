"""Unit tests for hope/commitment/episode_artifacts.py — Layer 9.B.1."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
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


@pytest.fixture
def signer():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes_raw()


def _entry(ep: str, h: str = "7"):
    return PerEpisodeEntry(
        episode_id=ep,
        horizon=h,
        cost_q=(-3.0, 0.0, 3.0),
        conv_q=(-1.0, 1.0, 3.0),
        eff_q=(-1.0, 0.5, 2.5),
        goal_miss_probability=0.10,
        instability_risk=0.05,
    )


@pytest.fixture
def entries():
    return [
        _entry(f"EP-{i:03d}", h)
        for i in range(8)
        for h in ("7", "14")
    ]


# ---------- entry encoding ----------


class TestBuildEntry:
    def test_basic(self):
        e = build_per_episode_entry(_entry("EP-1", "7"))
        assert e["ep"] == "EP-1"
        assert e["h"] == "7"
        assert e["cost_q"] == [-30, 0, 30]
        assert e["miss_p"] == 100_000
        assert e["instab_p"] == 50_000

    def test_invalid_horizon(self):
        with pytest.raises(ValueError, match="horizon"):
            build_per_episode_entry(PerEpisodeEntry(
                episode_id="EP-1", horizon="30",
                cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                goal_miss_probability=0.1, instability_risk=0.05,
            ))

    def test_invalid_episode_id(self):
        with pytest.raises(ValueError, match="invalid episode_id"):
            build_per_episode_entry(PerEpisodeEntry(
                episode_id="ep/with/slash", horizon="7",
                cost_q=(0, 0, 0), conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                goal_miss_probability=0.1, instability_risk=0.05,
            ))

    def test_non_monotone_quantile(self):
        with pytest.raises(ValueError, match="not monotone"):
            build_per_episode_entry(PerEpisodeEntry(
                episode_id="EP-1", horizon="7",
                cost_q=(5.0, 0.0, -5.0),
                conv_q=(0, 0, 0), eff_q=(0, 0, 0),
                goal_miss_probability=0.1, instability_risk=0.05,
            ))


# ---------- IMT root ----------


class TestComputeRoot:
    def test_deterministic(self, entries):
        encoded = [build_per_episode_entry(e) for e in entries]
        a = compute_episodes_imt_root(encoded)
        b = compute_episodes_imt_root(encoded)
        assert a == b
        assert len(a) == 32

    def test_order_independent(self, entries):
        encoded = [build_per_episode_entry(e) for e in entries]
        a = compute_episodes_imt_root(encoded)
        b = compute_episodes_imt_root(list(reversed(encoded)))
        assert a == b

    def test_changing_one_entry_changes_root(self, entries):
        encoded = [build_per_episode_entry(e) for e in entries]
        a = compute_episodes_imt_root(encoded)
        modified = list(encoded)
        modified[3] = {**modified[3], "cost_q": [99, 99, 99]}
        b = compute_episodes_imt_root(modified)
        assert a != b

    def test_empty_returns_32_bytes(self):
        root = compute_episodes_imt_root([])
        assert len(root) == 32

    def test_duplicate_pair_rejected(self):
        encoded = [
            build_per_episode_entry(_entry("EP-1", "7")),
            build_per_episode_entry(_entry("EP-1", "7")),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            compute_episodes_imt_root(encoded)


# ---------- bundle build/parse/verify ----------


class TestBundleRoundTrip:
    def test_basic(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A",
            miner_hotkey=pk,
            submitted_round=12345,
            entries=entries,
            miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        assert decoded["v"] == EPISODE_BUNDLE_VERSION
        assert decoded["epoch_id"] == "EPOCH-A"
        assert decoded["miner_hotkey"] == pk
        assert len(decoded["entries"]) == len(entries)
        assert verify_bundle_inner_sig(decoded, pk)

    def test_canonical(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A",
            miner_hotkey=pk,
            submitted_round=12345,
            entries=entries,
            miner_signing_key=sk,
        )
        re_encoded = canonical_cbor_dumps(canonical_cbor_loads(blob))
        assert re_encoded == blob

    def test_root_matches_recomputation(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        root = compute_episodes_imt_root(decoded["entries"])
        assert verify_bundle_root(decoded, expected_root=root)

    def test_root_mismatch(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        bogus = b"\x00" * 32
        assert not verify_bundle_root(decoded, expected_root=bogus)

    def test_inner_sig_rejects_other_hotkey(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        wrong = b"\x99" * 32
        assert not verify_bundle_inner_sig(decoded, wrong)

    def test_empty_entries_rejected(self, signer):
        sk, pk = signer
        with pytest.raises(ValueError, match="non-empty"):
            build_per_episode_bundle(
                epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
                entries=[], miner_signing_key=sk,
            )

    def test_invalid_epoch_id_rejected(self, signer, entries):
        sk, pk = signer
        with pytest.raises(ValueError, match="epoch_id"):
            build_per_episode_bundle(
                epoch_id="lowercase", miner_hotkey=pk, submitted_round=1,
                entries=entries, miner_signing_key=sk,
            )


# ---------- IMT proofs ----------


class TestInclusionProof:
    def test_valid_proof(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        root = compute_episodes_imt_root(decoded["entries"])
        proof = prove_episode_inclusion(decoded, episode_id="EP-003", horizon="7")
        assert verify_episode_inclusion(proof, expected_root=root)

    def test_proof_against_wrong_root_rejected(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        proof = prove_episode_inclusion(decoded, episode_id="EP-003", horizon="7")
        bogus = b"\x00" * 32
        assert not verify_episode_inclusion(proof, expected_root=bogus)


class TestNonInclusionProof:
    def test_valid_non_inclusion(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        root = compute_episodes_imt_root(decoded["entries"])
        # EP-999 wasn't in the bundle.
        proof = prove_episode_non_inclusion(decoded, episode_id="EP-999", horizon="7")
        assert verify_episode_non_inclusion(proof, expected_root=root)

    def test_non_inclusion_for_present_entry_raises(self, signer, entries):
        sk, pk = signer
        blob = build_per_episode_bundle(
            epoch_id="EPOCH-A", miner_hotkey=pk, submitted_round=1,
            entries=entries, miner_signing_key=sk,
        )
        decoded = parse_per_episode_bundle(blob)
        with pytest.raises(KeyError):
            prove_episode_non_inclusion(decoded, episode_id="EP-003", horizon="7")
