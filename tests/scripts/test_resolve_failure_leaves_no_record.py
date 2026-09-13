"""A resolve failure must leave no run record, whatever its cause.

The daemon runs the pipeline once per UTC day and takes an existing run
record as "already ran today". Resolve is the first stage: nothing has been
executed or written when it fails, so the only thing a record can do there
is stop the daemon from retrying — the next hourly tick would otherwise
resolve again at the cost of one listing call and one package fetch. A
basket that is not ready has always been treated that way; a timeout or any
other error on the same calls must be too.
"""

from __future__ import annotations

import os
import sys

import pytest

import scripts.run_daily_pipeline as rdp

DAY = "2026-09-12"


def _run_main(monkeypatch, tmp_path, failure):
    ledger = tmp_path / "ledger"
    monkeypatch.setenv("SN21_EXECUTOR_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setattr(sys, "argv", [
        "run_daily_pipeline", "--day", DAY, "--ledger-root", str(ledger),
        "--no-reference", "--skip-intake",
    ])

    def failing_resolve(_explicit, _day):
        raise failure

    monkeypatch.setattr(rdp, "resolve_basket", failing_resolve)
    rc = rdp.main()
    return rc, ledger


@pytest.mark.parametrize("failure", [
    rdp.BasketNotReady("BD-2026-09-11 is not in the operator listing"),
    TimeoutError("The read operation timed out"),
    ConnectionResetError("connection reset by peer"),
    RuntimeError("operator API returned 502"),
])
def test_no_run_record_after_a_failed_resolve(monkeypatch, tmp_path, failure):
    rc, ledger = _run_main(monkeypatch, tmp_path, failure)
    assert rc == 1
    assert not os.path.exists(os.path.join(ledger, "pipeline_runs", f"{DAY}.json")), (
        "a run record after a failed resolve marks the day done with nothing "
        "published and stops the daemon from retrying")


def test_a_failed_resolve_says_it_will_retry(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, TimeoutError("The read operation timed out"))
    out = capsys.readouterr().out
    assert "[resolve] ERROR TimeoutError" in out
    assert "will retry next tick" in out
    assert "===PIPELINE-END===" in out
