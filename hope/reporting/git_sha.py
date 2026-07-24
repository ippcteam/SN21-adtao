"""Git commit SHA capture for leaderboard reports.

Reports carry `scoring_formula_version` (a human-readable constant
mirroring the spec doc) AND `scoring_formula_commit` (the exact code
revision). Together they let any reader pin both the policy version
and the executable that produced the numbers.

The SHA is captured at scoring-run time via `git rev-parse HEAD` against
the current working tree. The validator host must therefore have:
  - `git` available on PATH, and
  - the repository checked out at the path Python imports `hope` from.

Both are true under the canonical Render / systemd deploys defined in
`deploy/`. Containerised deploys that strip the `.git` directory must
bake the SHA into a build-time env var and have callers read it instead;
see `current_commit_sha(fallback_env=...)`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_GIT_REV_TIMEOUT_SECONDS = 5


def current_commit_sha(
    *,
    repo_root: Path | None = None,
    fallback_env: str | None = None,
) -> str:
    """Return the full 40-character commit SHA of HEAD.

    Args:
        repo_root: directory to run `git rev-parse` against. Defaults to
            the directory containing the `hope` package — i.e. the repo
            root when the package is installed via `pip install -e .`.
        fallback_env: name of an env var to read the SHA from if `git` is
            unavailable. Set this on container builds that strip `.git`.

    Raises:
        RuntimeError: when `git` is unavailable and no fallback env var
            is set (or the named env var is empty).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_REV_TIMEOUT_SECONDS,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if fallback_env is None:
            raise RuntimeError(
                f"git rev-parse HEAD failed in {repo_root} and no fallback "
                f"env var was provided: {e}"
            ) from e
        sha = os.environ.get(fallback_env, "").strip()
        if not sha:
            raise RuntimeError(
                f"git rev-parse HEAD failed and fallback env {fallback_env} "
                f"is unset or empty: {e}"
            ) from e
        return sha

    sha = result.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"unexpected git rev-parse output: {sha!r}")
    return sha
