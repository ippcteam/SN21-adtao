"""The reg-index staleness alarm — ground-truth health check that would have
caught the 2026-06-04 freeze the same day instead of 11 days later.
"""
import json

from scripts.check_reg_index_staleness import (
    DEFAULT_THRESHOLD_BLOCKS,
    EXIT_FRESH,
    EXIT_STALE,
    EXIT_UNKNOWN,
    evaluate,
    main,
    read_last_scanned_block,
)
from hope.validator import daemon as daemon_mod


class TestEvaluate:
    def test_fresh_within_threshold(self):
        gap, stale = evaluate(last_scanned_block=8_413_000, head=8_413_500,
                              threshold_blocks=2000)
        assert gap == 500
        assert stale is False

    def test_stale_beyond_threshold(self):
        # the real freeze: scanned 8_332_540, head ~8_413_754 → 81k behind
        gap, stale = evaluate(last_scanned_block=8_332_540, head=8_413_754,
                              threshold_blocks=2000)
        assert gap == 81_214
        assert stale is True

    def test_exactly_at_threshold_is_not_stale(self):
        _, stale = evaluate(8_000_000, 8_002_000, threshold_blocks=2000)
        assert stale is False  # strictly greater-than trips it

    def test_unknown_inputs_never_stale(self):
        assert evaluate(None, 8_413_000, 2000) == (None, False)
        assert evaluate(8_413_000, None, 2000) == (None, False)


class TestReadCheckpoint:
    def test_reads_last_scanned_block(self, tmp_path):
        p = tmp_path / "idx.json.state.json"
        p.write_text(json.dumps({"last_scanned_block": 8_332_540, "entries": 58}))
        assert read_last_scanned_block(str(p)) == 8_332_540

    def test_missing_file_returns_none(self, tmp_path):
        assert read_last_scanned_block(str(tmp_path / "nope.json")) is None

    def test_malformed_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert read_last_scanned_block(str(p)) is None

    def test_absent_field_returns_none(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"entries": 58}))
        assert read_last_scanned_block(str(p)) is None


class TestMainExitCodes:
    def test_no_index_is_unknown(self):
        assert main(["--index", ""]) == EXIT_UNKNOWN

    def test_no_checkpoint_is_unknown(self, tmp_path):
        # index path with no sidecar → cannot assess, must not disrupt the tick
        assert main(["--index", str(tmp_path / "absent.json")]) == EXIT_UNKNOWN

    def test_stale_returns_exit_stale(self, tmp_path, monkeypatch):
        idx = tmp_path / "idx.json"
        (tmp_path / "idx.json.state.json").write_text(
            json.dumps({"last_scanned_block": 8_332_540}))

        class _FakeSub:
            def get_current_block(self):
                return 8_413_754

        monkeypatch.setattr("hope.validator._subtensor.make_subtensor",
                            lambda *a, **k: _FakeSub())
        assert main(["--index", str(idx), "--threshold-blocks", "2000"]) == EXIT_STALE

    def test_fresh_returns_exit_fresh(self, tmp_path, monkeypatch):
        idx = tmp_path / "idx.json"
        (tmp_path / "idx.json.state.json").write_text(
            json.dumps({"last_scanned_block": 8_413_700}))

        class _FakeSub:
            def get_current_block(self):
                return 8_413_754

        monkeypatch.setattr("hope.validator._subtensor.make_subtensor",
                            lambda *a, **k: _FakeSub())
        assert main(["--index", str(idx), "--threshold-blocks", "2000"]) == EXIT_FRESH


class TestDaemonWiring:
    def _cfg(self, **kw):
        return daemon_mod.DaemonConfig(reg_index="/var/data/idx.json", **kw)

    def test_alarm_is_first_step(self):
        names = [c[0] for c in daemon_mod.build_commands(self._cfg())]
        assert names[0] == "staleness-alarm"

    def test_alarm_runs_even_when_reg_index_scan_skipped(self):
        # the freeze case: scanning broken/skipped, alarm must still fire
        names = [c[0] for c in daemon_mod.build_commands(
            self._cfg(skip_reg_index=True))]
        assert "staleness-alarm" in names

    def test_alarm_can_be_disabled(self):
        names = [c[0] for c in daemon_mod.build_commands(
            self._cfg(skip_staleness_alarm=True))]
        assert "staleness-alarm" not in names

    def test_threshold_passed_through(self):
        cmd = daemon_mod.build_commands(
            self._cfg(reg_index_staleness_alarm_blocks=5000))[0]
        argv = cmd[1]
        assert "--threshold-blocks" in argv
        assert argv[argv.index("--threshold-blocks") + 1] == "5000"

    def test_default_threshold(self):
        assert daemon_mod.DaemonConfig().reg_index_staleness_alarm_blocks == \
            DEFAULT_THRESHOLD_BLOCKS
