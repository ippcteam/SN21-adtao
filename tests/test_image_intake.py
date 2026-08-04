"""Image intake — digest-anchored pull + gate feed (the M0 trust chain).

The commitment digest is the trust anchor: a registry serving different
bytes, a poisoned cache, or a malformed miner string must all fail loudly
and in ISOLATION (one bad image never aborts the sweep). No real docker in
these tests — puller/inspector/gate are injected fakes.
"""

import pytest

from hope.backtest.image_intake import (
    STATUS_ADMITTED,
    STATUS_INVALID_COMMITMENT,
    STATUS_PULL_FAILED,
    STATUS_REJECTED_DIGEST_MISMATCH,
    STATUS_REJECTED_GATE,
    IntakeResult,
    ModelCommitment,
    intake_all,
    intake_model,
    pull_by_digest,
    validate_commitment,
)

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
REF = "ghcr.io/minerco/sn21-model"


def _pull_ok(pinned):
    return True, ""


def _inspect_match(pinned):
    return [f"{REF}@{DIGEST}"]


def _gate(admitted=True, reason="beats_baseline"):
    def runner(local_ref):
        return {"verdict": {"admitted": admitted, "reason": reason}}
    return runner


# ---- validation before docker ----------------------------------------------

@pytest.mark.parametrize("digest", [
    "sha256:" + "a" * 63,            # short
    "sha256:" + "A" * 64,            # uppercase hex
    "sha512:" + "a" * 64,            # wrong algo
    "a" * 64,                        # bare hex
    "", None,
])
def test_malformed_digest_rejected_before_docker(digest):
    called = []
    res = pull_by_digest(REF, digest, puller=lambda p: called.append(p) or (True, ""))
    assert not res.pulled and "malformed digest" in res.error
    assert called == []  # docker never invoked


@pytest.mark.parametrize("ref", [
    "repo name",                     # whitespace
    "Repo/Upper",                    # uppercase
    "repo@sha256:" + "c" * 64,       # smuggled digest
    "repo;rm -rf /",                 # shell metachars
    "-flag-injection",               # leading dash
    "", None,
])
def test_malformed_ref_rejected_before_docker(ref):
    called = []
    res = pull_by_digest(ref, DIGEST, puller=lambda p: called.append(p) or (True, ""))
    assert not res.pulled and "malformed image ref" in res.error
    assert called == []


def test_registry_host_with_port_is_valid():
    c = ModelCommitment("hk", "registry.example.com:5000/team/model", DIGEST)
    assert validate_commitment(c) is None


# ---- pull + digest verification ---------------------------------------------

def test_happy_path_pull_is_digest_pinned_and_verified():
    seen = []
    res = pull_by_digest(REF, DIGEST,
                         puller=lambda p: seen.append(p) or (True, ""),
                         inspector=_inspect_match)
    assert res.pulled and res.verified
    assert res.local_ref == f"{REF}@{DIGEST}"
    assert seen == [f"{REF}@{DIGEST}"]  # pull itself was digest-pinned


def test_digest_mismatch_rejects_loudly():
    res = pull_by_digest(REF, DIGEST,
                         puller=_pull_ok,
                         inspector=lambda p: [f"{REF}@{OTHER}"])
    assert res.pulled and not res.verified
    assert "digest mismatch" in res.error


def test_pull_failure_reported():
    res = pull_by_digest(REF, DIGEST, puller=lambda p: (False, "network sad"))
    assert not res.pulled and res.error == "network sad"


# ---- intake_model statuses ---------------------------------------------------

def test_intake_admitted():
    r = intake_model(ModelCommitment("hk1", REF, DIGEST), _gate(True),
                     puller=_pull_ok, inspector=_inspect_match)
    assert r.status == STATUS_ADMITTED and r.detail == "beats_baseline"


def test_intake_gate_rejection():
    r = intake_model(ModelCommitment("hk1", REF, DIGEST),
                     _gate(False, "below_baseline_or_coverage"),
                     puller=_pull_ok, inspector=_inspect_match)
    assert r.status == STATUS_REJECTED_GATE


def test_intake_gate_exception_is_rejection_not_crash():
    def bomb(local_ref):
        raise RuntimeError("gate corpus unavailable")
    r = intake_model(ModelCommitment("hk1", REF, DIGEST), bomb,
                     puller=_pull_ok, inspector=_inspect_match)
    assert r.status == STATUS_REJECTED_GATE and "gate raised" in r.detail


def test_intake_statuses_for_pull_and_mismatch_and_invalid():
    mismatch = intake_model(ModelCommitment("hk", REF, DIGEST), _gate(),
                            puller=_pull_ok,
                            inspector=lambda p: [f"{REF}@{OTHER}"])
    assert mismatch.status == STATUS_REJECTED_DIGEST_MISMATCH

    pullfail = intake_model(ModelCommitment("hk", REF, DIGEST), _gate(),
                            puller=lambda p: (False, "boom"))
    assert pullfail.status == STATUS_PULL_FAILED

    invalid = intake_model(ModelCommitment("hk", "Bad Ref", DIGEST), _gate())
    assert invalid.status == STATUS_INVALID_COMMITMENT


# ---- sweep isolation ----------------------------------------------------------

def test_intake_all_isolates_and_summarises():
    commitments = [
        ModelCommitment("good", REF, DIGEST),
        ModelCommitment("badref", "NOPE", DIGEST),
        ModelCommitment("mismatch", REF, DIGEST),
        ModelCommitment("pullfail", REF, DIGEST),
    ]

    # deterministic fakes: badref never reaches the puller (pre-rejected),
    # so pulls are good(#1), mismatch(#2), pullfail(#3); inspections are
    # good(#1 -> match), mismatch(#2 -> wrong digest)
    state = {"n": 0}

    def puller2(pinned):
        state["n"] += 1
        return (False, "registry down") if state["n"] == 3 else (True, "")

    insp_state = {"n": 0}

    def inspector2(pinned):
        insp_state["n"] += 1
        return [f"{REF}@{OTHER}"] if insp_state["n"] == 2 else [f"{REF}@{DIGEST}"]

    out = intake_all(commitments, _gate(True), puller=puller2, inspector=inspector2)
    assert out["total"] == 4
    assert out["by_status"] == {
        STATUS_ADMITTED: 1,
        STATUS_INVALID_COMMITMENT: 1,
        STATUS_REJECTED_DIGEST_MISMATCH: 1,
        STATUS_PULL_FAILED: 1,
    }
    assert out["admitted"] == ["good"]
    assert all(isinstance(r, IntakeResult) for r in out["results"])
