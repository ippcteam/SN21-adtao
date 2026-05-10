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
    *,
    block_hash: Optional[str] = None,
) -> Optional[list[RawCommitField]]:
    """Read the non-TLE commit for (netuid, hotkey) at a specific block (or latest).

    Returns None if no commit exists. Otherwise a list of
    RawCommitField (one per Data variant in info.fields).

    Phase H, the chain's `Commitments::CommitmentOf` storage holds ONE
    entry per (netuid, hotkey) at a time — every new `set_commitment`
    overwrites the previous regardless of variant. To read historical
    commits, pass `block_hash` of the block where the commit landed; an
    ARCHIVE node retains the storage at that block.

    Args:
        block_hash: optional 0x-prefixed hex block hash for a block-pinned
            read. When None (default), reads at the chain head. Auditors
            verifying past epochs MUST pass the block_hash where the
            commit landed (the validator publishes these in 9.C.2 for
            its own commits and via events for miners' commits).

    Bypasses `Subtensor.get_commitment(...)` because that helper UTF-8
    decodes the bytes and mangles non-ASCII content.
    """
    query_kwargs = {
        "module": "Commitments",
        "storage_function": "CommitmentOf",
        "params": [netuid, hotkey_ss58],
    }
    if block_hash is not None:
        query_kwargs["block_hash"] = block_hash
    result = subtensor.substrate.query(**query_kwargs)
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
    # (the variant name) with a value whose shape varies by variant:
    #   - Raw{N}:            tuple-of-byte-ints OR (tuple-of-byte-ints,) (1-tuple wrap)
    #   - Sha256:            tuple-of-byte-ints (32 bytes)
    #   - TimelockEncrypted: dict {'encrypted': ..., 'reveal_round': N}
    #   - BlakeTwo256 etc.:  same tuple-of-bytes shape as Raw
    # We normalise all of these to bytes (or empty for non-byte variants
    # like TimelockEncrypted) so callers get a consistent RawCommitField
    # list and don't crash on unexpected payload types.
    out: list[RawCommitField] = []
    for outer in fields:
        for entry in outer:
            if not isinstance(entry, dict) or len(entry) != 1:
                continue
            variant, payload = next(iter(entry.items()))
            # Unwrap 1-tuples that wrap a tuple-of-ints (some Substrate
            # SCALE codecs nest the bytes one level deep).
            if (isinstance(payload, tuple)
                    and len(payload) == 1
                    and isinstance(payload[0], (tuple, list))):
                payload = payload[0]
            if isinstance(payload, (bytes, bytearray)):
                out.append(RawCommitField(variant=variant, bytes_=bytes(payload)))
            elif isinstance(payload, (tuple, list)):
                # Empty tuple → empty bytes; tuple-of-ints → bytes(...);
                # if it's still nested (unexpected), guard against TypeError.
                try:
                    out.append(RawCommitField(
                        variant=variant,
                        bytes_=bytes(payload) if payload else b"",
                    ))
                except TypeError:
                    out.append(RawCommitField(variant=variant, bytes_=b""))
            elif isinstance(payload, dict):
                # TimelockEncrypted / other structured variants — bytes are
                # not directly accessible from CommitmentOf. Emit an empty
                # marker so callers can see that the variant was present.
                out.append(RawCommitField(variant=variant, bytes_=b""))
    return out


def read_revealed_commitments(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
    *,
    block_hash: Optional[str] = None,
) -> list[RevealedEntry]:
    """Read auto-decrypted TLE plaintexts for (netuid, hotkey) at a specific block.

    Returns a list ordered as the chain stored them (oldest first in
    Phase 0 observation). Each `payload_bytes` is the FULL SCALE-encoded
    Data variant — the first 1-2 bytes are variant tag + length, followed
    by the actual plaintext we committed.

    Phase H findings: `RevealedCommitments` may store only the latest 1
    entry per (netuid, hotkey) on this chain runtime. To audit a specific
    historical reveal, pass `block_hash` of a block AFTER the reveal_round
    fired but BEFORE another reveal overwrote it. An archive node is
    required for this — the chain head can show only the most recent
    state.

    Args:
        block_hash: optional 0x-prefixed hex block hash for a block-pinned
            read. When None (default), reads at chain head.

    Bypasses `Subtensor.get_revealed_commitment_by_hotkey(...)` because
    that helper UTF-8 decodes the bytes and mangles non-ASCII content.
    """
    query_kwargs = {
        "module": "Commitments",
        "storage_function": "RevealedCommitments",
        "params": [netuid, hotkey_ss58],
    }
    if block_hash is not None:
        query_kwargs["block_hash"] = block_hash
    result = subtensor.substrate.query(**query_kwargs)
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


