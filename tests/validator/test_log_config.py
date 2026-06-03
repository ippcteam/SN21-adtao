"""Test the shared logging-visibility helper (works despite a global disable)."""
import logging

from hope.validator._log import configure_logging


def test_restores_visibility_after_global_disable(capsys):
    # Simulate what `import bittensor` does: a global stdlib-logging disable.
    logging.disable(logging.CRITICAL)
    lg = logging.getLogger("test.hope.visible")

    configure_logging(lg, "INFO")
    lg.info("decision-line")

    # global disable cleared, dedicated handler attached, no propagation
    assert logging.root.manager.disable == 0  # logging.NOTSET
    assert len(lg.handlers) == 1
    assert lg.propagate is False
    assert lg.level == logging.INFO
    assert "decision-line" in capsys.readouterr().out


def test_level_argument_is_honored():
    lg = logging.getLogger("test.hope.level")
    configure_logging(lg, "WARNING")
    assert lg.level == logging.WARNING
