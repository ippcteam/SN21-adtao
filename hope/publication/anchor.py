"""Anchoring the feed's rolling Merkle root on chain.

`daily_loop` already decides WHETHER to anchor — the flag, the root, the
guards. What it does not have is something to anchor WITH: it calls an
injected `chain_committer` and, with nothing wired, records
`skipped_no_committer` and moves on. This module is that committer.

WHY THE INJECTION SEAM EXISTS AT ALL
    Committing costs a chain write from the validator's hotkey. Building the
    committer inside the loop would mean every code path that runs the loop —
    tests, rehearsals, a local replay — is one misconfiguration away from
    spending on chain. So the loop takes a callable, the entrypoint decides
    whether to construct one, and the flag governs the entrypoint.

WHAT GETS COMMITTED
    Exactly 32 bytes: the rolling Merkle root over every day published so far
    (`feed_root`), as a `Data::Sha256` commitment. Not the day's own hash —
    `Commitments::CommitmentOf` holds one entry per (netuid, hotkey) and each
    write overwrites the last, so per-day hashes would leave only the newest
    day verifiable at chain head. The root covers the whole history, which is
    what `verify_day --expect-anchor` compares against.

THE SPEND GUARD
    The root only changes when a day is published, but the loop is idempotent
    and re-running it on the same day is normal. Without a memo of the last
    anchored root, a re-run spends a second write to commit the identical
    value — and reads on chain as a fresh anchor when nothing happened. So the
    committer records what it anchored and declines to repeat itself.

    The memo is written ONLY after a confirmed success. A failed commit
    leaves no record, so the next run retries rather than believing it already
    anchored.

Failures never propagate. The receipt and accuracy documents are already
written and hash-chained by the time this runs; an unreachable chain must
leave the day published and the anchor visibly absent, not lose the day.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

ANCHOR_STATE_FILE = "_last_anchor.json"

ROOT_BYTES = 32


def anchor_state_path(ledger_root: str) -> str:
    return os.path.join(ledger_root, ANCHOR_STATE_FILE)


def last_anchor(ledger_root: str | None) -> dict | None:
    """The last root this validator successfully committed, or None."""
    if not ledger_root:
        return None
    path = anchor_state_path(ledger_root)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        # An unreadable memo must not be read as "already anchored" — that
        # would silently stop anchoring. Treat it as absent and re-commit.
        return None


def record_anchor(ledger_root: str | None, record: dict) -> None:
    if not ledger_root:
        return
    os.makedirs(ledger_root, exist_ok=True)
    path = anchor_state_path(ledger_root)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, sort_keys=True)
    os.replace(tmp, path)


def make_committer(
    submit: Callable[[bytes], Any],
    ledger_root: str | None = None,
) -> Callable[[bytes], dict]:
    """Wrap a chain-submit callable into the committer `daily_loop` expects.

    `submit` takes the 32 root bytes and returns anything with `.success`,
    `.block_number` and `.message` — the shape `submit_sha256_commit`
    already returns. Keeping it a parameter means this module never imports
    bittensor and stays testable without a chain.
    """
    def _commit(root_bytes: bytes) -> dict:
        if not isinstance(root_bytes, (bytes, bytearray)) or len(root_bytes) != ROOT_BYTES:
            # Committing a short or empty value would read on chain as a real
            # anchor over a history it does not cover.
            return {"ok": False, "reason": "root_not_32_bytes",
                    "length": (len(root_bytes) if root_bytes is not None else None)}

        root_hex = bytes(root_bytes).hex()
        previous = last_anchor(ledger_root)
        if previous and previous.get("root") == root_hex:
            return {"ok": True, "skipped": "root_unchanged",
                    "root": root_hex, "block": previous.get("block")}

        try:
            result = submit(bytes(root_bytes))
        except Exception as exc:
            # The chain is the one dependency here that is expected to fail
            # transiently. Report it and let the next run retry.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "root": root_hex}

        ok = bool(getattr(result, "success", False))
        record = {
            "ok": ok,
            "root": root_hex,
            "block": getattr(result, "block_number", None),
            "message": getattr(result, "message", None),
        }
        if ok:
            record_anchor(ledger_root, record)
        return record

    return _commit


def bittensor_committer(
    subtensor,
    wallet,
    netuid: int,
    ledger_root: str | None = None,
    *,
    wait_for_finalization: bool = True,
) -> Callable[[bytes], dict]:
    """The production committer: a `Data::Sha256` write of the root.

    32 bytes against a per-pallet-epoch MaxSpace of ~3,100, so one daily
    anchor cannot crowd out anything else the hotkey commits.
    """
    from hope.commitment.on_chain import submit_sha256_commit

    def _submit(root_bytes: bytes):
        return submit_sha256_commit(
            subtensor, wallet, netuid, root_bytes,
            wait_for_finalization=wait_for_finalization,
            raise_error=False,
        )

    return make_committer(_submit, ledger_root)