@dataclass(frozen=True)
class CommitEvent:
    """One Commitments-pallet event emitted at a specific block.

    Events are the authoritative IMMUTABLE record of a commit. Storage
    (`CommitmentOf`) is mutable (single-slot, last-write-wins). For
    historical audit, scan events at the block where the commit landed.
    """

    block_number: int
    block_hash: str
    netuid: int
    hotkey_ss58: str
    event_name: str  # e.g. "CommitmentSet", "CommitmentRevealed"
    raw: dict       # full event payload for further inspection


def read_events_at_block(
    subtensor,
    block_hash: str,
    *,
    module_filter: Optional[str] = "Commitments",
) -> list[CommitEvent]:
    """Scan events emitted at a specific block, optionally filtered by pallet.

    The substrate `System::Events` storage at a given block_hash returns the
    full event log for that block. Each event has (module, name, attributes).

    For SN21 audit, call with the block_hash where a commit landed and
    filter to `Commitments`. The returned events tell you (netuid, hotkey)
    and the commit's metadata, complementing the storage read.

    Args:
        block_hash: 0x-prefixed hex of the block to scan.
        module_filter: pallet name; None = all events.

    Returns:
        List of CommitEvent. Empty if no matching events.
    """
    block_number = _block_number_for_hash(subtensor, block_hash)
    raw_events = subtensor.substrate.get_events(block_hash=block_hash)
    out: list[CommitEvent] = []
    for ev in raw_events:
        ev_data = ev.value if hasattr(ev, "value") else ev
        if not isinstance(ev_data, dict):
            continue
        ev_event = ev_data.get("event")
        if not isinstance(ev_event, dict):
            continue
        module = ev_event.get("module_id") or ev_event.get("pallet") or ev_event.get("module")
        name = ev_event.get("event_id") or ev_event.get("event") or ev_event.get("name")
        if module_filter is not None and module != module_filter:
            continue

        # Best-effort extract netuid + hotkey from the event attributes.
        attributes = ev_event.get("attributes") or ev_event.get("params") or []
        netuid: Optional[int] = None
        hotkey_ss58: Optional[str] = None
        if isinstance(attributes, list):
            for attr in attributes:
                if isinstance(attr, dict):
                    val = attr.get("value", attr)
                    if isinstance(val, int) and netuid is None:
                        netuid = val
                    elif isinstance(val, str) and hotkey_ss58 is None and val.startswith("5"):
                        hotkey_ss58 = val
        if netuid is None or hotkey_ss58 is None:
            # Skip events we can't attribute; probably not a commit event.
            continue

        out.append(CommitEvent(
            block_number=block_number,
            block_hash=block_hash,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            event_name=str(name) if name else "",
            raw=ev_event,
        ))
    return out


def _block_number_for_hash(subtensor, block_hash: str) -> int:
    """Resolve a block hash to its block number."""
    header = subtensor.substrate.get_block_header(block_hash=block_hash)
    if isinstance(header, dict):
        h = header.get("header", header)
        n = h.get("number") or h.get("blockNumber") or 0
        if isinstance(n, str):
            n = int(n, 16) if n.startswith("0x") else int(n)
        return int(n)
    return 0


