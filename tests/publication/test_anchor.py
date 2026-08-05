"""The chain committer for the rolling feed root.

The properties that matter are about SPEND and about SILENCE: it must not
write the same root twice, it must not record a write that did not land, and
it must never take the daily loop down when the chain is unreachable.
"""

import json
from pathlib import Path

import pytest

from hope.publication.anchor import (
    ROOT_BYTES,
    anchor_state_path,
    bittensor_committer,
    last_anchor,
    make_committer,
)

ROOT_A = bytes(range(32))
ROOT_B = bytes(range(32, 64))


class _Result:
    def __init__(self, success=True, block_number=7_000_000, message="ok"):
        self.success = success
        self.block_number = block_number
        self.message = message


def _recorder(result=None, results=None):
    """A fake submit that records every call it receives."""
    calls = []

    def submit(root_bytes):
        calls.append(root_bytes)
        if results is not None:
            return results[len(calls) - 1]
        return result if result is not None else _Result()

    return submit, calls


# ---- the happy path ---------------------------------------------------------

def test_commits_the_root_and_reports_the_block(tmp_path):
    submit, calls = _recorder()
    commit = make_committer(submit, str(tmp_path))

    out = commit(ROOT_A)

    assert calls == [ROOT_A]
    assert out["ok"] is True
    assert out["root"] == ROOT_A.hex()
    assert out["block"] == 7_000_000


def test_a_successful_commit_is_remembered(tmp_path):
    submit, _ = _recorder()
    make_committer(submit, str(tmp_path))(ROOT_A)

    memo = json.loads(Path(anchor_state_path(str(tmp_path))).read_text())
    assert memo["root"] == ROOT_A.hex() and memo["ok"] is True
    assert last_anchor(str(tmp_path))["root"] == ROOT_A.hex()


# ---- the spend guard --------------------------------------------------------

def test_the_same_root_is_never_committed_twice(tmp_path):
    """Re-running the loop on the same day must not spend a second write to
    say the identical thing — on chain that reads as a fresh anchor."""
    submit, calls = _recorder()
    commit = make_committer(submit, str(tmp_path))

    first = commit(ROOT_A)
    second = commit(ROOT_A)

    assert len(calls) == 1                       # one write, not two
    assert first["ok"] is True and "skipped" not in first
    assert second["ok"] is True and second["skipped"] == "root_unchanged"
    assert second["block"] == first["block"]     # reports the real anchor


def test_a_new_root_does_commit(tmp_path):
    submit, calls = _recorder()
    commit = make_committer(submit, str(tmp_path))

    commit(ROOT_A)
    out = commit(ROOT_B)

    assert calls == [ROOT_A, ROOT_B]
    assert out["ok"] is True and out["root"] == ROOT_B.hex()


def test_without_a_ledger_root_every_call_commits(tmp_path):
    """No memo, no dedupe — stated rather than silently assumed."""
    submit, calls = _recorder()
    commit = make_committer(submit, None)
    commit(ROOT_A)
    commit(ROOT_A)
    assert len(calls) == 2


# ---- failures stay loud, and stay retryable ---------------------------------

def test_a_failed_commit_is_not_recorded_so_the_next_run_retries(tmp_path):
    submit, calls = _recorder(results=[_Result(success=False, message="no space"),
                                       _Result(success=True)])
    commit = make_committer(submit, str(tmp_path))

    first = commit(ROOT_A)
    assert first["ok"] is False and first["message"] == "no space"
    assert last_anchor(str(tmp_path)) is None       # nothing remembered

    second = commit(ROOT_A)
    assert len(calls) == 2                          # retried, not skipped
    assert second["ok"] is True


def test_an_unreachable_chain_never_raises_into_the_loop(tmp_path):
    def submit(_root):
        raise ConnectionError("websocket closed")

    out = make_committer(submit, str(tmp_path))(ROOT_A)

    assert out["ok"] is False
    assert "ConnectionError" in out["error"]
    assert last_anchor(str(tmp_path)) is None


def test_an_unreadable_memo_re_commits_rather_than_assuming_anchored(tmp_path):
    """A corrupt memo must not be read as 'already done' — that would stop
    anchoring silently, which is the failure this whole feature exists to
    prevent."""
    Path(anchor_state_path(str(tmp_path))).write_text("{not json")
    submit, calls = _recorder()

    out = make_committer(submit, str(tmp_path))(ROOT_A)

    assert len(calls) == 1 and out["ok"] is True


# ---- what must never reach the chain ----------------------------------------

@pytest.mark.parametrize("bad", [b"", b"\x00" * 31, b"\x00" * 33, None])
def test_only_a_32_byte_root_is_ever_committed(tmp_path, bad):
    submit, calls = _recorder()
    out = make_committer(submit, str(tmp_path))(bad)

    assert calls == []                              # nothing reached the chain
    assert out["ok"] is False and out["reason"] == "root_not_32_bytes"
    assert ROOT_BYTES == 32


# ---- the production wiring --------------------------------------------------

def test_bittensor_committer_submits_a_sha256_commit(tmp_path, monkeypatch):
    """Pin the production path to the real helper's call shape, so a change
    to `submit_sha256_commit` surfaces here rather than on the day we anchor."""
    seen = {}

    def fake_submit(subtensor, wallet, netuid, root_bytes, **kw):
        seen.update(subtensor=subtensor, wallet=wallet, netuid=netuid,
                    root=root_bytes, kw=kw)
        return _Result()

    monkeypatch.setattr("hope.commitment.on_chain.submit_sha256_commit", fake_submit)

    commit = bittensor_committer("SUB", "WALLET", 21, str(tmp_path))
    out = commit(ROOT_A)

    assert out["ok"] is True
    assert seen["netuid"] == 21 and seen["root"] == ROOT_A
    assert seen["kw"]["raise_error"] is False       # returns, never raises
