#!/usr/bin/env python3
"""hope-validator-daemon — the consolidated long-running SN21 validator.

ONE long-running process that drives the three self-idempotent validator tools
on a fixed cadence, replacing the separate scoring cron + heartbeat cron + the
manual reg-index recovery step:

  1. reg-index tick  — `python -m scripts.build_reg_index --once` extends the
     registration index from its persisted checkpoint (against an ARCHIVE RPC).
     Self-checkpointing, so each tick scans only new blocks.

  2. scoring         — `hope-validator --release auto` resolves the latest
     CLOSED epoch (P2: discover_scoreable_release) and scores it. The on-chain
     `already_scored` guard makes repeat runs no-ops, so each closed epoch is
     scored exactly once — no daemon-side epoch bookkeeping needed.

  3. weight cycle    — `hope-validator-heartbeat` self-throttles on the
     <=1500-block gap and re-asserts the validator's last on-chain weights.
     Re-asserting the last weights IS "pull the last window" when a scoring
     window is missed, and keeps the validator above the activity cutoff.

Each tool runs as a SUBPROCESS so its bittensor/substrate state dies with it
(the RSS-isolation pattern used elsewhere in this package), and a failure in
one tool never blocks the others. The daemon holds no scoring/weights state of
its own — every tool is independently idempotent — which keeps this supervisor
small and auditable, exactly the "one long-running script" validators asked for.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("hope-validator-daemon")

# (name, argv, env_overrides) — env_overrides is merged onto os.environ for that
# subprocess only (the reg-index builder needs an ARCHIVE RPC, while scoring and
# heartbeat read recent state from the default/fast endpoint).
Command = tuple


@dataclass
class DaemonConfig:
    network: str = "finney"
    netuid: int = 21
    wallet_name: str = "sn21_validator"
    wallet_hotkey: str = "default"
    reg_index: str = ""                 # path to sn21-reg-index.json (persistent)
    reg_index_archive_url: str = ""     # SN21_SUBTENSOR_URL for the builder
    reg_index_cold_start_lookback_blocks: int = 0  # >0 bounds a no-checkpoint scan
    reg_index_max_blocks_per_tick: int = 0  # >0 caps each tick's scan (heartbeat safety)
    reg_index_staleness_alarm_blocks: int = 2000  # warn if head-last_scanned exceeds this
    skip_reg_index_head: bool = False   # disable the fast lite-node head refresh
    skip_staleness_alarm: bool = False  # disable the reg-index staleness alarm
    role: str = "miner"
    ed25519_key_file: str = ""          # scorer --ed25519-key-file (9.C inner_sig)
    archive_tier_2_urls: tuple = ()     # scorer --archive-tier-2 (miner AES_ct fetch)
    interval_seconds: float = 1800.0
    # Per-tool wall-clock ceilings. A tool that exceeds its ceiling is KILLED so
    # it can never block the rest of the tick — most importantly, so a stalled
    # reg-index/scoring (the public archive drops connections and --reconnect can
    # spin) can never starve the heartbeat and let the validator drift toward the
    # activity cutoff. 0 = no ceiling.
    heartbeat_timeout_seconds: float = 240.0
    reg_index_timeout_seconds: float = 1200.0
    reg_index_head_timeout_seconds: float = 420.0  # lite-node 256-hotkey sweep
    staleness_alarm_timeout_seconds: float = 120.0  # lite-node single head read
    scoring_timeout_seconds: float = 1800.0
    report_timeout_seconds: float = 300.0
    # Leaderboard publish: after a successful scoring tick writes its artifact,
    # POST it to the CMS (the step the old scoring cron ran separately). Idempotent
    # via --skip-if-posted so a published/frozen epoch isn't re-posted every tick.
    leaderboard_artifact_dir: str = ""  # SN21_EPOCH_ARTIFACT_DIR; "" disables the report step
    ignore_already_scored: bool = False
    heartbeat_dry_run: bool = False
    skip_reg_index: bool = False
    skip_scoring: bool = False
    skip_report: bool = False
    skip_heartbeat: bool = False


def build_commands(cfg: DaemonConfig) -> list:
    """Assemble the ordered (name, argv, env_overrides, timeout) tuples for one tick.

    Order is reg-index -> scoring -> HEARTBEAT LAST. Scoring is the only tool that
    commits *fresh* weights, and the heartbeat merely re-asserts the last weights
    when the activity gap grows. Running scoring before the heartbeat means:

      * on a weekly-close tick, scoring commits its weights first, then the
        heartbeat sees a freshly-reset gap (< threshold) and skips — so the two
        never compete for the same `set_weights` slot;
      * on every other tick, scoring is an `already_scored` no-op and the
        heartbeat backstops the activity floor as usual.

    Heartbeat-FIRST was tried to stop a stalled reg-index from starving the
    heartbeat, but it made the heartbeat grab the weight slot and then rate-limit
    (180-block) scoring's own commit, aborting the weekly run (no artifact / no
    leaderboard post). The per-tool *timeouts* below already prevent a stalled
    tool from starving the heartbeat, so heartbeat-last is both safe and
    collision-free. The heartbeat self-throttles below its gap threshold, so it's
    a no-op on the vast majority of ticks regardless of position.
    """
    cmds: list = []

    # Staleness alarm FIRST — ground-truth health for the reg-index, the analogue
    # of the LastUpdate-gap check for weights. It reads the builder's checkpoint
    # and chain head (lite node) and screams if the event scan has fallen too far
    # behind; it runs even when reg-index scanning is skipped/broken, so a frozen
    # index (2026-06-04 → unnoticed 11 days) can never again hide behind green logs.
    if not cfg.skip_staleness_alarm and cfg.reg_index:
        argv = [sys.executable, "-m", "scripts.check_reg_index_staleness",
                "--network", cfg.network, "--index", cfg.reg_index,
                "--threshold-blocks", str(cfg.reg_index_staleness_alarm_blocks)]
        cmds.append(("staleness-alarm", argv, {},
                     cfg.staleness_alarm_timeout_seconds))

    if not cfg.skip_reg_index and cfg.reg_index:
        # Fast, reliable FIRST: a head-only refresh reads each metagraph hotkey's
        # CURRENT commitment from the LITE node (no archive) and merges any valid
        # reg-v1 into the index. A miner who just registered hasn't overwritten
        # their reg-v1, so this one-pass sweep catches them every tick — keeping
        # the index current even when the archive block-scan below is slow or
        # stuck (which silently froze the index for days in the field). Merge-only,
        # so it never drops the block-scanned baseline.
        if not cfg.skip_reg_index_head:
            head_argv = [sys.executable, "-m", "scripts.refresh_reg_index_head",
                         "--network", cfg.network, "--netuid", str(cfg.netuid),
                         "--role", cfg.role, "--index", cfg.reg_index]
            cmds.append(("reg-index-head", head_argv, {},
                         cfg.reg_index_head_timeout_seconds))

        # Archive block-scan for historical completeness (slow; bounded per tick
        # so it can't starve the heartbeat). No longer the critical path for
        # catching new miners — the head refresh above handles that.
        env = {}
        if cfg.reg_index_archive_url:
            env["SN21_SUBTENSOR_URL"] = cfg.reg_index_archive_url
        argv = [sys.executable, "-m", "scripts.build_reg_index",
                "--network", cfg.network, "--netuid", str(cfg.netuid),
                "--role", cfg.role, "--index", cfg.reg_index, "--reconnect"]
        if cfg.reg_index_cold_start_lookback_blocks > 0:
            argv += ["--cold-start-lookback-blocks",
                     str(cfg.reg_index_cold_start_lookback_blocks)]
        if cfg.reg_index_max_blocks_per_tick > 0:
            argv += ["--max-blocks-per-pass", str(cfg.reg_index_max_blocks_per_tick)]
        cmds.append(("reg-index", argv, env, cfg.reg_index_timeout_seconds))

    if not cfg.skip_scoring:
        argv = ["hope-validator", "--release", "auto",
                "--network", cfg.network, "--netuid", str(cfg.netuid),
                "--wallet-name", cfg.wallet_name, "--wallet-hotkey", cfg.wallet_hotkey]
        if cfg.reg_index:
            argv += ["--reg-index-prebuilt", cfg.reg_index]
        if cfg.ed25519_key_file:
            argv += ["--ed25519-key-file", cfg.ed25519_key_file]
        for url in cfg.archive_tier_2_urls:
            argv += ["--archive-tier-2", url]
        if cfg.ignore_already_scored:
            argv += ["--ignore-already-scored"]
        cmds.append(("scoring", argv, {}, cfg.scoring_timeout_seconds))

    # Publish the leaderboard to the CMS — the step the old scoring cron ran
    # after hope-validator. Posts the NEWEST artifact scoring just wrote;
    # --skip-if-posted makes it a no-op when nothing new was produced, so a
    # published/frozen epoch is never re-posted as a -COR correction every tick.
    # HTTP POST only (no chain) → runs after scoring, before the heartbeat,
    # without touching the weight cycle.
    if not cfg.skip_report and cfg.leaderboard_artifact_dir:
        argv = [sys.executable, "-m", "scripts.post_epoch_report",
                "--artifact-dir", cfg.leaderboard_artifact_dir, "--skip-if-posted"]
        cmds.append(("report", argv, {}, cfg.report_timeout_seconds))

    if not cfg.skip_heartbeat:
        argv = ["hope-validator-heartbeat",
                "--network", cfg.network, "--netuid", str(cfg.netuid),
                "--wallet-name", cfg.wallet_name, "--wallet-hotkey", cfg.wallet_hotkey]
        if cfg.heartbeat_dry_run:
            argv += ["--dry-run"]
        cmds.append(("heartbeat", argv, {}, cfg.heartbeat_timeout_seconds))

    return cmds


# Exit code recorded when a tool is killed for exceeding its wall-clock ceiling
# (mirrors the shell convention 128+SIGKILL).
TIMEOUT_RC = -137


def _default_runner(name: str, argv: list, env_overrides: dict,
                    timeout: Optional[float] = None) -> int:
    """Run one tool as a subprocess; return its exit code.

    On timeout the child is killed (subprocess.run terminates it) and TIMEOUT_RC
    is returned so the tick continues with the next tool. -1 on launch error.
    """
    env = {**os.environ, **(env_overrides or {})}
    to = timeout if (timeout and timeout > 0) else None
    try:
        proc = subprocess.run(argv, env=env, timeout=to)
        return int(proc.returncode)
    except subprocess.TimeoutExpired:
        logger.error("%s: exceeded %.0fs ceiling; killed (will retry next tick)",
                     name, to)
        return TIMEOUT_RC
    except Exception as exc:  # FileNotFoundError, etc.
        logger.error("%s: failed to launch (%s)", name, exc)
        return -1


def run_tick(cfg: DaemonConfig,
             runner: Callable[..., int] = _default_runner) -> dict:
    """Run one supervisor pass: each tool in order, isolated from the others.

    A tool raising, exiting non-zero, or exceeding its timeout is logged and
    recorded but never blocks the rest of the tick. Returns {tool_name: exit_code}.
    """
    results: dict = {}
    for name, argv, env, timeout in build_commands(cfg):
        logger.info("tick: running %s", name)
        try:
            rc = runner(name, argv, env, timeout)
        except Exception as exc:
            logger.exception("%s raised: %s", name, exc)
            rc = -1
        results[name] = rc
        if rc == 0:
            logger.info("tick: %s ok", name)
        else:
            logger.warning("tick: %s exited rc=%s (continuing)", name, rc)
    return results


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", default=os.environ.get("BT_NETWORK", "finney"))
    p.add_argument("--netuid", type=int, default=int(os.environ.get("NETUID", "21")))
    p.add_argument("--wallet-name", default=os.environ.get("WALLET_NAME", "sn21_validator"))
    p.add_argument("--wallet-hotkey", default=os.environ.get("HOTKEY_NAME", "default"))
    p.add_argument("--reg-index", default=os.environ.get("SN21_REG_INDEX_PATH", ""),
                   help="Path to the persistent reg-index JSON (also fed to the scorer).")
    p.add_argument("--reg-index-archive-url",
                   default=os.environ.get("SN21_REG_INDEX_ARCHIVE_URL", ""),
                   help="Archive RPC for the reg-index builder (SN21_SUBTENSOR_URL "
                        "for that subprocess only). Required for reg-index scanning.")
    p.add_argument("--reg-index-cold-start-lookback-blocks", type=int,
                   default=int(os.environ.get("SN21_REG_INDEX_COLD_START_LOOKBACK_BLOCKS", "0")),
                   help="When the reg-index has no checkpoint yet, bound the first "
                        "scan to this many blocks back from head (0 = builder default). "
                        "Prevents a multi-hour cold-start scan on a slow archive.")
    p.add_argument("--role", default="miner")
    p.add_argument("--reg-index-max-blocks-per-tick", type=int,
                   default=int(os.environ.get("SN21_REG_INDEX_MAX_BLOCKS_PER_TICK", "0")),
                   help="Cap each tick's reg-index scan to N blocks so it never "
                        "blocks the heartbeat on a slow archive (0 = unbounded). "
                        "On the public archive (~8s/block) ~200 keeps the tick short.")
    p.add_argument("--ed25519-key-file", default=os.environ.get("SN21_ED25519_KEY_FILE", ""),
                   help="Validator ed25519 PEM for the scorer's 9.C inner_sig "
                        "(passed to hope-validator --ed25519-key-file).")
    p.add_argument("--archive-tier-2", action="append", default=None,
                   help="Tier-2 archive base URL(s) for the scorer to fetch miner "
                        "AES_ct. Repeatable; env ARCHIVE_TIER_2_URLS (space/comma "
                        "separated) is used if no flag is given.")
    p.add_argument("--interval-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_INTERVAL_SECS", "1800")))
    p.add_argument("--heartbeat-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_HEARTBEAT_TIMEOUT_SECS", "240")),
                   help="Kill the heartbeat tool if it runs longer than this (0 = no limit).")
    p.add_argument("--reg-index-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_REG_INDEX_TIMEOUT_SECS", "1200")),
                   help="Kill the reg-index tool if it runs longer than this; the "
                        "checkpoint persists so the next tick resumes (0 = no limit).")
    p.add_argument("--reg-index-head-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_REG_INDEX_HEAD_TIMEOUT_SECS", "420")),
                   help="Kill the fast head-only reg-index refresh if it runs longer "
                        "than this (lite-node 256-hotkey sweep; 0 = no limit).")
    p.add_argument("--scoring-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_SCORING_TIMEOUT_SECS", "1800")),
                   help="Kill the scoring tool if it runs longer than this; it is "
                        "idempotent so the next tick re-runs (0 = no limit).")
    p.add_argument("--report-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_REPORT_TIMEOUT_SECS", "300")),
                   help="Kill the CMS leaderboard POST if it runs longer than this (0 = no limit).")
    p.add_argument("--leaderboard-artifact-dir",
                   default=os.environ.get("SN21_EPOCH_ARTIFACT_DIR", "/tmp/sn21-epoch-artifacts"),
                   help="Dir where the scorer writes its epoch artifact; the daemon "
                        "POSTs the newest one to the CMS after scoring (idempotently, "
                        "via --skip-if-posted). Active only when SN21_LEADERBOARD_REPORTER=1.")
    p.add_argument("--ignore-already-scored", action="store_true")
    p.add_argument("--heartbeat-dry-run", action="store_true",
                   default=os.environ.get("SN21_HEARTBEAT_DRY_RUN", "0") == "1")
    p.add_argument("--skip-reg-index", action="store_true")
    p.add_argument("--skip-reg-index-head", action="store_true",
                   default=os.environ.get("SN21_SKIP_REG_INDEX_HEAD", "0") == "1",
                   help="Disable the fast lite-node head refresh (leave only the "
                        "archive block-scan).")
    p.add_argument("--reg-index-staleness-alarm-blocks", type=int,
                   default=int(os.environ.get("SN21_REG_INDEX_STALENESS_ALARM_BLOCKS", "2000")),
                   help="Warn loudly when chain head exceeds the reg-index's "
                        "last_scanned_block by more than this many blocks (a frozen "
                        "event scan silently DQs new miners).")
    p.add_argument("--skip-staleness-alarm", action="store_true",
                   default=os.environ.get("SN21_SKIP_STALENESS_ALARM", "0") == "1",
                   help="Disable the reg-index staleness alarm tick step.")
    p.add_argument("--staleness-alarm-timeout-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_STALENESS_ALARM_TIMEOUT_SECS", "120")),
                   help="Kill the staleness alarm if it runs longer than this "
                        "(lite-node single head read; 0 = no limit).")
    p.add_argument("--skip-scoring", action="store_true")
    p.add_argument("--skip-report", action="store_true")
    p.add_argument("--skip-heartbeat", action="store_true")
    p.add_argument("--once", action="store_true",
                   help="Run a single tick and exit (manual run / smoke test).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    cfg = DaemonConfig(
        network=args.network, netuid=args.netuid,
        wallet_name=args.wallet_name, wallet_hotkey=args.wallet_hotkey,
        reg_index=args.reg_index, reg_index_archive_url=args.reg_index_archive_url,
        reg_index_cold_start_lookback_blocks=args.reg_index_cold_start_lookback_blocks,
        reg_index_max_blocks_per_tick=args.reg_index_max_blocks_per_tick,
        reg_index_staleness_alarm_blocks=args.reg_index_staleness_alarm_blocks,
        skip_reg_index_head=args.skip_reg_index_head,
        skip_staleness_alarm=args.skip_staleness_alarm,
        staleness_alarm_timeout_seconds=args.staleness_alarm_timeout_seconds,
        role=args.role,
        ed25519_key_file=args.ed25519_key_file,
        archive_tier_2_urls=tuple(
            args.archive_tier_2 if args.archive_tier_2 is not None
            else [u for u in os.environ.get("ARCHIVE_TIER_2_URLS", "").replace(",", " ").split() if u]
        ),
        interval_seconds=args.interval_seconds,
        heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
        reg_index_timeout_seconds=args.reg_index_timeout_seconds,
        reg_index_head_timeout_seconds=args.reg_index_head_timeout_seconds,
        scoring_timeout_seconds=args.scoring_timeout_seconds,
        report_timeout_seconds=args.report_timeout_seconds,
        leaderboard_artifact_dir=args.leaderboard_artifact_dir,
        ignore_already_scored=args.ignore_already_scored,
        heartbeat_dry_run=args.heartbeat_dry_run,
        skip_reg_index=args.skip_reg_index, skip_scoring=args.skip_scoring,
        skip_report=args.skip_report, skip_heartbeat=args.skip_heartbeat,
    )

    # The report step publishes to the CMS only when the leaderboard reporter is
    # enabled (the same gate the scoring cron used + that makes the scorer write
    # the artifact). Without it, skip — a daemon with no reporter creds shouldn't
    # POST every tick.
    from hope.reporting.flags import reporter_enabled
    if not cfg.skip_report and not reporter_enabled():
        logger.info("leaderboard reporter disabled (SN21_LEADERBOARD_REPORTER!=1); "
                    "skipping the CMS report step")
        cfg.skip_report = True

    if cfg.skip_reg_index is False and not cfg.reg_index:
        logger.warning("no --reg-index path set; skipping the reg-index tick "
                       "(scoring will run without a fresh prebuilt index)")
        cfg.skip_reg_index = True

    if args.once:
        run_tick(cfg)
        return 0

    # Graceful shutdown on SIGTERM (Render stops workers with SIGTERM).
    stopping = {"flag": False}

    def _stop(signum, _frame):
        logger.info("received signal %s; finishing current tick then exiting", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("daemon start: netuid=%d interval=%.0fs reg_index=%s",
                cfg.netuid, cfg.interval_seconds, cfg.reg_index or "<none>")
    while not stopping["flag"]:
        run_tick(cfg)
        if stopping["flag"]:
            break
        # Sleep in short slices so a signal interrupts promptly.
        slept = 0.0
        while slept < cfg.interval_seconds and not stopping["flag"]:
            time.sleep(min(2.0, cfg.interval_seconds - slept))
            slept += 2.0
    logger.info("daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
