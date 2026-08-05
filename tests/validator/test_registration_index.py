"""Unit tests for hope/validator/registration_index.py.

These cover the in-memory index logic with a fake substrate that emits
synthetic registration events. Live-chain testing is exercised separately
by scripts/diag/probe_registration_index.py against testnet 466.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.chain_reader import CommitEvent, RawCommitField
from hope.commitment.registration import (
    REG_V1_PREFIX,
    RegistrationRole,
    build_registration_payload,
)
from hope.validator.registration_index import (
    RegistrationIndex,
)

# ---------------------------------------------------------------------------
# Test fixtures: fake subtensor + monkey-patched chain_reader helpers
# ---------------------------------------------------------------------------


@dataclass
class _PinnedCommit:
    """A registration commit pinned to a specific block_hash."""

    block_number: int
    block_hash: str
    hotkey_ss58: str
    fields: list[RawCommitField]


class _FakeSubtensor:
    """Stub that satisfies the chain_reader contract used by RegistrationIndex."""

    def __init__(self, *, current_block: int = 1000):
        self._current_block = current_block
        self.substrate = self  # both attrs handled here

    def get_current_block(self) -> int:
        return self._current_block

    def get_block_hash(self, block_number: int) -> str:
        return f"0x{block_number:064x}"


@pytest.fixture
def hotkey_kp_pair():
    """A miner SS58 + the 32-byte raw pubkey decoded from it."""
    # Use a deterministic well-known testnet SS58 so callers can sanity-check
    # the encode/decode round trip independently.
    from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
    miner_signing = Ed25519PrivateKey.generate()
    miner_pk = miner_signing.public_key().public_bytes_raw()
    kp = Keypair(public_key="0x" + miner_pk.hex(), ss58_format=42)
    return kp.ss58_address, bytes.fromhex(kp.public_key.removeprefix("0x")) if isinstance(kp.public_key, str) else bytes(kp.public_key)


def _build_event(hotkey_ss58: str, netuid: int = 466, name: str = "CommitmentSet") -> CommitEvent:
    return CommitEvent(
        block_number=0,
        block_hash="",
        netuid=netuid,
        hotkey_ss58=hotkey_ss58,
        event_name=name,
        raw={},
    )


def _install_fakes(
    monkeypatch,
    *,
    events_by_block: dict[int, list[CommitEvent]],
    commits_by_pin: dict[tuple[int, str], list[RawCommitField]],
):
    """Patch chain_reader helpers used by the index with deterministic stubs."""
    import hope.validator.registration_index as mod

    def fake_read_events(subtensor, block_hash: str, *, module_filter=None):
        # block_hash matches our get_block_hash format so we can recover the block number.
        try:
            block_num = int(block_hash, 16)
        except ValueError:
            return []
        return events_by_block.get(block_num, [])

    def fake_read_commitment_of(subtensor, netuid, hotkey_ss58, *, block_hash=None):
        try:
            block_num = int(block_hash, 16)
        except (ValueError, TypeError):
            return None
        return commits_by_pin.get((block_num, hotkey_ss58))

    monkeypatch.setattr(mod, "read_events_at_block", fake_read_events)
    monkeypatch.setattr(mod, "read_commitment_of", fake_read_commitment_of)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_finds_single_valid_registration(monkeypatch, hotkey_kp_pair):
    """A correctly-built registration at block N is indexed after scan_range(N, N)."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    inner_sk = Ed25519PrivateKey.generate()
    payload = build_registration_payload(
        role=RegistrationRole.MINER,
        ss58_pubkey=hotkey_pk,
        ed25519_signing_key=inner_sk,
    )

    events_by_block = {
        100: [_build_event(hotkey_ss58)],
    }
    commits_by_pin = {
        (100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=payload)],
    }
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    found = index.scan_range(100, 100)

    assert found == 1
    assert index.size == 1
    ed25519_pk = index.lookup(hotkey_pk)
    assert ed25519_pk == inner_sk.public_key().public_bytes_raw()


def test_lookup_returns_none_when_no_registration(monkeypatch, hotkey_kp_pair):
    """Hotkeys without a verified registration return None."""
    _hotkey_ss58, hotkey_pk = hotkey_kp_pair
    _install_fakes(monkeypatch, events_by_block={}, commits_by_pin={})

    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    index.scan_range(100, 200)
    assert index.lookup(hotkey_pk) is None
    assert index.lookup(b"\x00" * 32) is None


