"""Tests for the fast head-only reg-index refresh (scripts/refresh_reg_index_head.py).

Exercises the guarantees that matter operationally — a valid current-commitment
registration is ADDED, an existing entry is never DROPPED even when its hotkey
no longer carries a current reg-v1, and the same signature/role gate the chain
uses rejects wrong-role and bad-signature commitments — all without a live chain.
"""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.chain_reader import RawCommitField
from hope.commitment.registration import (
    RegistrationRole,
    build_registration_payload,
)
import scripts.refresh_reg_index_head as mod


def _keypair(uri: str):
    from bittensor_wallet.bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _reg_field(kp, role: RegistrationRole, *, corrupt: bool = False) -> RawCommitField:
    payload = build_registration_payload(
        role=role, ss58_pubkey=kp.public_key,
        ed25519_signing_key=Ed25519PrivateKey.generate(),
    )
    if corrupt:  # flip a signature byte so verify_registration fails
        payload = payload[:-1] + bytes([payload[-1] ^ 0xFF])
    return RawCommitField(variant="Raw109", bytes_=payload)


class _StubMetagraph:
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys


class _StubSubtensor:
    def __init__(self, head, hotkeys):
        self._head = head
        self._hotkeys = hotkeys

    def get_current_block(self):
        return self._head

    def metagraph(self, netuid):
        return _StubMetagraph(self._hotkeys)


def _patch_chain(monkeypatch, head, commitments: dict):
    """commitments maps ss58 -> list[RawCommitField] (or None)."""
    hotkeys = list(commitments.keys())
    monkeypatch.setattr(mod, "make_subtensor",
                        lambda network: _StubSubtensor(head, hotkeys))
    monkeypatch.setattr(mod, "read_commitment_of",
                        lambda sub, netuid, hk: commitments.get(hk))


def test_adds_valid_current_commitment(tmp_path, monkeypatch):
    kp = _keypair("//MinerOne")
    _patch_chain(monkeypatch, head=8_400_000,
                 commitments={kp.ss58_address: [_reg_field(kp, RegistrationRole.MINER)]})
    path = str(tmp_path / "reg-index.json")

    before, after = mod.refresh(path, "finney", 21, RegistrationRole.MINER)

    assert (before, after) == (0, 1)
    data = json.load(open(path))
    assert len(data) == 1
    assert data[0]["hotkey_ss58"] == kp.ss58_address
    assert data[0]["block_number"] == 8_400_000


def test_preserves_existing_never_drops(tmp_path, monkeypatch):
    # An established miner whose CURRENT commitment is no longer reg-v1 (they
    # overwrote it with a prediction commit) must survive the refresh.
    established = {
        "hotkey_ss58": "5Established",
        "hotkey_pk_hex": "aa" * 32,
        "ed25519_pk_hex": "bb" * 32,
        "role": "M",
        "block_number": 8_300_000,
    }
    path = str(tmp_path / "reg-index.json")
    with open(path, "w") as f:
        json.dump([established], f)

    fresh = _keypair("//MinerTwo")
    _patch_chain(monkeypatch, head=8_400_000, commitments={
        fresh.ss58_address: [_reg_field(fresh, RegistrationRole.MINER)],
        "5Established": [RawCommitField(variant="Sha256", bytes_=b"\x01" * 32)],
    })

    before, after = mod.refresh(path, "finney", 21, RegistrationRole.MINER)

    assert (before, after) == (1, 2)
    keys = {e["hotkey_ss58"] for e in json.load(open(path))}
    assert "5Established" in keys and fresh.ss58_address in keys


def test_skips_wrong_role(tmp_path, monkeypatch):
    kp = _keypair("//AValidator")
    _patch_chain(monkeypatch, head=8_400_000, commitments={
        kp.ss58_address: [_reg_field(kp, RegistrationRole.VALIDATOR)],
    })
    path = str(tmp_path / "reg-index.json")

    _, after = mod.refresh(path, "finney", 21, RegistrationRole.MINER)

    assert after == 0  # validator-role commit not added to a miner index


def test_skips_bad_signature(tmp_path, monkeypatch):
    kp = _keypair("//Forger")
    _patch_chain(monkeypatch, head=8_400_000, commitments={
        kp.ss58_address: [_reg_field(kp, RegistrationRole.MINER, corrupt=True)],
    })
    path = str(tmp_path / "reg-index.json")

    _, after = mod.refresh(path, "finney", 21, RegistrationRole.MINER)

    assert after == 0  # signature does not verify -> rejected
