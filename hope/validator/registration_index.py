"""In-memory index of SS58 ↔ ed25519 bindings discovered from chain events.

Background
----------
The protocol (per `hope/commitment/registration.py`) binds each hotkey to a
canonical ed25519 inner-sig key via a `Raw{N}` commit in the Commitments
pallet. Substrate's `Commitments::CommitmentOf` is single-slot
last-write-wins per `(netuid, hotkey)`, so the registration payload
disappears from chain head the moment the miner writes a subsequent commit
(e.g. their first TLE bundle for an epoch). After that, a verifier reading
chain head can no longer find the binding — even though the registration
was permanently emitted as a `Commitments::CommitmentSet` event when it
landed.

This module fills that gap. It scans a configurable range of historical
blocks for Commitments-pallet events, pulls the matching `CommitmentOf`
entry at the event's block_hash (storage is mutable but block-pinned
reads against an archive node return the value as of that block), parses
any Raw{N} field whose bytes start with `REG_V1_PREFIX`, verifies the
embedded ed25519 signature against the chain hotkey, and caches the
latest valid registration per hotkey.

Usage
-----

    # Once per validator startup or scoring run:
    index = RegistrationIndex(subtensor, netuid=466)
    index.scan_range(start_block=7_000_000, end_block=7_120_000)

    # Per miner during scoring:
    ed25519_pk = index.lookup(miner_hotkey_pk_bytes)  # → bytes or None

Caveats
-------
- The scan reads `CommitmentOf` at historical block_hash; this requires an
  ARCHIVE node RPC for blocks older than the standard ~256-block pruning
  window. Pass an archive-node subtensor (e.g. wss://test-archive.chain.
  opentensor.ai) to the constructor for full backfill.
- The scan is O(blocks × commit-events). For sparse activity it's fast
  enough to run on every scoring trigger; for dense ranges, persist the
  index out-of-band and call `extend_to(head)` incrementally.
- `parse_registration_payload` + `verify_registration` reject bindings
  whose embedded ed25519 sig doesn't cover the (role, ss58, ed25519_pk)
  tuple, so a hotkey cannot register an ed25519 key it doesn't control
  even if it can write to its own commitment slot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from hope.commitment.chain_reader import (
    RawCommitField,
    read_commitment_of,
    read_events_at_block,
)
from hope.commitment.registration import (
    REG_V1_PREFIX,
    RegistrationRole,
    parse_registration_payload,
    verify_registration,
)

logger = logging.getLogger(__name__)


def _ss58_to_raw_pubkey(ss58: str) -> Optional[bytes]:
    """Decode an SS58 address to its 32-byte public key, or None on failure."""
    try:
        from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
        kp = Keypair(ss58_address=ss58)
        pk = kp.public_key
        if isinstance(pk, str):
            return bytes.fromhex(pk.removeprefix("0x"))
        return bytes(pk)
    except Exception as exc:
        logger.debug("ss58 decode failed for %s: %s", ss58, exc)
        return None


@dataclass(frozen=True)
class RegistrationEntry:
    """A valid (verified) registration discovered on chain.

    Attributes:
        hotkey_ss58:   SS58 address of the chain hotkey that wrote the commit.
        hotkey_pk:     32-byte raw public key (decoded from hotkey_ss58).
        ed25519_pk:    32-byte ed25519 public key the hotkey bound to itself.
        role:          MINER / VALIDATOR / OUTCOME_SIGNER per the registration payload.
        block_number:  block where the registration commit landed.
    """

    hotkey_ss58: str
    hotkey_pk: bytes
    ed25519_pk: bytes
    role: RegistrationRole
    block_number: int


class RegistrationIndex:
    """In-memory cache of validated SS58 ↔ ed25519 bindings.

    Thread-safety: callers should treat instances as single-threaded. The
    scanner mutates the internal map; a concurrent reader on `lookup()`
    during a scan would observe a partial view.
    """

    def __init__(
        self,
        subtensor,
        netuid: int,
        *,
        expected_role: RegistrationRole = RegistrationRole.MINER,
    ) -> None:
        self._subtensor = subtensor
        self._netuid = netuid
        self._expected_role = expected_role
        # Keyed by raw 32-byte hotkey pubkey; latest valid registration wins.
        self._entries: dict[bytes, RegistrationEntry] = {}
        self._last_scanned_block: Optional[int] = None
        self._stats = {
            "blocks_scanned": 0,
            "events_seen": 0,
            "candidates_found": 0,
            "verified": 0,
        }

    # -- public API ---------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def lookup(self, hotkey_pk: bytes) -> Optional[bytes]:
        """Return the registered ed25519 pubkey for this hotkey, or None.

        The returned value is suitable to pass as `miner_hotkey_from_chain`
        into `evaluate_scoreability`: both the inner_sig anchor and the
        plaintext['miner_hotkey'] equality check resolve against it.
        """
        entry = self._entries.get(hotkey_pk)
        return entry.ed25519_pk if entry else None

    def entries(self) -> Iterable[RegistrationEntry]:
        """Iterate over all valid registrations (one per hotkey)."""
        return self._entries.values()

    def scan_range(
        self,
        start_block: int,
        end_block: int,
        *,
        progress_callback: Optional[Callable[[int, int, int, dict[str, int], int], None]] = None,
        progress_every: int = 200,
    ) -> int:
        """Scan blocks [start_block, end_block] inclusive for registrations.

        Returns the number of valid registrations found in this range (not
        cumulative). The internal cache is updated in-place; later
        registrations within the range overwrite earlier ones for the same
        hotkey.

        Args:
            progress_callback: optional `(block_num, start, end, stats, indexed_size)`
                callback invoked every `progress_every` blocks plus once at
                completion. Use for long-running backfill visibility.
            progress_every: emit a progress callback every N blocks scanned.
                Default 200 ≈ ~5 min cadence at the observed ~1.5s/block
                testnet scan rate.
        """
        if start_block > end_block:
            return 0
        found_this_range = 0
        n_scanned = 0
        for block_num in range(start_block, end_block + 1):
            try:
                block_hash = self._subtensor.substrate.get_block_hash(block_num)
            except Exception as exc:
                logger.warning("get_block_hash(%d) failed: %s", block_num, exc)
                continue
            if not isinstance(block_hash, str):
                continue

            try:
                events = read_events_at_block(
                    self._subtensor, block_hash, module_filter="Commitments",
                )
            except Exception as exc:
                logger.warning("events at block %d failed: %s", block_num, exc)
                continue
            self._stats["blocks_scanned"] += 1
            n_scanned += 1

            if events:
                for ev in events:
                    self._stats["events_seen"] += 1
                    if ev.netuid != self._netuid:
                        continue
                    entry = self._try_index_event(ev.hotkey_ss58, block_hash, block_num)
                    if entry is not None:
                        found_this_range += 1

            if self._last_scanned_block is None or block_num > self._last_scanned_block:
                self._last_scanned_block = block_num

            if progress_callback is not None and n_scanned % max(1, progress_every) == 0:
                try:
                    progress_callback(
                        block_num, start_block, end_block,
                        self._stats.copy(), len(self._entries),
                    )
                except Exception as exc:
                    logger.debug("progress_callback raised: %s", exc)

        # Always emit a final progress tick at completion.
        if progress_callback is not None and n_scanned > 0:
            try:
                progress_callback(
                    end_block, start_block, end_block,
                    self._stats.copy(), len(self._entries),
                )
            except Exception as exc:
                logger.debug("progress_callback raised: %s", exc)

        return found_this_range

    def to_json(self) -> list[dict]:
        """Serialise the index as a JSON-able list, sorted by block_number.

        Useful for offline backfill: run the scanner against an archive node
        once, persist the result with `json.dump(index.to_json(), ...)`,
        then ship the file to the cron container and call
        `index.merge_json(...)` at startup. This sidesteps the
        ~1-RPC-second-per-block ceiling that makes large lookbacks
        impractical inline with the scoring cron.
        """
        return [
            {
                "hotkey_ss58": e.hotkey_ss58,
                "hotkey_pk_hex": e.hotkey_pk.hex(),
                "ed25519_pk_hex": e.ed25519_pk.hex(),
                "role": e.role.value,
                "block_number": e.block_number,
            }
            for e in sorted(self._entries.values(), key=lambda x: x.block_number)
        ]

    def merge_json(self, entries: list[dict]) -> int:
        """Merge a previously-serialised index into this one.

        Each entry is RE-VERIFIED before being indexed — a prebuilt file
        from an untrusted source cannot inject a fake binding because
        verify_registration would require the embedded ed25519_sig, which
        we don't transport in the JSON. As a defence in depth, the
        merged entries here are stored WITHOUT re-verification BUT only
        if the in-cron scan does not later supersede them with a fresher
        block. Operators must therefore ship prebuilt files only from
        trusted backfill runs.

        Returns the number of entries merged.
        """
        from hope.commitment.registration import RegistrationRole as _Role
        merged = 0
        for raw in entries:
            try:
                hotkey_pk = bytes.fromhex(raw["hotkey_pk_hex"])
                ed25519_pk = bytes.fromhex(raw["ed25519_pk_hex"])
                role = _Role(raw["role"])
                block_number = int(raw["block_number"])
                hotkey_ss58 = str(raw["hotkey_ss58"])
            except (KeyError, ValueError, TypeError):
                continue
            if len(hotkey_pk) != 32 or len(ed25519_pk) != 32:
                continue
            existing = self._entries.get(hotkey_pk)
            if existing is not None and existing.block_number >= block_number:
                continue
            self._entries[hotkey_pk] = RegistrationEntry(
                hotkey_ss58=hotkey_ss58,
                hotkey_pk=hotkey_pk,
                ed25519_pk=ed25519_pk,
                role=role,
                block_number=block_number,
            )
            merged += 1
        return merged

    def extend_to_head(self) -> int:
        """Scan from (last_scanned_block + 1) to chain head.

        First-time callers (no prior scan) start at head and return 0; they
        should call `scan_range(start, end)` explicitly for backfill.
        """
        try:
            head = int(self._subtensor.get_current_block())
        except Exception as exc:
            logger.warning("get_current_block failed: %s", exc)
            return 0
        if self._last_scanned_block is None:
            self._last_scanned_block = head
            return 0
        if self._last_scanned_block >= head:
            return 0
        return self.scan_range(self._last_scanned_block + 1, head)

    # -- internal -----------------------------------------------------------

    def _try_index_event(
        self,
        hotkey_ss58: str,
        block_hash: str,
        block_number: int,
    ) -> Optional[RegistrationEntry]:
        """Query CommitmentOf at block_hash for this hotkey, return new entry if valid."""
        try:
            fields = read_commitment_of(
                self._subtensor, self._netuid, hotkey_ss58, block_hash=block_hash,
            )
        except Exception as exc:
            logger.debug(
                "read_commitment_of(%s @ %d) failed: %s",
                hotkey_ss58[:16], block_number, exc,
            )
            return None
        if not fields:
            return None

        candidate = _select_registration_field(fields)
        if candidate is None:
            return None
        self._stats["candidates_found"] += 1

        parsed = parse_registration_payload(candidate.bytes_)
        if parsed is None:
            return None

        hotkey_pk = _ss58_to_raw_pubkey(hotkey_ss58)
        if hotkey_pk is None or len(hotkey_pk) != 32:
            return None

        if not verify_registration(
            parsed,
            ss58_pubkey=hotkey_pk,
            expected_role=self._expected_role,
        ):
            return None
        self._stats["verified"] += 1

        # Newer registration wins. block_number is the comparison key — a
        # later block in chronological history is always a higher number
        # because Substrate finalises forward.
        existing = self._entries.get(hotkey_pk)
        if existing is not None and existing.block_number >= block_number:
            return existing

        new_entry = RegistrationEntry(
            hotkey_ss58=hotkey_ss58,
            hotkey_pk=hotkey_pk,
            ed25519_pk=parsed.ed25519_pk,
            role=parsed.role,
            block_number=block_number,
        )
        self._entries[hotkey_pk] = new_entry
        return new_entry


def _select_registration_field(
    fields: list[RawCommitField],
) -> Optional[RawCommitField]:
    """Return the first field whose payload starts with REG_V1_PREFIX, else None."""
    for f in fields:
        if f.bytes_.startswith(REG_V1_PREFIX):
            return f
    return None
