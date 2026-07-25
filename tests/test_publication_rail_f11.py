"""D11 rail + accuracy feed — determinism, chain, attestation, tamper."""
from datetime import date

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.publication.rail import (
    AttestedDocument, attest, build_document, canonical_bytes, chain_ok,
    document_sha256, verify,
)
from hope.publication.accuracy_feed import build_accuracy_metrics
from hope.scoring.daily_score_flow import HorizonResult

KEY = Ed25519PrivateKey.generate()
D = date(2026, 9, 1)


def _doc(prev=None, x=1):
    return build_document("test-feed", "2026-09-01", {"x": x},
                          "2026-09-01T09:00:00+00:00", prev)


class TestCanonical:
    def test_deterministic_and_order_insensitive(self):
        a = {"b": 1, "a": [2, 3]}
        b = {"a": [2, 3], "b": 1}
        assert canonical_bytes(a) == canonical_bytes(b)
        assert document_sha256(a) == document_sha256(b)


class TestAttestation:
    def test_roundtrip(self):
        att = attest(_doc(), KEY)
        assert verify(att) is True
        assert att.anchor_digest == att.sha256

    def test_tampered_document_fails(self):
        att = attest(_doc(), KEY)
        bad = AttestedDocument(document={**att.document, "metrics": {"x": 999}},
                               sha256=att.sha256, signature_hex=att.signature_hex,
                               public_key_hex=att.public_key_hex)
        assert verify(bad) is False

    def test_wrong_key_fails(self):
        att = attest(_doc(), KEY)
        other = attest(_doc(), Ed25519PrivateKey.generate())
        forged = AttestedDocument(document=att.document, sha256=att.sha256,
                                  signature_hex=other.signature_hex,
                                  public_key_hex=att.public_key_hex)
        assert verify(forged) is False


class TestChain:
    def test_valid_chain(self):
        d1 = _doc(None, 1)
        d2 = _doc(document_sha256(d1), 2)
        d3 = _doc(document_sha256(d2), 3)
        assert chain_ok([d1, d2, d3]) is True

    def test_broken_chain(self):
        d1 = _doc(None, 1)
        d2 = _doc("deadbeef" * 8, 2)
        assert chain_ok([d1, d2]) is False


class TestAccuracyFeed:
    def test_aggregation(self):
        rs = [
            HorizonResult("e1", 7, "m1", 0.8, D),
            HorizonResult("e1", 7, "m2", 0.6, D),
            HorizonResult("e2", 14, "m1", 0.9, D),
        ]
        m = build_accuracy_metrics(rs)
        assert m["episodes_finalised"] == 2
        assert m["miners_scored"] == 2
        assert m["results_total"] == 3
        assert m["horizons"]["7"]["scored"] == 2
        assert m["horizons"]["7"]["mean_score"] == 0.7
        assert m["horizons"]["14"]["max_score"] == 0.9

    def test_no_miner_identities_in_feed(self):
        m = build_accuracy_metrics([HorizonResult("e1", 7, "hotkey-abc", 0.8, D)])
        assert "hotkey-abc" not in str(m)
