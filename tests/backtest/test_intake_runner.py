"""Model intake, end to end, with the chain and docker injected.

Context for why this file exists at all: the registry parser, the
digest-pinned pull, the gate and the verdict rail were all built and tested,
and NOTHING CALLED THEM. `build_registry`, `intake_all` and `gate_submission`
had no production caller outside tests, so the quickstart's promise that the
subnet pulls and gate-admits a committed model had nothing behind it. These
tests cover the piece that closes that, and they lean on the two properties
that decide whether a stranger's container can earn money: only new digests
are ever run, and only a model that passed the GATE counts as admitted.
"""

import json
import os

import pytest

from hope.backtest.image_intake import (
    STATUS_ADMITTED,
    STATUS_PULL_FAILED,
    STATUS_REJECTED_DIGEST_MISMATCH,
    STATUS_REJECTED_GATE,
)
from hope.backtest.intake_runner import (
    commitments_from_chain,
    load_admitted,
    run_intake,
    verdict_dir,
    verdicted_digests,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REPO = "ghcr.io/someone/sn21-miner"


def _commit(digest, repo=REPO):
    return f"sn21-model:v1:{repo}@{digest}"


def _write_verdict(root, name, digest):
    d = verdict_dir(root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.json"), "w") as f:
        json.dump({"document": {"metrics": {"image_digest": digest}}}, f)


def _ok_pull(_ref):
    return True, ""


def _inspector_for(digest):
    return lambda ref: [f"{REPO}@{digest}"]


# ---- reading commitments off chain -----------------------------------------

def test_only_well_formed_model_commitments_become_submissions():
    chain = {
        "hkGood": _commit(DIGEST_A),
        "hkNotOurs": "some-other-subnet:v1:whatever",
        "hkGarbage": "sn21-model:v1:NOT A DIGEST",
        "hkSilent": None,
    }
    commitments, unparseable = commitments_from_chain(chain, chain.get)

    assert [c.hotkey for c in commitments] == ["hkGood"]
    assert commitments[0].digest == DIGEST_A
    # "not ours" and "silent" are not submissions at all; only the malformed
    # sn21 commitment counts as something a miner tried and got wrong.
    assert unparseable == 1


def test_digest_only_commitments_are_not_pullable_submissions():
    """Legacy digest-only form carries no repository, so there is nowhere to
    pull from. Counting it as a submission would mean gating something we
    cannot fetch."""
    chain = {"hk": f"sn21-model:v1:{DIGEST_A}"}
    commitments, unparseable = commitments_from_chain(chain, chain.get)
    assert commitments == [] and unparseable == 1


def test_a_hotkey_with_no_commitment_is_silently_absent():
    chain = {"hk": None}
    commitments, unparseable = commitments_from_chain(chain, chain.get)
    assert commitments == [] and unparseable == 0


# ---- the expensive step runs only when it must -----------------------------

def test_a_digest_with_a_verdict_is_never_gated_again(tmp_path):
    """Running an untrusted container is the most expensive thing here, and
    the chain slot is last-write-wins, so miners re-commit routinely. Work is
    keyed on the digest."""
    root = str(tmp_path)
    _write_verdict(root, "old", DIGEST_A)
    ran = []

    result = run_intake(
        ledger_root=root,
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: ran.append(ref) or {"verdict": {"admitted": True}},
    )

    assert ran == []                       # no container was run
    assert result.gated == 0
    assert result.already_verdicted == 1
    assert result.considered == 1


def test_a_rejected_digest_also_counts_as_verdicted(tmp_path):
    """A rejection is a verdict. Re-running yesterday's failure burns the same
    sandbox minutes to reach the same answer; a miner wanting another verdict
    publishes new bytes, which is a new digest."""
    root = str(tmp_path)
    _write_verdict(root, "rejected", DIGEST_A)
    assert DIGEST_A in verdicted_digests(root)


def test_a_new_digest_is_gated(tmp_path):
    root = str(tmp_path)
    _write_verdict(root, "old", DIGEST_A)
    ran = []

    result = run_intake(
        ledger_root=root,
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_B),
        gate_runner=lambda ref: ran.append(ref) or {"verdict": {"admitted": True}},
        puller=_ok_pull,
        inspector=_inspector_for(DIGEST_B),
    )

    assert result.gated == 1 and result.admitted == 1
    assert ran == [f"{REPO}@{DIGEST_B}"]


# ---- what "admitted" is allowed to mean ------------------------------------

def test_only_passing_the_gate_counts_as_admitted(tmp_path):
    """A model that pulls cleanly but loses to the naive baseline is NOT
    admitted. Conflating "we fetched it" with "it earned a place" is how a
    subnet ends up paying for nothing."""
    result = run_intake(
        ledger_root=str(tmp_path),
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: {"verdict": {"admitted": False, "reason": "below_baseline"}},
        puller=_ok_pull,
        inspector=_inspector_for(DIGEST_A),
    )

    assert result.admitted == 0 and result.rejected == 1
    assert result.details[0]["status"] == STATUS_REJECTED_GATE


def test_a_registry_serving_different_bytes_is_rejected(tmp_path):
    """The digest is the whole security property: it pins the bits the subnet
    runs to the bits the miner committed on chain."""
    result = run_intake(
        ledger_root=str(tmp_path),
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: {"verdict": {"admitted": True}},
        puller=_ok_pull,
        inspector=_inspector_for(DIGEST_B),   # served something else
    )

    assert result.admitted == 0
    assert result.details[0]["status"] == STATUS_REJECTED_DIGEST_MISMATCH


def test_a_pull_failure_is_not_an_admission(tmp_path):
    result = run_intake(
        ledger_root=str(tmp_path),
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: {"verdict": {"admitted": True}},
        puller=lambda ref: (False, "pull exit=1: unauthorized"),
    )
    assert result.admitted == 0
    assert result.details[0]["status"] == STATUS_PULL_FAILED


def test_one_miner_cannot_break_the_sweep_for_everyone(tmp_path):
    """Per-miner isolation. A stranger's container is untrusted input and the
    sweep has to survive it."""
    chain = {"hkBoom": _commit(DIGEST_A), "hkFine": _commit(DIGEST_B)}

    def gate(ref):
        if DIGEST_A[:20] in ref:
            raise RuntimeError("sandbox exploded")
        return {"verdict": {"admitted": True}}

    def inspector(ref):
        return [ref]

    result = run_intake(
        ledger_root=str(tmp_path),
        hotkeys=list(chain),
        read_commitment=chain.get,
        gate_runner=gate,
        puller=_ok_pull,
        inspector=inspector,
    )

    assert result.gated == 2
    assert result.admitted == 1              # the healthy one still got through
    statuses = {d["hotkey"]: d["status"] for d in result.details}
    assert statuses["hkFine"] == STATUS_ADMITTED
    assert statuses["hkBoom"] != STATUS_ADMITTED


# ---- verdicts are written, and the admitted set is not guessed at ----------

def test_every_verdict_is_handed_to_the_persister(tmp_path):
    written = {}
    run_intake(
        ledger_root=str(tmp_path),
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: {"verdict": {"admitted": True}},
        puller=_ok_pull,
        inspector=_inspector_for(DIGEST_A),
        persist=lambda digest, record: written.__setitem__(digest, record),
    )
    assert DIGEST_A in written
    assert written[DIGEST_A]["status"] == STATUS_ADMITTED


def test_no_admitted_file_means_nobody_is_admitted_yet(tmp_path):
    assert load_admitted(str(tmp_path)) == set()


def test_an_unreadable_admitted_file_raises_rather_than_emptying_the_field(tmp_path):
    """Treating a mangled file as "nobody is admitted" would quietly unrun
    every live model, which is worse than stopping."""
    d = verdict_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_admitted_digests.json"), "w") as f:
        f.write("{not json")

    with pytest.raises(RuntimeError, match="unreadable"):
        load_admitted(str(tmp_path))


# ---- fetch problems are retryable; gate verdicts are final -----------------

def _write_status_verdict(root, name, digest, status):
    d = verdict_dir(root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.json"), "w") as f:
        json.dump({"digest": digest, "status": status}, f)


def test_a_pull_failure_does_not_damn_the_digest(tmp_path):
    """Load-bearing for the front-running defence: the author commits the
    digest while the image is still PRIVATE, then flips it public. The sweep
    that runs in between fails to pull — and must leave the digest eligible,
    or the recommended defence becomes a self-inflicted permanent rejection."""
    root = str(tmp_path)
    _write_status_verdict(root, "early", DIGEST_A, STATUS_PULL_FAILED)
    ran = []

    result = run_intake(
        ledger_root=root,
        hotkeys=["hk1"],
        read_commitment=lambda hk: _commit(DIGEST_A),
        gate_runner=lambda ref: ran.append(ref) or {"verdict": {"admitted": True}},
        puller=_ok_pull,
        inspector=_inspector_for(DIGEST_A),
    )

    assert result.gated == 1 and result.admitted == 1   # retried, and passed


def test_a_registry_mismatch_is_retryable_too(tmp_path):
    """A registry mid-propagation can serve stale bytes; when the miner fixes
    it, the same digest deserves another look."""
    root = str(tmp_path)
    _write_status_verdict(root, "m", DIGEST_A, STATUS_REJECTED_DIGEST_MISMATCH)
    assert DIGEST_A not in verdicted_digests(root)


def test_gate_verdicts_are_final_in_both_directions(tmp_path):
    root = str(tmp_path)
    _write_status_verdict(root, "in", DIGEST_A, STATUS_ADMITTED)
    _write_status_verdict(root, "out", DIGEST_B, STATUS_REJECTED_GATE)
    assert verdicted_digests(root) == {DIGEST_A, DIGEST_B}


def test_a_verdict_with_no_status_stays_final(tmp_path):
    """Fail-safe direction for "cannot tell": do NOT re-run an unknown
    container — running strangers' code is the expensive, dangerous step."""
    root = str(tmp_path)
    _write_verdict(root, "legacy", DIGEST_A)   # envelope shape, no status
    assert DIGEST_A in verdicted_digests(root)
