"""Regression tests for chain_reader across async-substrate-interface 1.x/2.x.

A mainnet runtime upgrade (Jul 2026) forced bittensor >=10.5, which ships
async-substrate-interface 2.x. That changed the `substrate.query` result
shape for `Commitments` byte payloads:

  - 1.x (bittensor 10.2.x): byte payloads are bytes / tuple-of-ints;
    CommitmentOf `info.fields` is tuple-of-tuple-of-{variant: payload}.
  - 2.x: byte payloads are `0x`-hex strings;
    CommitmentOf `info.fields` is a list-of-{variant: payload} dicts.

`chain_reader` must decode BOTH so a rollback (or mixed fleet) stays safe.
These tests pin the normaliser + both readers against both shapes with no
network.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hope.commitment.chain_reader import (
    _scale_payload_to_bytes,
    read_commitment_of,
    read_revealed_commitments,
)


class TestScalePayloadToBytes:
    def test_2x_hex_string_with_prefix(self):
        assert _scale_payload_to_bytes("0xdeadbeef") == bytes.fromhex("deadbeef")

    def test_2x_hex_string_without_prefix(self):
        assert _scale_payload_to_bytes("cafebabe") == bytes.fromhex("cafebabe")

    def test_1x_bytes(self):
        assert _scale_payload_to_bytes(b"\x01\x02\x03") == b"\x01\x02\x03"

    def test_1x_tuple_of_ints(self):
        assert _scale_payload_to_bytes((1, 2, 3)) == b"\x01\x02\x03"

    def test_1x_nested_one_tuple_wrap(self):
        assert _scale_payload_to_bytes(((1, 2, 3),)) == b"\x01\x02\x03"

    def test_empty_tuple_is_empty_bytes(self):
        assert _scale_payload_to_bytes(()) == b""

    def test_dict_variant_returns_none(self):
        # TimelockEncrypted etc. — no directly-accessible bytes.
        assert _scale_payload_to_bytes({"encrypted": "0xaa", "reveal_round": 5}) is None

    def test_non_hex_string_returns_none(self):
        assert _scale_payload_to_bytes("not-hex-zz") is None


def _mock_query_result(value):
    r = MagicMock()
    r.value = value
    return r


class TestReadRevealedCommitmentsShapes:
    def test_2x_hex_tuple_shape(self):
        # 2.x: list of (0x-hex-string, block_int)
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result([
            ("0xdeadbeef", 12345),
            ("0xcafebabe", 12346),
        ])
        out = read_revealed_commitments(st, netuid=21, hotkey_ss58="5X")
        assert [(e.block_number, e.payload_bytes) for e in out] == [
            (12345, bytes.fromhex("deadbeef")),
            (12346, bytes.fromhex("cafebabe")),
        ]

    def test_1x_bytes_tuple_shape(self):
        # 1.x: list of (bytes, block_int)
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result([(b"\x01\x02\x03", 100)])
        out = read_revealed_commitments(st, netuid=21, hotkey_ss58="5X")
        assert len(out) == 1
        assert out[0].block_number == 100 and out[0].payload_bytes == b"\x01\x02\x03"

    def test_empty_returns_empty_list(self):
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result(None)
        assert read_revealed_commitments(st, netuid=21, hotkey_ss58="5X") == []


class TestReadCommitmentOfShapes:
    def test_2x_list_of_dicts_hex_payload(self):
        # 2.x: info.fields = [ {variant: 0x-hex} ]
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result(
            {"info": {"fields": [{"Raw4": "0x736e3231"}]}}
        )
        out = read_commitment_of(st, netuid=21, hotkey_ss58="5X")
        assert len(out) == 1
        assert out[0].variant == "Raw4"
        assert out[0].bytes_ == b"sn21"

    def test_1x_tuple_of_tuple_of_dict_bytes_payload(self):
        # 1.x: info.fields = ( ( {variant: bytes} , ) , )
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result(
            {"info": {"fields": (({"Raw4": (115, 110, 50, 49)},),)}}
        )
        out = read_commitment_of(st, netuid=21, hotkey_ss58="5X")
        assert len(out) == 1
        assert out[0].variant == "Raw4" and out[0].bytes_ == b"sn21"

    def test_none_when_no_commit(self):
        st = MagicMock()
        st.substrate.query.return_value = _mock_query_result(None)
        assert read_commitment_of(st, netuid=21, hotkey_ss58="5X") is None
