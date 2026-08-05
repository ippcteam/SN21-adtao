"""Tests for the consolidated validator daemon (supervisor logic only).

The daemon orchestrates three subprocess tools; these tests stub the runner so
no chain/subprocess is touched, and assert the daemon builds the right commands
(order, args, per-command env, per-tool timeout) and isolates tool failures.
"""
from hope.validator.daemon import (
    TIMEOUT_RC,
    DaemonConfig,
    build_commands,
    run_tick,
)


def _cfg(**kw):
    base = {"reg_index": "/data/sn21-reg-index.json",
                "reg_index_archive_url": "wss://archive.example:443",
                "wallet_name": "val", "wallet_hotkey": "hk", "netuid": 21, "network": "finney"}
    base.update(kw)
    return DaemonConfig(**base)


def _names(cmds):
    return [c[0] for c in cmds]


def _by_name(cmds, name):
    """Return (argv, env, timeout) for the named command."""
    _n, argv, env, timeout = next(c for c in cmds if c[0] == name)
    return argv, env, timeout


def test_reg_index_head_first_then_archive_scoring_heartbeat_last():
    # reg-index-head (fast lite-node refresh) runs FIRST so new registrations are
    # caught every tick even if the archive block-scan is slow/stuck; the archive
    # build follows for historical completeness. Heartbeat runs LAST: scoring
    # commits fresh weights first, so the heartbeat then sees a reset gap and
    # skips — they never compete for the set_weights slot (180-block rate limit).
    assert _names(build_commands(_cfg())) == [
        "staleness-alarm", "reg-index-head", "reg-index", "scoring", "heartbeat"]


def test_reg_index_head_uses_lite_node_and_refresh_script():
    argv, env, timeout = _by_name(build_commands(_cfg()), "reg-index-head")
    assert "scripts.refresh_reg_index_head" in argv
    assert "--index" in argv and "/data/sn21-reg-index.json" in argv
    assert "--network" in argv and "finney" in argv
    # The head sweep MUST run on a fast lite node. It pins SN21_SUBTENSOR_URL to
    # the lite endpoint via its own env so a globally-set archive SN21_SUBTENSOR_URL
    # (used by scoring for historical reads) is NOT inherited here — otherwise the
    # 256-hotkey pass runs on the slow archive node and blows the timeout.
    assert env == {"SN21_SUBTENSOR_URL": "finney"}
    assert timeout == _cfg().reg_index_head_timeout_seconds


def test_reg_index_head_network_override_pins_lite_endpoint():
    argv, env, _t = _by_name(
        build_commands(_cfg(reg_index_head_network="wss://lite.example:443")),
        "reg-index-head")
    assert argv[argv.index("--network") + 1] == "wss://lite.example:443"
    assert env == {"SN21_SUBTENSOR_URL": "wss://lite.example:443"}


def test_skip_reg_index_head_leaves_only_archive_scan():
    names = _names(build_commands(_cfg(skip_reg_index_head=True)))
    assert "reg-index-head" not in names
    assert names == ["staleness-alarm", "reg-index", "scoring", "heartbeat"]


def test_skip_reg_index_drops_both_reg_steps():
    names = _names(build_commands(_cfg(skip_reg_index=True)))
    assert "reg-index-head" not in names and "reg-index" not in names


def test_skip_reg_index_block_leaves_only_head_sweep():
    # Disabling just the slow archive block-scan keeps the fast head sweep
    # (and everything else) — the head sweep + committed index hold coverage.
    names = _names(build_commands(_cfg(skip_reg_index_block=True)))
    assert "reg-index" not in names           # block-scan dropped
    assert "reg-index-head" in names          # head sweep retained
    assert names == ["staleness-alarm", "reg-index-head", "scoring", "heartbeat"]


def test_each_command_carries_its_timeout():
    cmds = build_commands(_cfg())
    timeouts = {c[0]: c[3] for c in cmds}
    assert timeouts["heartbeat"] == _cfg().heartbeat_timeout_seconds
    assert timeouts["reg-index"] == _cfg().reg_index_timeout_seconds
    assert timeouts["scoring"] == _cfg().scoring_timeout_seconds


def test_reg_index_uses_archive_env_override_only():
    cmds = build_commands(_cfg())
    argv, env, _t = _by_name(cmds, "reg-index")
    assert "scripts.build_reg_index" in argv
    assert "--index" in argv and "/data/sn21-reg-index.json" in argv
    assert env == {"SN21_SUBTENSOR_URL": "wss://archive.example:443"}
    # scoring + heartbeat get NO env override (they use the default endpoint).
    # reg-index-head DOES pin its own lite endpoint (see its dedicated test).
    for n, _argv, e, _t in cmds:
        if n not in ("reg-index", "reg-index-head"):
            assert e == {}


def test_reg_index_cold_start_lookback_passthrough():
    # 0 (default) => no flag; >0 => bounded cold-start scan
    argv, _e, _t = _by_name(build_commands(_cfg()), "reg-index")
    assert "--cold-start-lookback-blocks" not in argv
    argv, _e, _t = _by_name(build_commands(_cfg(reg_index_cold_start_lookback_blocks=50)),
                            "reg-index")
    assert argv[argv.index("--cold-start-lookback-blocks") + 1] == "50"


def test_reg_index_max_blocks_per_tick_passthrough():
    argv, _e, _t = _by_name(build_commands(_cfg()), "reg-index")
    assert "--max-blocks-per-pass" not in argv
    argv, _e, _t = _by_name(build_commands(_cfg(reg_index_max_blocks_per_tick=200)),
                            "reg-index")
    assert argv[argv.index("--max-blocks-per-pass") + 1] == "200"


