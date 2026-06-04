"""Tests for the on-chain outcome commitment protocol (P4b)."""
from hope.commitment.outcome_commitment import (
    OUTCOME_COMMIT_PREFIX,
    outcomes_digest,
    build_outcome_commitment,
    parse_outcome_commitment,
    verify_package_against_commitment,
)

EPOCH = "WR-2026-W22-PUB-E1"


def _pkg(outcomes):
    """package shape: episodes[].{episode_id, validator_only_outcomes}."""
    return {"episodes": [{"episode_id": eid, "validator_only_outcomes": v}
                         for eid, v in outcomes]}


PKG = _pkg([("ep-b", {"t7": 100, "t14": 200}), ("ep-a", {"t7": 5, "t14": 9})])


def test_digest_is_deterministic_and_order_independent():
    # same logical content in a different episode order -> same digest (sorted)
    reordered = _pkg([("ep-a", {"t7": 5, "t14": 9}), ("ep-b", {"t7": 100, "t14": 200})])
    assert outcomes_digest(EPOCH, PKG) == outcomes_digest(EPOCH, reordered)
    assert len(outcomes_digest(EPOCH, PKG)) == 32


def test_digest_binds_epoch_id():
    assert outcomes_digest(EPOCH, PKG) != outcomes_digest("WR-2026-W21-PUB-E1", PKG)


def test_digest_changes_when_outcomes_change():
    tampered = _pkg([("ep-b", {"t7": 999, "t14": 200}), ("ep-a", {"t7": 5, "t14": 9})])
    assert outcomes_digest(EPOCH, PKG) != outcomes_digest(EPOCH, tampered)


def test_build_parse_roundtrip():
    d = outcomes_digest(EPOCH, PKG)
    raw = build_outcome_commitment(EPOCH, d)
    assert raw.startswith(OUTCOME_COMMIT_PREFIX)
    epoch, digest = parse_outcome_commitment(raw)
    assert epoch == EPOCH and digest == d


def test_parse_rejects_foreign_or_short():
    assert parse_outcome_commitment(b"something-else") is None
    assert parse_outcome_commitment(OUTCOME_COMMIT_PREFIX + b"\x00" * 8) is None  # < 32


def test_verify_match():
    raw = build_outcome_commitment(EPOCH, outcomes_digest(EPOCH, PKG))
    assert verify_package_against_commitment(EPOCH, PKG, raw) is True


def test_verify_rejects_tampered_package():
    raw = build_outcome_commitment(EPOCH, outcomes_digest(EPOCH, PKG))
    tampered = _pkg([("ep-b", {"t7": 999, "t14": 200}), ("ep-a", {"t7": 5, "t14": 9})])
    assert verify_package_against_commitment(EPOCH, tampered, raw) is False


def test_verify_rejects_replay_to_other_epoch():
    # a commitment built for EPOCH must not verify for a different epoch's claim
    raw = build_outcome_commitment(EPOCH, outcomes_digest(EPOCH, PKG))
    assert verify_package_against_commitment("WR-2026-W21-PUB-E1", PKG, raw) is False


def test_verify_rejects_foreign_commitment():
    assert verify_package_against_commitment(EPOCH, PKG, b"not-a-commitment") is False


class _Field:
    def __init__(self, b):
        self.bytes_ = b


def test_read_and_verify_on_chain(monkeypatch):
    import hope.commitment.chain_reader as cr
    from hope.commitment import outcome_commitment as oc
    raw = build_outcome_commitment(EPOCH, outcomes_digest(EPOCH, PKG))
    monkeypatch.setattr(cr, "read_commitment_of", lambda *a, **k: [_Field(b"junk"), _Field(raw)])
    ok, reason = oc.verify_outcome_commitment(None, 21, "5Signer", EPOCH, PKG)
    assert ok is True and reason == "ok"
    tampered = _pkg([("ep-a", {"t7": 5}), ("ep-b", {"t7": 999})])
    ok2, _ = oc.verify_outcome_commitment(None, 21, "5Signer", EPOCH, tampered)
    assert ok2 is False


def test_verify_on_chain_missing_commitment(monkeypatch):
    import hope.commitment.chain_reader as cr
    from hope.commitment import outcome_commitment as oc
    monkeypatch.setattr(cr, "read_commitment_of", lambda *a, **k: None)
    ok, reason = oc.verify_outcome_commitment(None, 21, "5Signer", EPOCH, PKG)
    assert ok is False and "no sn21-outcomes-v1" in reason