def decode_revealed_tle_plaintext(payload_bytes: bytes) -> bytes:
    """Decode a `RevealedCommitments` entry's payload back to original bytes.

    The chain's auto-decrypt path for TLE commits (committed via
    `bittensor_drand.get_encrypted_commitment(data: str, ...)`) stores the
    decrypted output as:

        <SCALE_compact_length_prefix> || <utf8_bytes_of_hex_string>

    Where `utf8_bytes_of_hex_string` is `original_plaintext.hex().encode()`.

    This helper:
      1. Decodes the SCALE compact-encoded length prefix (1, 2, or 4 bytes).
      2. Slices the payload to that length.
      3. Interprets the slice as ASCII hex and decodes back to raw bytes.

    Returns the original `plaintext: bytes` that was passed to
    `hope.commitment.on_chain.submit_timelock_commit`.

    Raises:
        ValueError: malformed input (bad SCALE prefix, non-hex payload).
    """
    if not payload_bytes:
        raise ValueError("empty payload")

    # SCALE compact integer: bottom 2 bits of first byte indicate the mode.
    #   0b00: 1-byte mode, length = (b0 >> 2)
    #   0b01: 2-byte mode (LE), length = (u16 >> 2)
    #   0b10: 4-byte mode (LE), length = (u32 >> 2)
    #   0b11: BigInt mode (length-prefixed bytes, then LE u(8*N))
    b0 = payload_bytes[0]
    mode = b0 & 0b11
    if mode == 0b00:
        length = b0 >> 2
        body_offset = 1
    elif mode == 0b01:
        if len(payload_bytes) < 2:
            raise ValueError("SCALE compact prefix truncated (mode 1)")
        u16 = int.from_bytes(payload_bytes[:2], "little", signed=False)
        length = u16 >> 2
        body_offset = 2
    elif mode == 0b10:
        if len(payload_bytes) < 4:
            raise ValueError("SCALE compact prefix truncated (mode 2)")
        u32 = int.from_bytes(payload_bytes[:4], "little", signed=False)
        length = u32 >> 2
        body_offset = 4
    else:
        # BigInt mode: rare for our payload sizes (<2^30 bytes). Not supported.
        raise ValueError("SCALE compact BigInt mode not supported")

    body = payload_bytes[body_offset : body_offset + length]
    if len(body) < length:
        raise ValueError(
            f"SCALE prefix claims length {length} but only "
            f"{len(body)} bytes available"
        )

    # The body is the hex string we wrote — decode back to bytes.
    try:
        hex_str = body.decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"payload body not ASCII hex: {e}") from e
    try:
        return bytes.fromhex(hex_str)
    except ValueError as e:
        raise ValueError(f"payload body not valid hex: {e}") from e


# Note on SCALE Data-variant prefix handling.
#
# The chain's `Data` enum is SCALE-encoded with a 1-byte variant tag
# (Raw{N}=1..129, Sha256=130, TimelockEncrypted≈134). For the
# auto-decrypted output of a TimelockEncrypted commit, the runtime
# stores the decrypted plaintext as `<scale_compact_length><utf8_bytes>`
# inside the variant. Callers strip the prefix in one of three ways:
#
#   1. Match a known plaintext prefix (e.g. `b"sn21-reg-v1:"`) and
#      slice from the prefix offset.
#   2. Skip a fixed offset (1 variant byte, optionally + 1-2 compact
#      length bytes) when the variant is known up front.
#   3. Hex-decode after detecting an even-length hex string (the
#      Phase H TLE round-trip pattern via
#      `decode_revealed_tle_plaintext` above).
#
# We previously exported a `strip_scale_data_variant_prefix` no-op
# helper as a "documentation landing pad" but no caller used it;
# specific parsers (parse_registration_payload, decode_revealed_tle_plaintext)
# handle their own prefix detection inline.


