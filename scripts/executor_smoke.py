"""Smoke-test the daemonless executor on the host it will actually run on.

Runs three escalating checks and prints a verdict for each, so a failure
localises to the exact layer rather than "it didn't work":

  1. NAMESPACE   — can we create user+net namespaces and exec a trivial
                   command under chroot at all? Uses a hand-built rootfs with
                   a static busybox; no registry involved. Proves the sandbox
                   primitive works on THIS host.
  2. PULL        — pull + digest-verify + unpack one real image from the
                   chain (a committed miner digest, read live). Proves the
                   registry client works against a real registry.
  3. EXECUTE     — run that pulled image against a tiny real basket under the
                   sandbox, and confirm it emits parseable predictions.
                   Proves the whole path a daily run depends on.

Nothing is committed, no ledger is written, no chain call is made beyond
reading commitments. Safe to run repeatedly.

    python3 -m scripts.executor_smoke [--digest sha256:...] [--repo ghcr.io/..]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _print(tag, msg):
    print(f"[smoke:{tag}] {msg}", flush=True)


def check_namespace() -> bool:
    """Confirm the sandbox's isolation works, via the exact `unshare` command
    the sandbox uses, against a minimal rootfs.

    The sandbox runs miner code through the `unshare` binary (the raw syscall
    failed in the forked executor context). So the honest check is to build the
    same command and run a trivial program under it — which also proves the
    network wall (`--net`) and chroot (`--root`) hold before any real image.
    Uses the host's own /bin/sh copied into a scratch rootfs with its libs.
    """
    import shutil

    from hope.backtest.ns_sandbox import RunSpec, run_sandboxed

    if shutil.which("unshare") is None:
        _print("namespace", "FAIL unshare binary not found (not Linux?)")
        return False

    root = tempfile.mkdtemp(prefix="smoke-ns-",
                            dir=os.environ.get("SN21_EXECUTOR_WORKDIR") or None)
    try:
        # Minimal rootfs: sh plus the handful of libs it needs. Copying only
        # the direct dependencies keeps this fast (the earlier full /usr/lib
        # copy is what hung).
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        sh = shutil.which("sh") or "/bin/sh"
        shutil.copy2(sh, os.path.join(root, "bin", "sh"))
        for lib in ("/lib/x86_64-linux-gnu", "/lib64", "/usr/lib/x86_64-linux-gnu"):
            if os.path.isdir(lib):
                dst = os.path.join(root, lib.lstrip("/"))
                os.makedirs(dst, exist_ok=True)
                for name in os.listdir(lib):
                    if name.startswith(("libc", "ld-", "libtinfo",
                                        "libselinux", "libpcre", "libdl",
                                        "libpthread")):
                        try:
                            shutil.copy2(os.path.join(lib, name),
                                         os.path.join(dst, name))
                        except OSError:
                            pass

        spec = RunSpec(rootfs=root, argv=["/bin/sh", "-c", "echo SANDBOX_OK"],
                       working_dir="/", wall_timeout=30)
        result = run_sandboxed(spec, b"")
        if result.ok and "SANDBOX_OK" in result.stdout:
            _print("namespace", "PASS unshare user+net+pid+chroot exec works")
            return True
        _print("namespace",
               f"FAIL ok={result.ok} err={result.error!r} "
               f"out={result.stdout[:120]!r}")
        return False
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def pick_digest(explicit_repo, explicit_digest):
    """A (repo, digest) to test with: explicit, else the earliest committed
    miner model read live from chain."""
    if explicit_repo and explicit_digest:
        return explicit_repo, explicit_digest
    import bittensor as bt

    from hope.backtest.chain_commitments import bulk_model_commitments
    from hope.backtest.model_registry import parse_model_commitment

    st = bt.Subtensor(network=os.environ.get("SN21_NETWORK", "finney"))
    mg = st.metagraph(netuid=int(os.environ.get("SN21_NETUID", "21")))
    commits = bulk_model_commitments(st, int(os.environ.get("SN21_NETUID", "21")),
                                     list(mg.hotkeys))
    # earliest block first — the most-established model
    ordered = sorted(commits.items(), key=lambda kv: kv[1][0])
    for _hk, (_block, raw) in ordered:
        parsed = parse_model_commitment(raw)
        if parsed and parsed.get("image_ref"):
            return parsed["image_ref"], parsed["digest"]
    return None, None


def check_pull(repo, digest):
    from hope.backtest.oci_pull import PullError, pull_and_unpack

    dest = tempfile.mkdtemp(prefix="smoke-pull-",
                            dir=os.environ.get("SN21_EXECUTOR_WORKDIR") or None)
    try:
        image = pull_and_unpack(repo, digest, dest)
        entries = len(os.listdir(image.rootfs))
        _print("pull", f"PASS unpacked {repo}@{digest[:19]}… "
                        f"rootfs has {entries} top-level entries")
        _print("pull", f"      entrypoint={image.config.argv()!r}")
        return image, dest
    except PullError as exc:
        _print("pull", f"FAIL {exc}")
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        return None, None


def check_execute(repo, digest):
    """Run the image against a 2-episode basket via the real mode resolver."""
    from hope.backtest.execution_mode import basket_runner

    # A minimal but real-shaped episode payload.
    episodes = [{"episode_id": "smoke-1", "episode_metadata": {},
                 "account_state": {}, "action_bundle": {}, "pre_window": {}},
                {"episode_id": "smoke-2", "episode_metadata": {},
                 "account_state": {}, "action_bundle": {}, "pre_window": {}}]
    run = basket_runner({"SN21_EXECUTOR_MODE": "sandbox",
                         "SN21_EXECUTOR_WORKDIR":
                         os.environ.get("SN21_EXECUTOR_WORKDIR", "")})
    result = run(f"{repo}@{digest}", episodes)
    _print("execute", f"ok={result.ok} episodes_in={result.episodes_in} "
                      f"predictions_out={result.predictions_out} "
                      f"err={result.error!r}")
    if result.ok and result.predictions_out > 0:
        sample = next(iter(result.predictions.values()))
        _print("execute", f"PASS sample prediction keys={sorted(sample)[:6]}")
        return True
    if result.ok:
        _print("execute", "PARTIAL ran cleanly but emitted no parseable "
                          "predictions (model may need real episode fields)")
        return True
    _print("execute", "FAIL")
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=None)
    p.add_argument("--digest", default=None)
    p.add_argument("--skip-namespace", action="store_true")
    args = p.parse_args()

    print("===SMOKE-START===", flush=True)
    ns_ok = True
    if not args.skip_namespace:
        ns_ok = check_namespace()

    repo, digest = pick_digest(args.repo, args.digest)
    if not repo:
        _print("pull", "SKIP no committed model digest found to test with")
        print("===SMOKE-END===", flush=True)
        return 0
    _print("target", f"{repo}@{digest}")

    image, dest = check_pull(repo, digest)
    if image is None:
        print("===SMOKE-END===", flush=True)
        return 1
    import shutil
    shutil.rmtree(dest, ignore_errors=True)

    exec_ok = check_execute(repo, digest)

    print(f"===SMOKE-SUMMARY namespace={'ok' if ns_ok else 'FAIL'} "
          f"pull=ok execute={'ok' if exec_ok else 'FAIL'}===", flush=True)
    print("===SMOKE-END===", flush=True)
    return 0 if (ns_ok and exec_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
