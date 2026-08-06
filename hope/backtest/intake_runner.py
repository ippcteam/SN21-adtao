"""Model intake: on-chain commitments in, attested admission verdicts out.

The pieces of this pipeline all existed and were tested — the registry parser,
the digest-pinned pull, the gate, the verdict rail — and nothing called them.
`build_registry`, `intake_all` and `gate_submission` had no production caller
anywhere outside tests, which meant the quickstart's promise ("the subnet
pulls, gate-admits, and runs it against every daily basket") had nothing
behind it. This module is the missing middle.

WHAT IT DOES, IN ORDER
    1. read each registered hotkey's latest commitment off chain
    2. keep the ones that parse as sn21-model:v1 and are new to us
    3. pull each image BY DIGEST and verify the registry served those bytes
    4. run it against the held-out corpus and score it against the naive
       baseline
    5. write the verdict as an attested, hash-chained document, so an
       admission is provable and a rejection is contestable with evidence

IDEMPOTENCE IS THE POINT
    A digest that already has a verdict is never re-gated. Miners re-commit
    for all sorts of reasons, the chain slot is last-write-wins, and running
    an untrusted container is the most expensive thing this subnet does — so
    the work is keyed on the digest, not on the commitment.

EVERY EDGE IS INJECTED
    Chain, docker and clock all arrive as parameters. That is what lets the
    whole path be tested without a chain to read or an image to pull, which
    matters because the parts that are hardest to exercise in production are
    exactly the ones that decide whether a stranger's container earns money.

The gate itself runs a container, so this needs a Docker host. That is a
placement decision, not a code one, and it is why this ships as a callable
entrypoint rather than a scheduled task.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from hope.backtest.image_intake import (
    STATUS_ADMITTED,
    STATUS_REJECTED_GATE,
    ModelCommitment,
    intake_all,
)
from hope.backtest.model_registry import (
    MODEL_COMMIT_PREFIX,
    parse_model_commitment,
)

VERDICT_DIR = "admission_verdicts"
ADMITTED_FILE = "_admitted_digests.json"


@dataclass(frozen=True)
class IntakeRun:
    """What one sweep did, in terms a human can check against the feed."""
    considered: int          # hotkeys with a parseable model commitment
    already_verdicted: int   # digests we had already judged
    gated: int               # containers actually run this sweep
    admitted: int
    rejected: int
    unparseable: int         # commitments that were not ours or malformed
    details: list[dict]

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "already_verdicted": self.already_verdicted,
            "gated": self.gated,
            "admitted": self.admitted,
            "rejected": self.rejected,
            "unparseable": self.unparseable,
            "details": self.details,
        }


def verdict_dir(ledger_root: str) -> str:
    return os.path.join(ledger_root, VERDICT_DIR)


def admitted_path(ledger_root: str) -> str:
    return os.path.join(verdict_dir(ledger_root), ADMITTED_FILE)


def load_admitted(ledger_root: str) -> set[str]:
    """Digests that have passed the gate. This is what makes a model runnable
    in the registry, so a mangled file must not silently empty the field."""
    path = admitted_path(ledger_root)
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get("admitted", []))
    except (OSError, ValueError):
        # Refusing to guess: an unreadable admitted-set is reported by the
        # caller rather than treated as "nobody is admitted", which would
        # quietly unrun every live model.
        raise RuntimeError(f"admitted-digest file is unreadable: {path}")


# Only a GATE outcome is a verdict on the MODEL. Everything else is a verdict
# on the fetch, and fetches are retryable.
#
# This distinction is load-bearing for the front-running defence (audit
# 2026-08-06, scenario E): the way an author denies a registry-watcher the
# chance to commit their digest first is to commit BEFORE the image is
# public — digest on chain, then flip the repo public. That discipline only
# works if the intake sweep that runs in between, finds the image unpullable,
# and moves on does NOT record a permanent verdict. A pull failure damning
# the digest forever would convert the recommended defence into a
# self-inflicted rejection.
FINAL_STATUSES = frozenset({STATUS_ADMITTED, STATUS_REJECTED_GATE})


def verdicted_digests(ledger_root: str) -> set[str]:
    """Digests with a FINAL verdict — gated and judged, either way.

    A gate rejection is final: re-running yesterday's failing container burns
    the same sandbox minutes for the same answer, and a miner who wants a new
    verdict publishes new bytes, which is a new digest. A pull failure or a
    registry mismatch is NOT final — the registry may simply not be public
    yet, or still propagating — so those digests stay eligible for the next
    sweep.

    A record with no readable status is treated as final, deliberately: the
    fail-safe direction for "I can't tell" is to not re-run an unknown
    container, because running strangers' code is the expensive, dangerous
    step.
    """
    directory = verdict_dir(ledger_root)
    if not os.path.isdir(directory):
        return set()
    out = set()
    for name in os.listdir(directory):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        try:
            with open(os.path.join(directory, name)) as f:
                envelope = json.load(f)
        except (OSError, ValueError):
            continue
        # Verdicts appear in two shapes: the attested envelope
        # ({document: {metrics: {...}}}) and the flat record the sweep's
        # persist callback writes. Read both.
        body = (envelope.get("document") or {}).get("metrics") or envelope
        digest = body.get("image_digest") or body.get("digest")
        status = body.get("status")
        if not digest:
            continue
        if status is None or status in FINAL_STATUSES:
            out.add(digest)
    return out


def commitments_from_chain(
    hotkeys: Iterable[str],
    read_commitment: Callable[[str], str | None],
) -> tuple[list[ModelCommitment], int]:
    """(parseable commitments, count of unparseable ones).

    A hotkey whose commitment is absent, not ours, or malformed is simply not
    a submission — it is counted and skipped. Nothing miner-controlled reaches
    docker before `parse_model_commitment` has accepted its grammar.
    """
    commitments, unparseable = [], 0
    for hotkey in hotkeys:
        raw = read_commitment(hotkey)
        if not raw:
            continue
        if not raw.startswith(MODEL_COMMIT_PREFIX):
            # Not an SN21 model commitment at all. The chain slot is shared and
            # last-write-wins, so a hotkey may legitimately be carrying
            # something else entirely — counting that as a malformed SUBMISSION
            # would inflate a failure number miners are shown.
            continue
        parsed = parse_model_commitment(raw)
        if not parsed or not parsed.get("image_ref"):
            # Ours, but unusable: either malformed, or the deprecated
            # digest-only form, which names no repository to pull from.
            unparseable += 1
            continue
        commitments.append(ModelCommitment(
            hotkey=hotkey,
            image_ref=parsed["image_ref"],
            digest=parsed["digest"],
        ))
    return commitments, unparseable


def run_intake(
    ledger_root: str,
    hotkeys: Iterable[str],
    read_commitment: Callable[[str], str | None],
    gate_runner: Callable[[str], dict],
    puller: Callable[[str], tuple[bool, str]] | None = None,
    inspector: Callable[[str], list[str]] | None = None,
    persist: Callable[[str, dict], None] | None = None,
) -> IntakeRun:
    """One intake sweep. Returns what it did; writes verdicts through `persist`.

    `gate_runner(digest) -> verdict dict` is the expensive step and is only
    ever called for a digest with no verdict on file.
    """
    commitments, unparseable = commitments_from_chain(hotkeys, read_commitment)
    seen = verdicted_digests(ledger_root)

    pending = [c for c in commitments if c.digest not in seen]
    skipped = len(commitments) - len(pending)

    if not pending:
        return IntakeRun(considered=len(commitments), already_verdicted=skipped,
                         gated=0, admitted=0, rejected=0,
                         unparseable=unparseable, details=[])

    outcome = intake_all(pending, gate_runner, puller=puller, inspector=inspector)

    admitted, rejected, details = 0, 0, []
    for result in outcome.get("results", []):
        record = {
            "hotkey": getattr(result, "hotkey", None),
            "digest": getattr(result, "digest", None),
            "status": getattr(result, "status", None),
            "detail": getattr(result, "detail", None),
            # The gate's own numbers — what it scored and against what — so a
            # rejection can be argued with rather than just announced.
            "gate": getattr(result, "gate", None),
        }
        # An admitted model is one that PASSED the gate, not one that merely
        # pulled. A pull failure and a below-baseline model are both "not
        # admitted" and must stay distinguishable in the record.
        if record["status"] == STATUS_ADMITTED:
            admitted += 1
        else:
            rejected += 1
        details.append(record)
        if persist:
            persist(record["digest"], record)

    return IntakeRun(considered=len(commitments), already_verdicted=skipped,
                     gated=len(pending), admitted=admitted, rejected=rejected,
                     unparseable=unparseable, details=details)
