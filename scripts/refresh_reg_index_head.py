#!/usr/bin/env python3
"""Fast head-only refresh of the registration index from current commitments.

The block-scanning builder (`scripts/build_reg_index.py`) needs an ARCHIVE RPC
and walks the chain block-by-block (~1 RPC-second per block), which is slow and
can fall behind. This tool is the complementary *fast* path: it reads each
metagraph hotkey's CURRENT `CommitmentOf` from a lite node, keeps the ones that
carry a valid `sn21-reg-v1` binding for the requested role, and merges them into
the index file.

It is exact for the case that matters most operationally — a miner who has
registered but not yet submitted predictions has NOT overwritten their reg-v1
commitment, so it is still their current commitment and is found here in one
read. (A miner who has since overwritten reg-v1 with a prediction commit is not
re-discovered by this pass, but such a miner was already submitting and is in
the prior index; this tool only ADDS, never drops, existing entries.)

Use it to:
  * unblock freshly-registered miners immediately without a slow archive scan;
  * keep a submission-gate index current at service boot (seeded from the
    committed file), so the gate never serves a stale list.

Every candidate is re-verified with the same `verify_registration` the chain
gate uses — a prebuilt file cannot inject a fake binding.

Usage:
    python -m scripts.refresh_reg_index_head --network finney --netuid 21 \\
        --role miner --index /var/data/sn21-validator/sn21-reg-index.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from hope.commitment.chain_reader import read_commitment_of
from hope.commitment.registration import (
    REG_V1_PREFIX,
    RegistrationRole,
    parse_registration_payload,
    verify_registration,
)
from hope.validator._subtensor import make_subtensor
from hope.validator.registration_index import _ss58_to_raw_pubkey

logger = logging.getLogger("refresh_reg_index_head")

_ROLES = {
    "miner": RegistrationRole.MINER,
    "validator": RegistrationRole.VALIDATOR,
    "outcome_signer": RegistrationRole.OUTCOME_SIGNER,
}


def _load_existing(index_path: str) -> dict:
    """Load the prior index (bare list) keyed by hotkey_pk_hex; {} if absent."""
    try:
        with open(index_path) as f:
            prior = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(prior, list):
        return {}
    return {e["hotkey_pk_hex"]: e for e in prior if "hotkey_pk_hex" in e}


def _atomic_write(index_path: str, entries: list) -> None:
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    tmp = index_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, index_path)


def refresh(index_path: str, network: str, netuid: int,
            role: RegistrationRole) -> tuple[int, int]:
    """Merge current-commitment registrations into the index. Returns (before, after)."""
    sub = make_subtensor(network)
    head = int(sub.get_current_block())
    hotkeys = list(sub.metagraph(netuid).hotkeys)
    logger.info("head=%d hotkeys=%d index=%s role=%s",
                head, len(hotkeys), index_path, role.value)

    by_pk = _load_existing(index_path)
    before = len(by_pk)
    added: list[str] = []

    for hk in hotkeys:
        try:
            fields = read_commitment_of(sub, netuid, hk)
        except Exception as exc:  # one flaky read must not abort the sweep
            logger.debug("read_commitment_of failed for %s: %s", hk, exc)
            continue
        if not fields:
            continue
        payload = next(
            (f.bytes_ for f in fields if f.bytes_.startswith(REG_V1_PREFIX)), None
        )
        if payload is None:
            continue
        parsed = parse_registration_payload(payload)
        if parsed is None:
            continue
        pk = _ss58_to_raw_pubkey(hk)
        if pk is None:
            continue
        if not verify_registration(parsed, ss58_pubkey=pk, expected_role=role):
            continue
        pk_hex = pk.hex()
        if pk_hex not in by_pk:
            by_pk[pk_hex] = {
                "hotkey_ss58": hk,
                "hotkey_pk_hex": pk_hex,
                "ed25519_pk_hex": parsed.ed25519_pk.hex(),
                "role": role.value,
                "block_number": head,
            }
            added.append(hk)

    merged = sorted(by_pk.values(), key=lambda e: e["block_number"])
    _atomic_write(index_path, merged)
    logger.info("refresh complete: before=%d after=%d added=%d",
                before, len(merged), len(added))
    for hk in added:
        logger.info("  + %s", hk)
    return before, len(merged)


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--network", default="finney",
                   help="Subtensor network or wss:// URL (overridden by SN21_SUBTENSOR_URL).")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--role", choices=list(_ROLES), default="miner")
    p.add_argument("--index", required=True,
                   help="Path to the reg-index JSON (bare list). Existing entries are "
                        "preserved (merge, never drop); new current-commitment "
                        "registrations are added.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    # bittensor's import hijacks stdlib logging; restore a visible handler.
    from hope.validator._log import configure_logging
    configure_logging(logger, args.log_level)

    try:
        refresh(args.index, args.network, args.netuid, _ROLES[args.role])
    except Exception as exc:
        logger.error("refresh failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