def test_scoring_uses_release_auto_and_prebuilt_index():
    argv, _e, _t = _by_name(build_commands(_cfg()), "scoring")
    assert argv[0] == "hope-validator"
    assert argv[1:3] == ["--release", "auto"]
    assert "--reg-index-prebuilt" in argv and "/data/sn21-reg-index.json" in argv
    assert "--ignore-already-scored" not in argv


def test_scoring_passes_ed25519_key_and_archive_tiers():
    cmds = build_commands(_cfg(ed25519_key_file="/keys/validator.pem",
                               archive_tier_2_urls=("https://a.test", "https://b.test")))
    argv, _e, _t = _by_name(cmds, "scoring")
    assert argv[argv.index("--ed25519-key-file") + 1] == "/keys/validator.pem"
    # repeatable --archive-tier-2, one per url
    t2 = [argv[i + 1] for i, a in enumerate(argv) if a == "--archive-tier-2"]
    assert t2 == ["https://a.test", "https://b.test"]


def test_scoring_omits_ed25519_and_tiers_when_unset():
    argv, _e, _t = _by_name(build_commands(_cfg()), "scoring")
    assert "--ed25519-key-file" not in argv and "--archive-tier-2" not in argv


def test_ignore_already_scored_flag_passthrough():
    argv, _e, _t = _by_name(build_commands(_cfg(ignore_already_scored=True)), "scoring")
    assert "--ignore-already-scored" in argv


def test_heartbeat_dry_run_flag_passthrough():
    argv, _e, _t = _by_name(build_commands(_cfg(heartbeat_dry_run=True)), "heartbeat")
    assert argv[0] == "hope-validator-heartbeat" and "--dry-run" in argv


def test_skip_toggles_drop_commands():
    assert _names(build_commands(_cfg(skip_scoring=True))) == [
        "staleness-alarm", "reg-index-head", "reg-index", "heartbeat"]
    assert _names(build_commands(_cfg(skip_heartbeat=True))) == [
        "staleness-alarm", "reg-index-head", "reg-index", "scoring"]
    # skip_reg_index drops the scan steps but the staleness alarm still fires
    # (its whole point is to scream when scanning is broken/skipped).
    assert _names(build_commands(_cfg(skip_reg_index=True))) == [
        "staleness-alarm", "scoring", "heartbeat"]


def test_no_reg_index_path_means_no_reg_index_command():
    assert _names(build_commands(_cfg(reg_index=""))) == ["scoring", "heartbeat"]


def test_report_step_absent_without_artifact_dir():
    # No leaderboard_artifact_dir → no CMS post step (default).
    assert "report" not in _names(build_commands(_cfg()))


def test_report_step_runs_after_scoring_before_heartbeat():
    cmds = build_commands(_cfg(leaderboard_artifact_dir="/data/sn21-epoch-artifacts"))
    assert _names(cmds) == [
        "staleness-alarm", "reg-index-head", "reg-index", "scoring", "report", "heartbeat"]
    argv, _e, _t = _by_name(cmds, "report")
    assert "scripts.post_epoch_report" in argv
    assert argv[argv.index("--artifact-dir") + 1] == "/data/sn21-epoch-artifacts"
    assert "--skip-if-posted" in argv  # idempotent: never re-posts a frozen epoch


def test_skip_report_drops_the_report_step():
    cmds = build_commands(_cfg(leaderboard_artifact_dir="/data/x", skip_report=True))
    assert "report" not in _names(cmds)


def test_run_tick_calls_every_tool_in_order_and_records_codes():
    calls = []
    def fake(name, argv, env, timeout):
        calls.append(name)
        return 0
    res = run_tick(_cfg(), runner=fake)
    assert calls == ["staleness-alarm", "reg-index-head", "reg-index", "scoring", "heartbeat"]
    assert res == {"staleness-alarm": 0, "heartbeat": 0, "reg-index-head": 0,
                   "reg-index": 0, "scoring": 0}


def test_run_tick_passes_each_tools_timeout_to_runner():
    seen = {}
    def fake(name, argv, env, timeout):
        seen[name] = timeout
        return 0
    run_tick(_cfg(), runner=fake)
    assert seen["heartbeat"] == _cfg().heartbeat_timeout_seconds
    assert seen["reg-index"] == _cfg().reg_index_timeout_seconds
    assert seen["scoring"] == _cfg().scoring_timeout_seconds


def test_run_tick_isolates_failures_so_heartbeat_still_runs_last():
    # reg-index degraded + scoring blowing up must NOT stop the heartbeat (last)
    # from running — the activity floor is maintained regardless of upstream tools.
    def fake(name, argv, env, timeout):
        if name == "scoring":
            raise RuntimeError("boom")
        if name == "reg-index":
            return 3
        return 0  # heartbeat
    res = run_tick(_cfg(), runner=fake)
    assert res == {"staleness-alarm": 0, "reg-index-head": 0, "reg-index": 3,
                   "scoring": -1, "heartbeat": 0}


def test_default_runner_returns_timeout_rc_on_expiry():
    # A tool that exceeds its ceiling is killed and reported as TIMEOUT_RC so the
    # tick continues — sleep far longer than the tiny timeout we pass.
    import sys

    from hope.validator.daemon import _default_runner
    rc = _default_runner("slow",
                         [sys.executable, "-c", "import time; time.sleep(30)"],
                         {}, timeout=0.5)
    assert rc == TIMEOUT_RC
