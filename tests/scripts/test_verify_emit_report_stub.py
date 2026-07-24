"""Tests for `verify_epoch.py --emit-report` in v1 stub mode.

Per Q12 the flag is a fast-follow: v1 prints an informational message
to stderr and exits with a distinct non-zero code so cron / CI can
identify the stub path. The full implementation lands in v1.1 when the
operator's truth file is publicly hosted.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make scripts/ importable for the test.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_emit_report_stub_exits_with_distinct_code(capsys):
    from scripts import verify_epoch
    exit_code = verify_epoch.main([
        "--epoch-id", "WR-2026-W18-PUB-E1",
        "--validator-hotkey", "5G" + "x" * 46,
        "--emit-report",
    ])
    assert exit_code == 4   # distinct stub exit code, not 0/1/2/3


def test_emit_report_stub_message_explains_status(capsys):
    from scripts import verify_epoch
    verify_epoch.main([
        "--epoch-id", "WR-2026-W18-PUB-E1",
        "--validator-hotkey", "5G" + "x" * 46,
        "--emit-report",
    ])
    captured = capsys.readouterr()
    # The user-facing message must say WHAT is missing and WHEN it lands.
    assert "truth file" in captured.err.lower()
    assert "v1.1" in captured.err
    # Internal byte-identity note so operator-side users aren't confused.
    assert "aggregator" in captured.err.lower()


def test_emit_report_stub_does_not_touch_chain(capsys):
    """The stub must short-circuit before any chain RPC."""
    from scripts import verify_epoch
    # Pass a clearly-bogus chain endpoint; if the stub hit chain, this
    # would error noisily. Stub should exit cleanly.
    exit_code = verify_epoch.main([
        "--epoch-id", "WR-2026-W18-PUB-E1",
        "--validator-hotkey", "5G" + "x" * 46,
        "--network", "test",
        "--netuid", "466",
        "--emit-report",
    ])
    assert exit_code == 4
