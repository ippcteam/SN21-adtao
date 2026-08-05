"""Leaderboard reporting package — pure aggregation from epoch artifacts to
public report payloads.

This package owns the path between a private per-epoch artifact (per-UID
scores + tier result) and the aggregate-only payload posted to the
operator's CMS endpoint. Every module here is:

  - Pure: no I/O, no clock, no randomness, no chain reads. Same input →
    same output forever. The verifier (`scripts/verify_epoch.py`) calls
    the same aggregator so external reproducibility is byte-identical
    to what the operator posts.
  - Aggregate-only: the public payload type is structurally incapable of
    carrying UIDs, hotkeys, or per-miner score detail. Anti-doxxing is
    enforced by the type system, not by a manual filter.

See `docs/SN21_REWARD_MECHANISM.md` for the scoring spec and the data
contract the operator's team consumes (`01-the operator-data-contract.md`) for the
payload shape.
"""