def test_later_block_overrides_earlier(monkeypatch, hotkey_kp_pair):
    """If a hotkey re-registers at a higher block, the latest binding wins."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    sk_a = Ed25519PrivateKey.generate()
    sk_b = Ed25519PrivateKey.generate()
    pay_a = build_registration_payload(role=RegistrationRole.MINER, ss58_pubkey=hotkey_pk, ed25519_signing_key=sk_a)
    pay_b = build_registration_payload(role=RegistrationRole.MINER, ss58_pubkey=hotkey_pk, ed25519_signing_key=sk_b)

    events_by_block = {
        100: [_build_event(hotkey_ss58)],
        200: [_build_event(hotkey_ss58)],
    }
    commits_by_pin = {
        (100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=pay_a)],
        (200, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=pay_b)],
    }
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    index.scan_range(50, 250)

    # Newer one wins regardless of scan direction.
    assert index.lookup(hotkey_pk) == sk_b.public_key().public_bytes_raw()


def test_role_mismatch_rejected(monkeypatch, hotkey_kp_pair):
    """A VALIDATOR-role registration is NOT indexed by a MINER-role scanner."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    sk = Ed25519PrivateKey.generate()
    payload = build_registration_payload(
        role=RegistrationRole.VALIDATOR,
        ss58_pubkey=hotkey_pk,
        ed25519_signing_key=sk,
    )

    events_by_block = {100: [_build_event(hotkey_ss58)]}
    commits_by_pin = {(100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=payload)]}
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    index = RegistrationIndex(
        _FakeSubtensor(), netuid=466,
        expected_role=RegistrationRole.MINER,
    )
    found = index.scan_range(100, 100)

    assert found == 0
    assert index.lookup(hotkey_pk) is None


def test_invalid_signature_rejected(monkeypatch, hotkey_kp_pair):
    """Forged registration (sig over wrong ed25519_pk) is rejected by verify_registration."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    sk_a = Ed25519PrivateKey.generate()
    sk_b = Ed25519PrivateKey.generate()

    # Build a payload where the sig is from sk_a but the embedded pubkey is sk_b's.
    # We splice the bytes directly because build_registration_payload always
    # builds an internally-consistent payload.
    real_pay = build_registration_payload(
        role=RegistrationRole.MINER, ss58_pubkey=hotkey_pk, ed25519_signing_key=sk_a,
    )
    real_pk = sk_a.public_key().public_bytes_raw()
    swapped_pk = sk_b.public_key().public_bytes_raw()
    assert real_pay[len(REG_V1_PREFIX) + 1: len(REG_V1_PREFIX) + 1 + 32] == real_pk
    forged = (
        real_pay[: len(REG_V1_PREFIX) + 1]
        + swapped_pk
        + real_pay[len(REG_V1_PREFIX) + 1 + 32:]
    )

    events_by_block = {100: [_build_event(hotkey_ss58)]}
    commits_by_pin = {(100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=forged)]}
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    found = index.scan_range(100, 100)
    assert found == 0
    assert index.lookup(hotkey_pk) is None


def test_wrong_netuid_skipped(monkeypatch, hotkey_kp_pair):
    """Events under a different netuid are ignored even if payload is valid."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    sk = Ed25519PrivateKey.generate()
    payload = build_registration_payload(
        role=RegistrationRole.MINER, ss58_pubkey=hotkey_pk, ed25519_signing_key=sk,
    )

    events_by_block = {
        100: [_build_event(hotkey_ss58, netuid=999)],  # wrong netuid
    }
    commits_by_pin = {(100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=payload)]}
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    found = index.scan_range(100, 100)
    assert found == 0


def test_empty_range_noop():
    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    assert index.scan_range(200, 100) == 0
    assert index.size == 0


def test_to_json_round_trip(monkeypatch, hotkey_kp_pair):
    """An indexed registration survives serialise → deserialise."""
    hotkey_ss58, hotkey_pk = hotkey_kp_pair
    sk = Ed25519PrivateKey.generate()
    payload = build_registration_payload(
        role=RegistrationRole.MINER, ss58_pubkey=hotkey_pk, ed25519_signing_key=sk,
    )

    events_by_block = {100: [_build_event(hotkey_ss58)]}
    commits_by_pin = {(100, hotkey_ss58): [RawCommitField(variant="Raw109", bytes_=payload)]}
    _install_fakes(monkeypatch, events_by_block=events_by_block, commits_by_pin=commits_by_pin)

    src = RegistrationIndex(_FakeSubtensor(), netuid=466)
    src.scan_range(100, 100)
    dumped = src.to_json()
    assert len(dumped) == 1

    dst = RegistrationIndex(_FakeSubtensor(), netuid=466)
    merged = dst.merge_json(dumped)
    assert merged == 1
    assert dst.lookup(hotkey_pk) == sk.public_key().public_bytes_raw()


