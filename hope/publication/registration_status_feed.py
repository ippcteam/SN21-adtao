"""Per-hotkey model registration status — the public answer to
"my model is admitted but the dashboard shows nothing."

Admission is per DIGEST; being scored needs three more things the admission
feed alone cannot show: the admitted digest must be the one CURRENTLY committed
on chain, the container must actually run, and its first predictions must have
SETTLED (~15 days after the first run). This feed joins those facts into one
per-hotkey record so a miner (and we) can tell "settlement lag" from a real
problem without guessing.

Statuses:
  active   — current on-chain digest is admitted and runnable
  pending  — current on-chain digest is committed but not yet judged (intake
             will verdict it) — nothing runs until it is admitted
  rejected — current on-chain digest was judged and did not pass the baseline
  none     — no model commitment on chain for this hotkey

`build_registration_status_document` opens a subtensor and reads the ledger; the
pure `assemble` does the join and is what the tests pin. Any chain/ledger error
is the caller's to swallow — this is a convenience feed, never a blocker.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone


def _hex(digest: str | None) -> str | None:
    """The bare 64-hex of a digest, however it is spelled (sha256:..., repo@...)."""
    if not digest:
        return None
    s = str(digest)
    if "@" in s:
        s = s.split("@", 1)[1]
    if "sha256:" in s:
        s = s.split("sha256:", 1)[1]
    s = s.strip().lower()
    return s or None


def _short(digest: str | None) -> str | None:
    h = _hex(digest)
    return h[:16] if h else None


def assemble(registry: dict,
             uid_by_hotkey: dict,
             verdict_status_by_hex: dict,
             scored_hotkeys: set,
             first_settle_by_hotkey: dict) -> list[dict]:
    """Join registry + verdicts + settlement into per-hotkey status records.

    Pure: every input is already resolved, so this is fully testable without a
    chain. `registry` is build_registry's output ({active: hk->ShadowModel,
    pending_admission: hk->digest}).
    """
    out: list[dict] = []

    for hk, model in (registry.get("active") or {}).items():
        digest = getattr(model, "image_digest", None)
        scored = hk in scored_hotkeys
        out.append({
            "hotkey": hk,
            "uid": uid_by_hotkey.get(hk),
            "current_digest": _short(digest),
            "status": "active",
            "scored": scored,
            # Only meaningful before the first entries land; once scored, the
            # settlement question is answered by the entries themselves.
            "first_settle_date": None if scored else first_settle_by_hotkey.get(hk),
        })

    for hk, digest in (registry.get("pending_admission") or {}).items():
        vs = verdict_status_by_hex.get(_hex(digest))
        status = "rejected" if vs and "reject" in str(vs).lower() else "pending"
        out.append({
            "hotkey": hk,
            "uid": uid_by_hotkey.get(hk),
            "current_digest": _short(digest),
            "status": status,
            "scored": hk in scored_hotkeys,
            "first_settle_date": None,
        })

    out.sort(key=lambda r: (r["uid"] if r["uid"] is not None else 1 << 30, r["hotkey"]))
    return out


def _first_settle_by_hotkey(ledger_root: str, horizon_days: int = 7) -> dict:
    """hotkey -> first-settle date (ISO) from its earliest real shadow run.

    The first (episode, horizon) a hotkey covers settles
    action_window_end + 1 + horizon + SETTLING_WINDOW days. We use the earliest
    basket day it delivered predictions on as the action window.
    """
    from hope.backtest import shadow as _shadow
    from hope.scoring.settle_day_flow import settle_date

    first_day: dict[str, str] = {}
    for day in _shadow.shadow_days(ledger_root):  # ascending
        for hk, (eps_in, preds_out) in _shadow.day_coverage(ledger_root, day).items():
            if (preds_out or 0) > 0 and hk not in first_day:
                first_day[hk] = day
    out: dict[str, str] = {}
    for hk, day in first_day.items():
        try:
            out[hk] = settle_date(date.fromisoformat(day), horizon_days).isoformat()
        except (ValueError, TypeError):
            continue
    return out


def _verdict_status_by_hex(ledger_root: str) -> dict:
    """digest-hex -> latest verdict status, from the published verdicts doc."""
    try:
        from hope.publication.verdict_feed import build_verdicts_document
        doc = build_verdicts_document(ledger_root)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for v in (doc or {}).get("verdicts", []):
        h = _hex(v.get("digest"))
        if h:
            out[h] = str(v.get("status"))
    return out


def _scored_hotkeys(ledger_root: str, as_of: date) -> set:
    """Hotkeys that already hold at least one scored standing entry."""
    try:
        from hope.scoring import standing_ledger
        return set(standing_ledger.load_entries(ledger_root, as_of=as_of).keys())
    except Exception:
        return set()


def build_registration_status_document(ledger_root: str,
                                       network: str | None = None,
                                       netuid: int | None = None) -> dict:
    """Live per-hotkey registration status. Opens a subtensor — the caller must
    swallow failures (convenience feed)."""
    import bittensor as bt

    from hope.backtest.chain_commitments import (
        as_registry_reader, bulk_model_commitments)
    from hope.backtest.intake_runner import load_admitted
    from hope.backtest.model_registry import build_registry

    network = network or os.environ.get("SN21_NETWORK", "finney")
    netuid = netuid if netuid is not None else int(os.environ.get("SN21_NETUID", "21"))
    today = datetime.now(timezone.utc).date()
    as_of = today.isoformat()

    subtensor = bt.Subtensor(network=network)
    metagraph = subtensor.metagraph(netuid=netuid)
    hotkeys = list(metagraph.hotkeys)
    uid_by_hotkey = {str(metagraph.hotkeys[i]): int(metagraph.uids[i])
                     for i in range(len(hotkeys))}

    admitted = load_admitted(ledger_root)
    commitments = bulk_model_commitments(subtensor, netuid, hotkeys)
    registry = build_registry(hotkeys, as_registry_reader(commitments),
                              admitted, as_of)

    statuses = assemble(
        registry,
        uid_by_hotkey,
        _verdict_status_by_hex(ledger_root),
        _scored_hotkeys(ledger_root, today),
        _first_settle_by_hotkey(ledger_root),
    )
    return {
        "feed": "sn21-registration-status",
        "note": ("per hotkey: the model digest currently committed on chain, "
                 "whether it is admitted (active), awaiting a verdict (pending) "
                 "or rejected, and — for an admitted digest not yet scored — the "
                 "date its first predictions settle. 'active' with a future "
                 "first_settle_date is normal settlement lag, not a fault."),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "statuses": statuses,
        "total": len(statuses),
    }
