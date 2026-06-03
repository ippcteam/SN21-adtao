"""Tests for the consolidated validator daemon (supervisor logic only).

The daemon orchestrates three subprocess tools; these tests stub the runner so
no chain/subprocess is touched, and assert the daemon builds the right commands
(order, args, per-command env) and isolates tool failures.
"""
from hope.validator.daemon import DaemonConfig, build_commands, run_tick


def _cfg(**kw):
    base = dict(reg_index="/data/sn21-reg-index.json",
                reg_index_archive_url="wss://archive.example:443",
                wallet_name="val", wallet_hotkey="hk", netuid=21, network="finney")
    base.update(kw)
    return DaemonConfig(**base)


def _names(cmds):
    return [c[0] for c in cmds]


def test_default_tick_runs_all_three_in_order():
    assert _names(build_commands(_cfg())) == ["reg-index", "scoring", "heartbeat"]


def test_reg_index_uses_archive_env_override_only():
    cmds = build_commands(_cfg())
    name, argv, env = next(c for c in cmds if c[0] == "reg-index")
    assert "scripts.build_reg_index" in argv
    assert "--index" in argv and "/data/sn21-reg-index.json" in argv
    assert env == {"SN21_SUBTENSOR_URL": "wss://archive.example:443"}
    # scoring + heartbeat get NO env override (they use the default endpoint)
    for n, _argv, e in cmds:
        if n != "reg-index":
            assert e == {}


def test_reg_index_cold_start_lookback_passthrough():
    # 0 (default) => no flag; >0 => bounded cold-start scan
    _n, argv, _e = next(c for c in build_commands(_cfg()) if c[0] == "reg-index")
    assert "--cold-start-lookback-blocks" not in argv
    _n, argv, _e = next(c for c in build_commands(_cfg(reg_index_cold_start_lookback_blocks=50))
                        if c[0] == "reg-index")
    assert argv[argv.index("--cold-start-lookback-blocks") + 1] == "50"


def test_scoring_uses_release_auto_and_prebuilt_index():
    _n, argv, _e = next(c for c in build_commands(_cfg()) if c[0] == "scoring")
    assert argv[0] == "hope-validator"
    assert argv[1:3] == ["--release", "auto"]
    assert "--reg-index-prebuilt" in argv and "/data/sn21-reg-index.json" in argv
    assert "--ignore-already-scored" not in argv


def test_scoring_passes_ed25519_key_and_archive_tiers():
    cmds = build_commands(_cfg(ed25519_key_file="/keys/validator.pem",
                               archive_tier_2_urls=("https://a.test", "https://b.test")))
    _n, argv, _e = next(c for c in cmds if c[0] == "scoring")
    assert argv[argv.index("--ed25519-key-file") + 1] == "/keys/validator.pem"
    # repeatable --archive-tier-2, one per url
    t2 = [argv[i + 1] for i, a in enumerate(argv) if a == "--archive-tier-2"]
    assert t2 == ["https://a.test", "https://b.test"]


def test_scoring_omits_ed25519_and_tiers_when_unset():
    _n, argv, _e = next(c for c in build_commands(_cfg()) if c[0] == "scoring")
    assert "--ed25519-key-file" not in argv and "--archive-tier-2" not in argv


def test_ignore_already_scored_flag_passthrough():
    _n, argv, _e = next(c for c in build_commands(_cfg(ignore_already_scored=True))
                        if c[0] == "scoring")
    assert "--ignore-already-scored" in argv


def test_heartbeat_dry_run_flag_passthrough():
    _n, argv, _e = next(c for c in build_commands(_cfg(heartbeat_dry_run=True))
                        if c[0] == "heartbeat")
    assert argv[0] == "hope-validator-heartbeat" and "--dry-run" in argv


def test_skip_toggles_drop_commands():
    assert _names(build_commands(_cfg(skip_scoring=True))) == ["reg-index", "heartbeat"]
    assert _names(build_commands(_cfg(skip_heartbeat=True))) == ["reg-index", "scoring"]
    assert _names(build_commands(_cfg(skip_reg_index=True))) == ["scoring", "heartbeat"]


def test_no_reg_index_path_means_no_reg_index_command():
    assert _names(build_commands(_cfg(reg_index=""))) == ["scoring", "heartbeat"]


def test_run_tick_calls_every_tool_and_records_codes():
    calls = []
    def fake(name, argv, env):
        calls.append(name)
        return 0
    res = run_tick(_cfg(), runner=fake)
    assert calls == ["reg-index", "scoring", "heartbeat"]
    assert res == {"reg-index": 0, "scoring": 0, "heartbeat": 0}


def test_run_tick_isolates_failures():
    def fake(name, argv, env):
        if name == "scoring":
            raise RuntimeError("boom")
        if name == "heartbeat":
            return 3
        return 0
    res = run_tick(_cfg(), runner=fake)
    # reg-index ok, scoring raised -> -1, heartbeat ran despite scoring failing
    assert res == {"reg-index": 0, "scoring": -1, "heartbeat": 3}
