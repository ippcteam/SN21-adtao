"""Every control here has a LIVE CONSUMER, asserted.

This file exists because the same defect has now appeared six times: a control
is written, tested, flagged and deployed, and nothing calls it. The anchor flag
with no committer, intake with no caller, one-payer on a host that will not run
the loop, bridge participation with zero consumers, the coverage gate nobody
imported — and then, hours after writing that list down, a coldkey cap whose
input nothing supplied and an audit block that was computed and discarded.

A unit test on a pure module cannot catch this: the module is correct. What
was missing was the wiring, so the wiring is what these tests assert.
"""

import inspect
from typing import ClassVar

from hope.validator import daily_loop
from hope.validator.daily_stream_weights import DailyAllocation

# ---- 1. the coldkey cap receives an identity map ---------------------------

def test_the_loop_accepts_and_forwards_a_coldkey_reader():
    """The cap was wired into the allocation and nothing passed coldkey_of, so
    it could never fire."""
    assert "coldkey_reader" in inspect.signature(daily_loop.run_daily_loop).parameters
    src = inspect.getsource(daily_loop.run_daily_loop)
    assert "coldkey_of=coldkey_of" in src, "the read must reach the allocation"


def test_the_entrypoint_builds_one():
    """A parameter nothing constructs is the same defect one level up."""
    import scripts.run_daily_loop as entry

    assert hasattr(entry, "_coldkey_reader")
    assert "coldkey_reader=_coldkey_reader()" in inspect.getsource(entry.main)


def test_an_unconfigured_chain_disables_the_cap_rather_than_guessing():
    import scripts.run_daily_loop as entry

    assert entry._coldkey_reader({}) is None
    assert entry._coldkey_reader({"SN21_BT_NETWORK": "finney"}) is None
    assert entry._coldkey_reader({"SN21_BT_NETWORK": "finney",
                                  "SN21_NETUID": "not-a-number"}) is None


def test_a_metagraph_read_failure_fails_open():
    """A metagraph we could not read is not evidence that anyone is farming.
    Confiscating seats over our own outage is the worse error."""
    src = inspect.getsource(daily_loop.run_daily_loop)
    assert "the cap is NOT applied today" in src
    assert "coldkey_of = None" in src


# ---- 2. the audit reaches the published record -----------------------------

def test_the_collapse_audit_is_persisted():
    """It was computed into the allocation and thrown away, which left
    verify_day --recheck-grouping comparing against an empty set every time —
    a verification that could never fail is not a verification."""
    src = inspect.getsource(daily_loop.run_daily_loop)
    assert '"collapse_audit": alloc.collapse_audit' in src


def test_the_allocation_actually_carries_one():
    assert "collapse_audit" in DailyAllocation.__dataclass_fields__


def test_verify_day_reads_the_field_the_loop_writes():
    """The two halves have to name the same key or the check silently passes."""
    import scripts.verify_day as vd

    assert 'collapse_audit' in inspect.getsource(vd.recheck_grouping)


# ---- 3. determinism is enforced, not recommended ---------------------------

def test_the_gate_rechecks_determinism():
    from hope.backtest import gate_service

    src = inspect.getsource(gate_service.gate_submission)
    assert "_nondeterminism_detail" in src


def test_a_model_that_answers_differently_is_refused(monkeypatch):
    """The published promise is that a rerun reproduces the score. A model
    giving two answers to one question makes that false for every day it runs,
    so it is refused rather than noted."""
    from hope.backtest import gate_service

    calls = {"n": 0}

    class Run:
        ok = True
        error = None
        episodes_in = 1
        predictions_out = 1

        def __init__(self, preds):
            self.predictions = preds

    def fake_run(digest, episodes, timeout_s=0):
        calls["n"] += 1
        # The re-run answers differently from what the first run recorded.
        return Run({"e1": {"7": {"cost_delta_pct": {"p50": -0.99}}}})

    monkeypatch.setattr(gate_service, "run_basket_docker", fake_run)
    detail = gate_service._nondeterminism_detail(
        "sha256:x", [{"episode_id": "e1"}],
        {"e1": {"7": {"cost_delta_pct": {"p50": -0.05}}}}, 25, 60)

    assert detail is not None and "differently" in detail


def test_a_stable_model_passes_the_determinism_check(monkeypatch):
    from hope.backtest import gate_service

    same = {"e1": {"7": {"cost_delta_pct": {"p50": -0.05}}}}

    class Run:
        ok = True
        error = None
        episodes_in = 1
        predictions_out = 1
        predictions: ClassVar = same

    monkeypatch.setattr(gate_service, "run_basket_docker",
                        lambda *a, **k: Run())
    assert gate_service._nondeterminism_detail(
        "sha256:x", [{"episode_id": "e1"}], same, 25, 60) is None


def test_abstaining_is_allowed_but_contradicting_is_not(monkeypatch):
    """A model may decline an episode. What it may not do is answer the same
    question two ways."""
    from hope.backtest import gate_service

    class Run:
        ok = True
        error = None
        episodes_in = 1
        predictions_out = 0
        predictions: ClassVar = {"e1": None}

    monkeypatch.setattr(gate_service, "run_basket_docker",
                        lambda *a, **k: Run())
    assert gate_service._nondeterminism_detail(
        "sha256:x", [{"episode_id": "e1"}],
        {"e1": {"7": {"cost_delta_pct": {"p50": -0.05}}}}, 25, 60) is None


def test_a_rerun_that_will_not_start_is_not_nondeterminism(monkeypatch):
    """Liveness is judged elsewhere, and by a rule that does not confuse a
    crash with a lie."""
    from hope.backtest import gate_service

    class Dead:
        ok = False
        error = "exit=1"

    monkeypatch.setattr(gate_service, "run_basket_docker",
                        lambda *a, **k: Dead())
    assert gate_service._nondeterminism_detail(
        "sha256:x", [{"episode_id": "e1"}], {"e1": {}}, 25, 60) is None
