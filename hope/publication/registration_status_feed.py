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
             will verdict it) — nothing runs until it is admitted, unless
             running_digest names the hotkey's previous admitted model,
             which keeps running meanwhile (SN21_KEEP_LAST_ADMITTED_FROM)
  rejected — current on-chain digest was judged and did not pass the baseline
             (running_digest as for pending)
  none     — no model commitment on chain for this hotkey

`build_registration_status_document` opens a subtensor and reads the ledger; the
pure `assemble` does the join and is what the tests pin. Any chain/ledger error
is the caller's to swallow — this is a convenience feed, never a blocker.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone


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

    kept = registry.get("kept_last_admitted") or {}
    for hk, digest in (registry.get("pending_admission") or {}).items():
        vs = verdict_status_by_hex.get(_hex(digest))
        status = "rejected" if vs and "reject" in str(vs).lower() else "pending"
        rec = {
            "hotkey": hk,
            "uid": uid_by_hotkey.get(hk),
            "current_digest": _short(digest),
            "status": status,
            "scored": hk in scored_hotkeys,
            "first_settle_date": None,
        }
        if hk in kept:
            # The hotkey's previous admitted model keeps running meanwhile.
            rec["running_digest"] = _short(getattr(kept[hk], "image_digest", None))
        out.append(rec)

    out.sort(key=lambda r: (r["uid"] if r["uid"] is not None else 1 << 30, r["hotkey"]))
    return out


def add_unchanged_days(statuses: list[dict], record: dict, as_of: str,
                       start: str | None = None) -> list[dict]:
    """Each miner's days with its current model, and the first day it is not run
    (Sir Rob, KzoNlmEF, 2 Nov 2026: a miner must be able to see them). Only for a
    hotkey whose committed digest is the one it has been running; a model that
    has not run yet has no days. `removed_on` is set once it is not run any more.
    `start` is the rule's first day: no model stops before it, so a model past
    its 40 days before then shows that day."""
    from hope.backtest import unchanged_models as U
    removed = (record or {}).get("removed") or {}
    for rec in statuses:
        entry = ((record or {}).get("hotkeys") or {}).get(rec["hotkey"])
        if not entry or _short(entry.get("digest")) != rec.get("current_digest"):
            continue
        st = U.standing(entry, as_of)
        if not st:
            continue
        rec["model_since"] = st["digest_since"]
        rec["days_unchanged"] = st["days_unchanged"]
        rec["not_run_from"] = max(st["removal_on"], start) if start else st["removal_on"]
        if entry.get("digest") in removed:
            rec["removed_on"] = removed[entry["digest"]]["removed_on"]
    return statuses


def add_liveness(statuses: list[dict], events, states: dict, as_of, params) -> list[dict]:
    """Each hotkey's strikes in the published window, and its eviction with the
    first run that can bring it back, so a miner can see them before they cost
    anything (MINER_MODEL_SPEC §5; a miner's review, 12 Nov 2026: they were in no
    public feed). An eviction from before the rule's first day is not shown: it
    is not enforced and does not count.

    `as_of` is the RUN day (the feed's day). Strikes are kept by basket day, and
    the run of day D has seen baskets up to D-1, so the window is the 14 baskets
    ending D-1 - the count eviction uses (it ended on D, one basket short, until
    12 Nov 2026). `evicted_on` and `returns_from` are run days like every other
    date in this feed: the first run the model is not paid, and the first run
    whose clean result can bring it back (each one day after its basket day)."""
    from hope.scoring import chronic_failure as cf
    run_day = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    basket = run_day - timedelta(days=1)
    eff = params.effective_from
    for rec in statuses:
        hk = rec["hotkey"]
        st = (states or {}).get(hk)
        # While evicted: the strikes in the window that led to it. After a
        # return: those since the eviction, as the next one would count them.
        evicted = st is not None and st.evicted_on is not None
        since = st.last_eviction_on if st is not None and not evicted else None
        if eff is not None and since is not None and since < eff:
            since = None
        rec["strikes"] = cf.strikes_in_window(events, hk, basket, params.window_days, since=since)
        if st is not None and st.evicted_on is not None and (eff is None or st.evicted_on >= eff):
            rec["evicted_on"] = (st.evicted_on + timedelta(days=1)).isoformat()
            if st.reinstate_not_before is not None:
                rec["returns_from"] = (st.reinstate_not_before + timedelta(days=1)).isoformat()
    return statuses


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

    from hope.backtest.chain_commitments import as_registry_reader, bulk_model_commitments
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
    from hope.backtest import last_admitted
    if last_admitted.enabled(as_of):
        registry["kept_last_admitted"] = last_admitted.fallbacks(
            registry, last_admitted.load(ledger_root) or {}, admitted, as_of)

    statuses = assemble(
        registry,
        uid_by_hotkey,
        _verdict_status_by_hex(ledger_root),
        _scored_hotkeys(ledger_root, today),
        _first_settle_by_hotkey(ledger_root),
    )
    # Shown once the rule's date is set (SN21_UNCHANGED_REMOVAL_FROM, set on the
    # notice day), so each miner sees its date during the notice week (Sir Rob,
    # KzoNlmEF, 4 Nov 2026); the record behind it is kept every day by the run.
    from hope.backtest import unchanged_models
    start = unchanged_models.switch_date()
    if start:
        add_unchanged_days(statuses, unchanged_models.load(ledger_root) or {}, as_of, start)
    # Strikes and evictions, once the eviction rule has its date (MINER_MODEL_SPEC §5).
    from hope.scoring import chronic_failure as cf
    from hope.scoring import standing_ledger
    params = cf.params_from_env()
    liveness = None
    if params.effective_from is not None:
        add_liveness(statuses,
                     standing_ledger.load_strike_events(ledger_root, as_of=today - timedelta(days=1),
                                                        window_days=params.window_days),
                     standing_ledger.load_eviction_states(ledger_root), today, params)
        liveness = {"strikes_window_days": params.window_days,
                    "strikes_to_evict": params.strikes_to_evict,
                    "return_after_days": params.repeat_review_days,
                    "rule_from_basket_day": params.effective_from.isoformat()}
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
        **({"liveness": liveness} if liveness else {}),
    }
