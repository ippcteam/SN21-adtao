"""Substrate-direct readback for SN21 chain commitments.

The Bittensor SDK's `Subtensor.get_commitment(...)` and
`Subtensor.get_revealed_commitment_by_hotkey(...)` lossily UTF-8 decode
binary bytes — characters >127 in the source bytes get mangled into
multi-byte codepoints that cannot be recovered. This breaks any path
that needs byte-exact reads of:

  - registration payloads (binary `Raw{N}` with sn21-reg-v1 prefix + sig)
  - timelock-encrypted plaintexts after auto-decrypt (binary CBOR)

This module bypasses the SDK's text-mangling layer and goes directly
to the substrate `query()` API, which returns the underlying SCALE
shape — a Python tuple of byte values that we convert with `bytes(t)`.

Empirical chain layout (testnet 466, 2026-05-03):

  Commitments::CommitmentOf(netuid, hotkey)
    → dict {"deposit": int, "block": int, "info": {"fields": (...)}}
    → fields is a tuple of variant-tagged tuples, e.g.
        ({"Raw109": (115, 110, ...)},)
        ({"Sha256": (..32 ints..)},)

  Commitments::RevealedCommitments(netuid, hotkey)
    → list of tuples (payload_int_tuple, block_number)
    → payload_int_tuple is the full SCALE encoding of the auto-decrypted
       Data variant (variant byte + length tag + bytes). For our use,
       we store the WHOLE thing and let the parser strip the SCALE
       prefix bytes if needed.

The chain-side caps + lag behaviour observed (Phase 0):
  - At most 1 RevealedCommitments entry per (netuid, hotkey) at a time.
    Older reveals are evicted by newer ones; the architecture's "10
    reveal cache" assumption was wrong.
  - Auto-decrypt may lag by tens of minutes after reveal_round passes.
    Some commits never appear in RevealedCommitments at all (Q36 finding).
  - CommitmentOf is overwritten by every new non-TLE commit from the
    same hotkey. Only the LATEST is queryable. To audit older commits,
    use an archive node + block-pinned reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RawCommitField:
    """One field decoded from a CommitmentOf info.fields tuple.

    `variant` is the Data enum tag (e.g. "Raw109", "Sha256",
    "TimelockEncrypted"). `bytes_` is the raw byte payload — empty for
    variants like "None" or `Raw0`.
    """

    variant: str
    bytes_: bytes


@dataclass(frozen=True)
class RevealedEntry:
    """One auto-decrypted TLE plaintext entry from RevealedCommitments."""

    block_number: int
    payload_bytes: bytes  # full SCALE-encoded Data variant including tag


def read_commitment_of(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
) -> Optional[list[RawCommitField]]:
    """Read the latest non-TLE commit for (netuid, hotkey).

    Returns None if no commit exists. Otherwise a list of
    RawCommitField (one per Data variant in info.fields). For SN21
    registration, the typical case is a single Raw109 field.

    Bypasses `Subtensor.get_commitment(...)` because that helper UTF-8
    decodes the bytes and mangles non-ASCII content.
    """
    result = subtensor.substrate.query(
        module="Commitments",
        storage_function="CommitmentOf",
        params=[netuid, hotkey_ss58],
    )
    val = result.value if hasattr(result, "value") else result
    if not val:
        return None

    info = val.get("info") if isinstance(val, dict) else None
    if not info:
        return None
    fields = info.get("fields")
    if not fields:
        return []

    # `fields` is a tuple-of-tuple-of-dict. Each inner dict has one key
    # (the variant name) with a value that is a tuple of byte ints.
    out: list[RawCommitField] = []
    for outer in fields:
        for entry in outer:
            if not isinstance(entry, dict) or len(entry) != 1:
                continue
            variant, payload = next(iter(entry.items()))
            if isinstance(payload, (tuple, list)):
                out.append(RawCommitField(
                    variant=variant,
                    bytes_=bytes(payload) if payload else b"",
                ))
            elif isinstance(payload, (bytes, bytearray)):
                out.append(RawCommitField(variant=variant, bytes_=bytes(payload)))
    return out


def read_revealed_commitments(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
) -> list[RevealedEntry]:
    """Read all auto-decrypted TLE plaintexts for (netuid, hotkey).

    Returns a list ordered as the chain stored them (oldest first in
    Phase 0 observation, but callers should not rely on order). Each
    `payload_bytes` is the FULL SCALE-encoded Data variant — the first
    1-2 bytes are variant tag + length, followed by the actual plaintext
    we committed.

    Bypasses `Subtensor.get_revealed_commitment_by_hotkey(...)` because
    that helper UTF-8 decodes the bytes and mangles non-ASCII content.
    """
    result = subtensor.substrate.query(
        module="Commitments",
        storage_function="RevealedCommitments",
        params=[netuid, hotkey_ss58],
    )
    val = result.value if hasattr(result, "value") else result
    if not val:
        return []

    out: list[RevealedEntry] = []
    for entry in val:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        payload, block_num = entry
        if isinstance(payload, (tuple, list)):
            payload_bytes = bytes(payload)
        elif isinstance(payload, (bytes, bytearray)):
            payload_bytes = bytes(payload)
        else:
            continue
        if not isinstance(block_num, int):
            continue
        out.append(RevealedEntry(block_number=block_num, payload_bytes=payload_bytes))
    return out


def strip_scale_data_variant_prefix(payload: bytes) -> bytes:
    """Strip the leading Data enum variant tag + length from a SCALE-encoded payload.

    The chain's `Data` enum encoding for the variants we care about:
      - `Raw{N}` (variant 1..129): 1-byte variant tag, then N bytes
        directly (no length byte — the variant index encodes the length).
      - `Sha256` (variant 130): 1-byte variant tag, then exactly 32 bytes.
      - `TimelockEncrypted` (variant ~134): 1-byte variant tag, then a
        `BoundedVec<u8, ...>` which IS length-prefixed.

    For the auto-decrypted output of a TimelockEncrypted commit, the
    chain stores the DECRYPTED plaintext (often re-wrapped in some
    variant). The Phase 0 stale entry we observed had a 514-byte payload
    starting with `0x01 0x08 0x63 ...` — variant 1 (Raw0) + something.

    For a clean read, callers can:
      1. Try parsing payload[0:] directly (if they wrote raw bytes).
      2. Try payload[1:] (skip 1 variant byte).
      3. Try payload[2:] (skip 1 variant + 1 length byte for compact-encoded len).
      4. Match against a known prefix (e.g., `b"sn21-reg-v1:"`) and slice
         from the prefix offset.

    This function returns the payload UNCHANGED — it's a documentation
    landing pad. Specific parsers (e.g., `parse_registration_payload`)
    handle the variant stripping themselves via prefix detection.
    """
    return payload