# ----------------------------------------------------------------------------
# Commitments pallet budget — read-side helpers
#
# Substrate's Commitments pallet enforces a per-(netuid, hotkey) byte
# budget per pallet-epoch (`MaxSpace`, ~3100 bytes by default). Once
# `used_space + new_commit_size > MaxSpace`, the chain rejects with
# `SpaceLimitExceeded`. A validator scoring run consumes ~1232 bytes
# (9.C.1 + 9.C.2 + optional 9.C.6); plenty of headroom in a fresh
# epoch, but exhausted if the same hotkey re-runs scoring within the
# same pallet-epoch.
#
# The pallet-epoch counter is governance-set and NOT necessarily aligned
# with the subnet tempo. Callers should query budget before commits and
# either skip cleanly or wait for the next pallet-epoch (when
# `last_epoch` advances, `used_space` resets to 0).
# ----------------------------------------------------------------------------

# Conservative byte budget required for a full validator scoring round
# (9.C.1 + 9.C.2 + optional 9.C.6). Below this the runner should refuse
# to start commits to avoid leaving an epoch in a partially-scored state.
MIN_VALIDATOR_BUDGET_BYTES = 1300


def read_commitments_budget(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
    *,
    block_hash: Optional[str] = None,
) -> tuple[int, int, int]:
    """Read the Commitments pallet budget state for `(netuid, hotkey)`.

    Returns:
        Tuple ``(used_space, max_space, last_epoch)``. ``last_epoch`` is the
        pallet-epoch counter at which the current ``used_space`` was tracked;
        when the pallet-epoch ticks over, the runtime resets ``used_space``
        on the first new commit.

        ``max_space`` is read live from the runtime constant
        ``Commitments.MaxSpace``; if not present, defaults to 3100.

    The chain rejects new commits with ``SpaceLimitExceeded`` when
    ``used_space + new_commit_size > max_space``.
    """
    query_kwargs = {"module": "Commitments", "storage_function": "UsedSpaceOf",
                    "params": [netuid, hotkey_ss58]}
    if block_hash is not None:
        query_kwargs["block_hash"] = block_hash
    raw = subtensor.substrate.query(**query_kwargs)
    val = raw.value if hasattr(raw, "value") else raw
    used = 0
    last_epoch = 0
    if isinstance(val, dict):
        used = int(val.get("used_space", 0) or 0)
        last_epoch = int(val.get("last_epoch", 0) or 0)

    max_space = 3100
    try:
        ms = subtensor.substrate.query("Commitments", "MaxSpace", [])
        ms_v = ms.value if hasattr(ms, "value") else ms
        if isinstance(ms_v, int):
            max_space = ms_v
    except Exception:
        pass

    return used, max_space, last_epoch


def commitments_budget_sufficient(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
    *,
    needed_bytes: int = MIN_VALIDATOR_BUDGET_BYTES,
    block_hash: Optional[str] = None,
) -> tuple[bool, int, int]:
    """Return ``(sufficient, remaining_bytes, last_epoch)``.

    ``sufficient`` is True iff ``max_space - used_space >= needed_bytes``.
    """
    used, max_space, last_epoch = read_commitments_budget(
        subtensor, netuid, hotkey_ss58, block_hash=block_hash,
    )
    remaining = max_space - used
    return remaining >= needed_bytes, remaining, last_epoch


def validator_already_scored_epoch(
    subtensor,
    netuid: int,
    validator_hotkey_ss58: str,
    epoch_id: str,
    *,
    block_hash: Optional[str] = None,
) -> bool:
    """Return True if the validator has already submitted a 9.C.1 commit
    for ``epoch_id``. Used to make scoring runs idempotent.

    The check inspects the validator's revealed commitments and looks
    for a CBOR plaintext whose ``epoch_id`` field matches. Pending (not
    yet auto-decrypted) TLE commits cannot be inspected this way; for
    those, the byte-budget check is the second line of defence — a
    re-run with a recent same-epoch commit will fail
    ``commitments_budget_sufficient`` because the commit ate ~600 bytes.
    """
    try:
        from hope.commitment.canonical import canonical_cbor_loads
    except ImportError:
        return False
    revealed = read_revealed_commitments(
        subtensor, netuid, validator_hotkey_ss58, block_hash=block_hash,
    )
    for entry in revealed:
        try:
            payload = decode_revealed_tle_plaintext(entry.payload_bytes)
            obj = canonical_cbor_loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("epoch_id") == epoch_id:
            return True
    return False
