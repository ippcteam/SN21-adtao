"""Image intake — miner's committed digest -> pulled, verified, gated image.

The missing operational step between the registry (WHAT was committed, on
chain) and the gate service (does it beat the baseline): actually fetching
the container a miner published and proving the local bytes are the bytes
they committed to. The commitment is the trust anchor — a registry that
serves anything other than the committed sha256 must fail the intake
loudly, never silently run substituted code.

Two safeguards stack:
  1. We pull BY DIGEST (`repo@sha256:...`), so the docker daemon itself
     content-addresses the fetch — a registry cannot substitute bytes
     without failing the pull.
  2. We still inspect the local image's RepoDigests afterwards and require
     the committed digest to appear — belt and braces, and the check that
     catches a poisoned local cache or a tag-based pull sneaking in via a
     future refactor.

Untrusted-input discipline: image refs and digests originate from miners'
chain commitments. Both are validated against strict grammars BEFORE any
docker interaction, and all subprocess calls use argument lists — nothing
miner-controlled is ever interpolated into a shell string.

Pure core with injected I/O (puller/inspector/gate_runner) so unit tests
never need a docker daemon; the default implementations shell out to the
docker CLI exactly like container_runner does.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

# Same strictness as model_registry.parse_model_commitment: 64 lowercase hex.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Conservative subset of the OCI reference grammar: a lowercase host
# segment (optionally with :port), then lowercase path segments.
# Deliberately REJECTS '@' (we append the committed digest ourselves — a
# ref smuggling its own digest must not reach docker), tags, and any
# whitespace/shell metacharacters by construction of the character class.
_IMAGE_REF_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,127}(:[0-9]{1,5})?(/[a-z0-9][a-z0-9._-]{0,127})*$")

# Statuses an intake can end in (invalid_commitment = malformed ref/digest,
# rejected before docker is ever invoked).
STATUS_ADMITTED = "admitted"
STATUS_REJECTED_GATE = "rejected_gate"
STATUS_REJECTED_DIGEST_MISMATCH = "rejected_digest_mismatch"
STATUS_PULL_FAILED = "pull_failed"
STATUS_INVALID_COMMITMENT = "invalid_commitment"


@dataclass(frozen=True)
class ModelCommitment:
    """One miner's submission as read off-chain: who, where, what bytes."""
    hotkey: str
    image_ref: str      # repository path the miner published to (no tag/@)
    digest: str         # the on-chain committed sha256:<64 hex>


@dataclass(frozen=True)
class PullResult:
    pulled: bool
    verified: bool
    local_ref: Optional[str] = None   # the digest-pinned ref to run
    error: Optional[str] = None


@dataclass(frozen=True)
class IntakeResult:
    hotkey: str
    digest: str
    status: str
    detail: Optional[str] = None
    gate: Optional[dict] = None       # gate-service result when it ran


def validate_commitment(c: ModelCommitment) -> Optional[str]:
    """Return a rejection reason for a malformed commitment, else None.
    Runs BEFORE any docker interaction — miner-controlled strings that fail
    the grammar never reach a subprocess."""
    if not _DIGEST_RE.match(c.digest or ""):
        return f"malformed digest: {c.digest!r}"
    if not c.image_ref or len(c.image_ref) > 255 \
            or not _IMAGE_REF_RE.match(c.image_ref):
        return f"malformed image ref: {c.image_ref!r}"
    return None


def _default_puller(pinned_ref: str) -> tuple[bool, str]:
    """docker pull by digest-pinned ref. Argument list, never a shell."""
    try:
        proc = subprocess.run(["docker", "pull", pinned_ref],
                              capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "pull timeout>600s"
    except FileNotFoundError:
        return False, "docker_not_available"
    if proc.returncode != 0:
        return False, f"exit={proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}"
    return True, ""


def _default_inspector(pinned_ref: str) -> list[str]:
    """RepoDigests of the local image; [] when inspect fails."""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", pinned_ref,
             "--format", "{{range .RepoDigests}}{{.}}\n{{end}}"],
            capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.decode(errors="replace").splitlines()
            if ln.strip()]


