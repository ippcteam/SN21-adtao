"""The bulk commitment read is an optimisation, and must fail to empty rather
than to wrong — a commitment it drops is a miner it silently denies."""

import binascii

from hope.backtest.chain_commitments import (
    as_registry_reader,
    as_single_reader,
    block_of,
    bulk_model_commitments,
    model_string_in,
)

DIGEST = "sha256:" + "a" * 64
COMMIT = f"sn21-model:v1:ghcr.io/someone/sn21-mv@{DIGEST}"


def _record(text, block=100):
    hexed = "0x" + binascii.hexlify(text.encode()).decode()
    return {"deposit": 0, "block": block,
            "info": {"fields": [{"Raw64": hexed}]}}


class _Value:
    def __init__(self, value):
        self.value = value


class _Substrate:
    def __init__(self, rows, raises=False):
        self._rows = rows
        self._raises = raises

    def query_map(self, module, storage_function, params, page_size):
        if self._raises:
            raise RuntimeError("endpoint down")
        return iter(self._rows)


class _Subtensor:
    def __init__(self, substrate):
        self.substrate = substrate


def test_extracts_the_model_string():
    assert model_string_in(_record(COMMIT)) == COMMIT


def test_ignores_a_commitment_that_is_not_ours():
    assert model_string_in(_record("someone-else:v3:whatever")) is None


def test_block_is_read_when_present():
    assert block_of(_record(COMMIT, block=8815342)) == 8815342
    assert block_of({"no": "block"}) is None


def test_only_currently_registered_hotkeys_are_returned():
    """The map retains entries for deregistered hotkeys; reviving one into a
    runnable set would run a model for somebody who has left."""
    rows = [("hk_live", _Value(_record(COMMIT))),
            ("hk_gone", _Value(_record(COMMIT)))]
    out = bulk_model_commitments(_Subtensor(_Substrate(rows)), 21, ["hk_live"])
    assert list(out) == ["hk_live"]


def test_rpc_failure_returns_empty_so_the_caller_falls_back():
    out = bulk_model_commitments(_Subtensor(_Substrate([], raises=True)),
                                 21, ["hk"])
    assert out == {}


def test_hotkey_without_a_model_commitment_is_absent():
    rows = [("hk", _Value(_record("reg-v1:something")))]
    assert bulk_model_commitments(_Subtensor(_Substrate(rows)), 21, ["hk"]) == {}


def test_registry_reader_shape():
    """build_registry expects [(block, raw)]; the pallet keeps one entry."""
    read = as_registry_reader({"hk": (99, COMMIT)})
    assert read("hk") == [(99, COMMIT)]
    assert read("absent") == []


def test_single_reader_shape():
    read = as_single_reader({"hk": (99, COMMIT)})
    assert read("hk") == COMMIT
    assert read("absent") is None
