"""One place that decides HOW a basket is executed: docker, or the daemonless
namespace sandbox.

Every caller that runs an image — the admission gate, the shadow day — asks
this module for a runner rather than hardcoding one. The mode is configuration
(`SN21_EXECUTOR_MODE`), so the same code runs on a docker host and on the
Render worker that has no daemon, and the choice is made in exactly one place
instead of drifting between call sites.

The returned runner always yields a `container_runner.RunResult`, so nothing
downstream depends on which executor ran.
"""

from __future__ import annotations

import os

MODE_ENV = "SN21_EXECUTOR_MODE"
MODE_DOCKER = "docker"
MODE_SANDBOX = "sandbox"       # daemonless namespaces (Render)

# The scratch dir where the sandbox unpacks images. On Render this must be the
# mounted disk, not the small ephemeral root.
WORKDIR_ENV = "SN21_EXECUTOR_WORKDIR"


def executor_mode(environ=os.environ) -> str:
    mode = (environ.get(MODE_ENV) or MODE_DOCKER).strip().lower()
    return mode if mode in (MODE_DOCKER, MODE_SANDBOX) else MODE_DOCKER


def basket_runner(environ=os.environ):
    """A callable (pinned_ref, episodes) -> RunResult for the active mode.

    `pinned_ref` is the shadow model's `repo@digest`. Docker consumes it whole;
    the sandbox splits it, because pulling without a daemon needs the repo and
    the digest separately.
    """
    mode = executor_mode(environ)
    if mode == MODE_SANDBOX:
        from hope.backtest.local_executor import run_basket_local, split_pinned_ref
        workdir = environ.get(WORKDIR_ENV) or None

        def run(pinned_ref, episodes):
            repo, digest = split_pinned_ref(pinned_ref)
            return run_basket_local(repo, digest, episodes, workdir_root=workdir)
        return run

    from hope.backtest.container_runner import run_basket_docker

    def run(pinned_ref, episodes):
        return run_basket_docker(pinned_ref, episodes)
    return run