def test_merge_json_skips_older_entries():
    """Loading a prebuilt entry that's older than what we have is a no-op."""
    # Seed an index with a hand-crafted entry at block 200.
    pk = b"\x11" * 32
    ed_a = b"\x22" * 32
    ed_b = b"\x33" * 32
    seed = [{
        "hotkey_ss58": "5SeedPlaceholder",
        "hotkey_pk_hex": pk.hex(),
        "ed25519_pk_hex": ed_a.hex(),
        "role": "M",
        "block_number": 200,
    }]
    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    assert index.merge_json(seed) == 1
    assert index.lookup(pk) == ed_a

    # Now attempt to merge an OLDER entry for the same hotkey.
    older = [{
        "hotkey_ss58": "5SeedPlaceholder",
        "hotkey_pk_hex": pk.hex(),
        "ed25519_pk_hex": ed_b.hex(),
        "role": "M",
        "block_number": 100,
    }]
    assert index.merge_json(older) == 0
    assert index.lookup(pk) == ed_a  # unchanged


def test_merge_json_ignores_malformed_entries():
    """Bad JSON entries (wrong length, bad hex, missing keys) are silently dropped."""
    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    bad = [
        {"hotkey_ss58": "x", "hotkey_pk_hex": "abc", "ed25519_pk_hex": "abc", "role": "M", "block_number": 1},
        {"hotkey_ss58": "x"},  # missing keys
        {"hotkey_ss58": "x", "hotkey_pk_hex": "nothex", "ed25519_pk_hex": "00" * 32, "role": "M", "block_number": 1},
    ]
    assert index.merge_json(bad) == 0
    assert index.size == 0


# ---------------------------------------------------------------------------
# Silent-gap regression: a block that fails to read must STOP the pass, never
# be skipped while the checkpoint advances past it (the freeze bug that dropped
# real miners forever when the flaky archive timed out mid-scan).
# ---------------------------------------------------------------------------


class _FlakyHashSubtensor(_FakeSubtensor):
    """get_block_hash raises on one block (simulates a dropped archive socket)."""

    def __init__(self, *, fail_block: int, **kw):
        super().__init__(**kw)
        self._fail_block = fail_block

    def get_block_hash(self, block_number: int) -> str:
        if block_number == self._fail_block:
            raise ConnectionError("websocket keepalive ping timeout")
        return f"0x{block_number:064x}"


def test_unreadable_block_stops_pass_without_advancing_checkpoint(monkeypatch):
    # Block 102 is unreadable. The scan must stop at 101 — NOT skip 102 and run
    # on to 105, which would advance last_scanned_block past an unread block and
    # drop its registrations permanently. (reconnect_network=None → no reconnect,
    # so the bounded retries all fail and the pass breaks.)
    _install_fakes(monkeypatch, events_by_block={}, commits_by_pin={})
    index = RegistrationIndex(_FlakyHashSubtensor(fail_block=102), netuid=466)
    index.scan_range(100, 105)
    assert index.last_scanned_block == 101  # last contiguous block, no gap past 102


def test_events_read_failure_also_stops_without_gap(monkeypatch):
    # Same guarantee when get_block_hash succeeds but events read fails.
    import hope.validator.registration_index as mod

    def flaky_read_events(subtensor, block_hash, *, module_filter=None):
        if int(block_hash, 16) == 103:
            raise TimeoutError("archive timed out closing connection")
        return []

    monkeypatch.setattr(mod, "read_events_at_block", flaky_read_events)
    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    index.scan_range(100, 110)
    assert index.last_scanned_block == 102  # stopped at the block before the failure


def test_clean_scan_still_advances_to_end(monkeypatch):
    # Happy path unchanged: a fully-readable range advances the checkpoint to end.
    _install_fakes(monkeypatch, events_by_block={}, commits_by_pin={})
    index = RegistrationIndex(_FakeSubtensor(), netuid=466)
    index.scan_range(100, 105)
    assert index.last_scanned_block == 105
