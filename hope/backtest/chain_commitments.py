"""Reading miners' model commitments off chain, in bulk.

WHY BULK

    `Commitments::CommitmentOf` holds one entry per (netuid, hotkey). Reading
    it a hotkey at a time costs one RPC round trip each, and at 256 hotkeys
    against a public endpoint that is upwards of ten minutes — long enough
    that the intake sweep stops being something anyone runs on a schedule.
    One paged map read returns the same data in seconds.

WHY IT IS ONLY AN OPTIMISATION

    `chain_reader.read_commitment_of` remains the contract: it decodes
    properly typed fields and is what the rest of the system tests against.
    Every function here degrades to empty on any failure so the caller falls
    back to that path. A faster read must never become the reason a miner's
    commitment goes unseen — that would silently deny someone admission.

WHAT IS TRUSTED

    Nothing. The strings returned are miner-controlled. Callers must pass
    them through `parse_model_commitment`, which enforces the grammar,
    before any of it reaches docker.
"""

from __future__ import annotations

import binascii
import re

from hope.backtest.model_registry import MODEL_COMMIT_PREFIX

_HEX_BLOB = re.compile(r"0x([0-9a-fA-F]{8,})")


def model_string_in(value) -> str | None:
    """The model commitment inside a decoded CommitmentOf record, if present.

    The substrate client returns these as nested structures whose exact shape
    varies by version, so the byte payloads are recovered from the record's
    text form rather than by walking a shape we would have to keep in step.
    """
    for hexpart in _HEX_BLOB.findall(str(value)):
        try:
            text = binascii.unhexlify(hexpart).decode("utf8", "ignore").strip()
        except (binascii.Error, ValueError):
            continue
        if text.startswith(MODEL_COMMIT_PREFIX):
            return text
    return None


def block_of(value) -> int | None:
    """The block a commitment landed in, when the record carries it."""
    try:
        record = value.value if hasattr(value, "value") else value
        block = record.get("block")
        return int(block) if block is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def bulk_model_commitments(subtensor, netuid: int, hotkeys) -> dict:
    """hotkey -> (block, raw_commitment) for every registered model commitment.

    Restricted to `hotkeys` deliberately: the map also carries entries for
    hotkeys that have since deregistered, and those must not be revived into
    a runnable set.
    """
    try:
        rows = subtensor.substrate.query_map(
            module="Commitments", storage_function="CommitmentOf",
            params=[netuid], page_size=300)
    except Exception:
        return {}

    wanted = set(hotkeys)
    out: dict = {}
    try:
        for key, value in rows:
            hotkey = str(key)
            if hotkey not in wanted:
                continue
            raw = value.value if hasattr(value, "value") else value
            text = model_string_in(raw)
            if text:
                out[hotkey] = (block_of(raw) or 0, text)
    except Exception:
        return {}
    return out


def as_registry_reader(commitments: dict):
    """Adapt the bulk result to `build_registry`'s reader signature.

    build_registry expects hotkey -> [(block, raw)] because the prediction era
    could hold several commits. The pallet keeps only the latest, so the list
    is one entry or none — the shape is preserved rather than the plurality.
    """
    def read(hotkey):
        entry = commitments.get(hotkey)
        return [entry] if entry else []
    return read


def as_single_reader(commitments: dict):
    """Adapt the bulk result to the intake sweep's reader signature."""
    def read(hotkey):
        entry = commitments.get(hotkey)
        return entry[1] if entry else None
    return read
