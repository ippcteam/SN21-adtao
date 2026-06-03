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
    role: str = "miner"
    interval_seconds: float = 1800.0
    ignore_already_scored: bool = False
    heartbeat_dry_run: bool = False
    skip_reg_index: bool = False
    skip_scoring: bool = False
    skip_heartbeat: bool = False


def build_commands(cfg: DaemonConfig) -> list:
    """Assemble the ordered (name, argv, env_overrides) tuples for one tick."""
    cmds: list = []

    if not cfg.skip_reg_index and cfg.reg_index:
        env = {}
        if cfg.reg_index_archive_url:
            env["SN21_SUBTENSOR_URL"] = cfg.reg_index_archive_url
        cmds.append((
            "reg-index",
            [sys.executable, "-m", "scripts.build_reg_index",
             "--network", cfg.network, "--netuid", str(cfg.netuid),
             "--role", cfg.role, "--index", cfg.reg_index, "--reconnect"],
            env,
        ))

    if not cfg.skip_scoring:
        argv = ["hope-validator", "--release", "auto",
                "--network", cfg.network, "--netuid", str(cfg.netuid),
                "--wallet-name", cfg.wallet_name, "--wallet-hotkey", cfg.wallet_hotkey]
        if cfg.reg_index:
            argv += ["--reg-index-prebuilt", cfg.reg_index]
        if cfg.ignore_already_scored:
            argv += ["--ignore-already-scored"]
        cmds.append(("scoring", argv, {}))

    if not cfg.skip_heartbeat:
        argv = ["hope-validator-heartbeat",
                "--network", cfg.network, "--netuid", str(cfg.netuid),
                "--wallet-name", cfg.wallet_name, "--wallet-hotkey", cfg.wallet_hotkey]
        if cfg.heartbeat_dry_run:
            argv += ["--dry-run"]
        cmds.append(("heartbeat", argv, {}))

    return cmds


def _default_runner(name: str, argv: list, env_overrides: dict) -> int:
    """Run one tool as a subprocess; return its exit code (-1 on launch error)."""
    env = {**os.environ, **(env_overrides or {})}
    try:
        proc = subprocess.run(argv, env=env)
        return int(proc.returncode)
    except Exception as exc:  # FileNotFoundError, etc.
        logger.error("%s: failed to launch (%s)", name, exc)
        return -1


def run_tick(cfg: DaemonConfig,
             runner: Callable[[str, list, dict], int] = _default_runner) -> dict:
    """Run one supervisor pass: each tool in order, isolated from the others.

    A tool raising or exiting non-zero is logged and recorded but never blocks
    the rest of the tick. Returns {tool_name: exit_code}.
    """
    results: dict = {}
    for name, argv, env in build_commands(cfg):
        logger.info("tick: running %s", name)
        try:
            rc = runner(name, argv, env)
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
    p.add_argument("--role", default="miner")
    p.add_argument("--interval-seconds", type=float,
                   default=float(os.environ.get("SN21_DAEMON_INTERVAL_SECS", "1800")))
    p.add_argument("--ignore-already-scored", action="store_true")
    p.add_argument("--heartbeat-dry-run", action="store_true",
                   default=os.environ.get("SN21_HEARTBEAT_DRY_RUN", "0") == "1")
    p.add_argument("--skip-reg-index", action="store_true")
    p.add_argument("--skip-scoring", action="store_true")
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
        role=args.role, interval_seconds=args.interval_seconds,
        ignore_already_scored=args.ignore_already_scored,
        heartbeat_dry_run=args.heartbeat_dry_run,
        skip_reg_index=args.skip_reg_index, skip_scoring=args.skip_scoring,
        skip_heartbeat=args.skip_heartbeat,
    )

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
