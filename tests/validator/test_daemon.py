"""Tests for the consolidated validator daemon (supervisor logic only).

The daemon orchestrates three subprocess tools; these tests stub the runner so
no chain/subprocess is touched, and assert the daemon builds the right commands
(order, args, per-command env, per-tool timeout) and isolates tool failures.
"""
from hope.validator.daemon import (
    DaemonConfig, build_commands, run_tick, TIMEOUT_RC,
)


def _cfg(**kw):
    base = dict(reg_index="/data/sn21-reg-index.json",
                reg_index_archive_url="wss://archive.example:443",
                wallet_name="val", wallet_hotkey="hk", netuid=21, network="finney")
    base.update(kw)
    return DaemonConfig(**base)


def _names(cmds):
    return [c[0] for c in cmds]


def _by_name(cmds, name):
    """Return (argv, env, timeout) for the named command."""
    _n, argv, env, timeout = next(c for c in cmds if c[0] == name)
    return argv, env, timeout


def test_heartbeat_runs_first_then_reg_index_then_scoring():
    # Heartbeat is safety-critical + fast, so it must run BEFORE the slow
    # archive-bound tools — a stalled reg-index/scoring can never delay it.
    assert _names(build_commands(_cfg())) == ["heartbeat", "reg-index", "scoring"]


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
    # scoring + heartbeat get NO env override (they use the default endpoint)
    for n, _argv, e, _t in cmds:
        if n != "reg-index":
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
    assert _names(build_commands(_cfg(skip_scoring=True))) == ["heartbeat", "reg-index"]
    assert _names(build_commands(_cfg(skip_heartbeat=True))) == ["reg-index", "scoring"]
    assert _names(build_commands(_cfg(skip_reg_index=True))) == ["heartbeat", "scoring"]


def test_no_reg_index_path_means_no_reg_index_command():
    assert _names(build_commands(_cfg(reg_index=""))) == ["heartbeat", "scoring"]


def test_run_tick_calls_every_tool_in_order_and_records_codes():
    calls = []
    def fake(name, argv, env, timeout):
        calls.append(name)
        return 0
    res = run_tick(_cfg(), runner=fake)
    assert calls == ["heartbeat", "reg-index", "scoring"]
    assert res == {"heartbeat": 0, "reg-index": 0, "scoring": 0}


def test_run_tick_passes_each_tools_timeout_to_runner():
    seen = {}
    def fake(name, argv, env, timeout):
        seen[name] = timeout
        return 0
    run_tick(_cfg(), runner=fake)
    assert seen["heartbeat"] == _cfg().heartbeat_timeout_seconds
    assert seen["reg-index"] == _cfg().reg_index_timeout_seconds
    assert seen["scoring"] == _cfg().scoring_timeout_seconds


def test_run_tick_isolates_failures_after_heartbeat_already_ran():
    # heartbeat runs first and succeeds; a later tool blowing up never undoes it.
    def fake(name, argv, env, timeout):
        if name == "scoring":
            raise RuntimeError("boom")
        if name == "reg-index":
            return 3
        return 0
    res = run_tick(_cfg(), runner=fake)
    assert res == {"heartbeat": 0, "reg-index": 3, "scoring": -1}


def test_default_runner_returns_timeout_rc_on_expiry():
    # A tool that exceeds its ceiling is killed and reported as TIMEOUT_RC so the
    # tick continues — sleep far longer than the tiny timeout we pass.
    from hope.validator.daemon import _default_runner
    import sys
    rc = _default_runner("slow",
                         [sys.executable, "-c", "import time; time.sleep(30)"],
                         {}, timeout=0.5)
    assert rc == TIMEOUT_RC