def pull_by_digest(image_ref: str, expected_digest: str,
                   puller: Optional[Callable[[str], tuple[bool, str]]] = None,
                   inspector: Optional[Callable[[str], list[str]]] = None,
                   ) -> PullResult:
    """Pull the image digest-pinned and verify the local RepoDigests carry
    the committed digest. Validation of both inputs happens here too, so
    direct callers get the same before-docker rejection as intake_model."""
    c = ModelCommitment(hotkey="", image_ref=image_ref, digest=expected_digest)
    reason = validate_commitment(c)
    if reason:
        return PullResult(pulled=False, verified=False, error=reason)

    pinned = f"{image_ref}@{expected_digest}"
    ok, err = (puller or _default_puller)(pinned)
    if not ok:
        return PullResult(pulled=False, verified=False, error=err)

    repo_digests = (inspector or _default_inspector)(pinned)
    if not any(rd.endswith(f"@{expected_digest}") for rd in repo_digests):
        return PullResult(
            pulled=True, verified=False,
            error=f"digest mismatch: committed {expected_digest} not in "
                  f"local RepoDigests {repo_digests!r}")
    return PullResult(pulled=True, verified=True, local_ref=pinned)


def intake_model(commitment: ModelCommitment,
                 gate_runner: Callable[[str], dict],
                 puller: Optional[Callable[[str], tuple[bool, str]]] = None,
                 inspector: Optional[Callable[[str], list[str]]] = None,
                 ) -> IntakeResult:
    """Full intake for one commitment: validate -> pull -> verify -> gate.

    gate_runner(local_ref) -> the gate-service result dict (the caller
    closes over episodes/outcomes/keys — intake owns fetching and byte
    verification, not corpus selection). A gate_runner that raises is a
    gate REJECTION with the exception recorded, not an intake crash: one
    miner's pathological image must never abort a sweep.
    """
    reason = validate_commitment(commitment)
    if reason:
        return IntakeResult(commitment.hotkey, commitment.digest,
                            STATUS_INVALID_COMMITMENT, detail=reason)

    pull = pull_by_digest(commitment.image_ref, commitment.digest,
                          puller=puller, inspector=inspector)
    if not pull.pulled:
        return IntakeResult(commitment.hotkey, commitment.digest,
                            STATUS_PULL_FAILED, detail=pull.error)
    if not pull.verified:
        return IntakeResult(commitment.hotkey, commitment.digest,
                            STATUS_REJECTED_DIGEST_MISMATCH, detail=pull.error)

    try:
        gate = gate_runner(pull.local_ref)
    except Exception as exc:  # noqa: BLE001 — per-miner isolation is the contract
        return IntakeResult(commitment.hotkey, commitment.digest,
                            STATUS_REJECTED_GATE,
                            detail=f"gate raised: {exc}")
    admitted = bool((gate or {}).get("verdict", {}).get("admitted"))
    return IntakeResult(
        commitment.hotkey, commitment.digest,
        STATUS_ADMITTED if admitted else STATUS_REJECTED_GATE,
        detail=(gate or {}).get("verdict", {}).get("reason"),
        gate=gate)


def intake_all(commitments: Iterable[ModelCommitment],
               gate_runner: Callable[[str], dict],
               puller: Optional[Callable[[str], tuple[bool, str]]] = None,
               inspector: Optional[Callable[[str], list[str]]] = None,
               ) -> dict:
    """Intake sweep over the pending-admission set. Per-miner isolation:
    every commitment gets an IntakeResult, no exception propagates."""
    results: list[IntakeResult] = []
    for c in commitments:
        try:
            results.append(intake_model(c, gate_runner,
                                        puller=puller, inspector=inspector))
        except Exception as exc:  # noqa: BLE001 — absolute backstop
            results.append(IntakeResult(c.hotkey, c.digest,
                                        STATUS_PULL_FAILED,
                                        detail=f"intake raised: {exc}"))
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "results": results,
        "admitted": [r.hotkey for r in results if r.status == STATUS_ADMITTED],
        "by_status": by_status,
        "total": len(results),
    }
