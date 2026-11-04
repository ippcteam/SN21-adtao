"""A model the kernel kills is named for what happened, not as a crash (30 Oct 2026).

On 2026-10-29 six models were killed at the CPU hard limit (all at 905 s, peak
140-417 MB, no container OOM). util-linux unshare < 2.40 cannot re-raise SIGKILL
on itself, printed "sigprocmask unblock failed: Invalid argument" and exited 1,
so each read as "exit=1" - a crash. The label changes; who is charged does not
(running out of the published time budget was already the miner's)."""
from hope.backtest.ns_sandbox import (
    DEFAULT_CPU_SECONDS,
    ERR_EXIT_PREFIX,
    ERR_TIMEOUT_PREFIX,
    killed_child_error,
)
from hope.scoring.absence_penalty import PUBLISHED_RUN_BUDGET_S, operator_fault
from hope.scoring.chronic_failure import (
    FAULT_MINER,
    REASON_TIMEOUT,
    classify_failure,
    failure_reason,
)

OLD_UNSHARE = "unshare: sigprocmask unblock failed: Invalid argument\n"


def test_the_29_oct_kills_are_named_as_the_cpu_limit():
    err = killed_child_error(1, OLD_UNSHARE, 905.4, DEFAULT_CPU_SECONDS)
    assert err.startswith(f"{ERR_TIMEOUT_PREFIX}{DEFAULT_CPU_SECONDS}s: CPU time limit reached")
    assert "SIGKILL" in err and "crash" not in err


def test_newer_unshare_reraises_the_signal_and_is_named_the_same():
    for rc in (-9, 137):
        assert killed_child_error(rc, "", 906.0, 900).startswith("timeout>900s: CPU time limit")


def test_a_kill_before_the_cpu_budget_is_not_called_a_timeout():
    err = killed_child_error(1, OLD_UNSHARE, 41.7, 900)
    assert err.startswith(f"{ERR_EXIT_PREFIX}137: killed (SIGKILL) after 41s")


def test_a_real_crash_is_left_alone():
    assert killed_child_error(1, "Traceback ...\nValueError: bad input", 3.0, 900) is None
    assert killed_child_error(2, OLD_UNSHARE, 905.0, 900) is None


def test_who_is_charged_does_not_change():
    err = killed_child_error(1, OLD_UNSHARE, 905.4, 900)
    # not ours: the budget it ran out of is the published one
    assert operator_fault(err, PUBLISHED_RUN_BUDGET_S) is False
    assert classify_failure(False, err) == FAULT_MINER
    assert failure_reason(False, err) == REASON_TIMEOUT


def test_the_heartbeat_groups_it_as_a_timeout():
    err = killed_child_error(1, OLD_UNSHARE, 905.4, 900)
    assert err.split(":")[0].split(">")[0].strip() == "timeout"
