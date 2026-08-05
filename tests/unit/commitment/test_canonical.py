"""Tests for canonical CBOR encoding + AAD construction."""

from __future__ import annotations

from hope.commitment.canonical import (
    aes_gcm_aad,
    canonical_cbor_dumps,
    canonical_cbor_loads,
    is_canonically_encoded,
)


class TestCanonicalCBOR:
    def test_roundtrip_simple_dict(self):
        obj = {"a": 1, "b": 2, "c": "hello"}
        data = canonical_cbor_dumps(obj)
        recovered = canonical_cbor_loads(data)
        assert recovered == obj

    def test_keys_are_sorted(self):
        # Different insertion order should produce identical bytes
        a = canonical_cbor_dumps({"z": 1, "a": 2, "m": 3})
        b = canonical_cbor_dumps({"a": 2, "m": 3, "z": 1})
        assert a == b

    def test_nested_keys_sorted(self):
        a = canonical_cbor_dumps({"outer": {"z": 1, "a": 2}})
        b = canonical_cbor_dumps({"outer": {"a": 2, "z": 1}})
        assert a == b

    def test_bytes_preserved(self):
        obj = {"hash": b"\xde\xad\xbe\xef"}
        data = canonical_cbor_dumps(obj)
        assert canonical_cbor_loads(data) == obj

    def test_integers_smallest_form(self):
        # Small integers must be encoded in smallest form
        # 0 → 1 byte; 23 → 1 byte; 255 → 2 bytes; 65535 → 3 bytes
        for n in [0, 1, 23, 24, 100, 255, 256, 65535, 65536]:
            data = canonical_cbor_dumps({"n": n})
            assert canonical_cbor_loads(data) == {"n": n}

    def test_empty_dict(self):
        data = canonical_cbor_dumps({})
        assert canonical_cbor_loads(data) == {}

    def test_is_canonically_encoded_accepts_canonical(self):
        obj = {"a": 1, "b": "two", "c": [1, 2, 3]}
        data = canonical_cbor_dumps(obj)
        assert is_canonically_encoded(data) is True

    def test_is_canonically_encoded_rejects_garbage(self):
        assert is_canonically_encoded(b"\xff\xff\xff\xff") is False
        assert is_canonically_encoded(b"") is False

    def test_deterministic_across_calls(self):
        obj = {"a": [1, 2, 3], "b": {"x": True, "y": None}}
        results = {canonical_cbor_dumps(obj) for _ in range(10)}
        assert len(results) == 1


class TestAESGCMAAD:
    def test_aad_format(self):
        aad = aes_gcm_aad("WR-2026-W18-PUB-E1")
        assert aad == b"sn21-prediction-v1:WR-2026-W18-PUB-E1"

    def test_aad_is_utf8(self):
        aad = aes_gcm_aad("test")
        assert isinstance(aad, bytes)
        # Must round-trip through UTF-8
        assert aad.decode("utf-8") == "sn21-prediction-v1:test"

    def test_different_epochs_yield_different_aads(self):
        a = aes_gcm_aad("EP-A")
        b = aes_gcm_aad("EP-B")
        assert a != b

    def test_aad_deterministic(self):
        a = aes_gcm_aad("WR-2026-W18-PUB-E1")
        b = aes_gcm_aad("WR-2026-W18-PUB-E1")
        assert a == b
