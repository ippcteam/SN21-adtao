"""The SN21 daily pipeline — one entrypoint, run once a day on the executor.

    python3 -m scripts.run_daily_pipeline [--day YYYY-MM-DD] [--basket BD-...]
        [--ledger-root DIR] [--corpus-size N] [--intake-limit N] [--dry-run]

WHAT IT DOES, IN ORDER (each stage fail-soft, with its own summary line):

  0. resolve   — today's basket (BD-<date>), full episode payloads fetched from
                 the operator data API over HTTP. No operator database login.
  1. intake    — gate every newly-committed model image through the namespace
                 sandbox against the admission corpus; write the admitted-digest
                 set. Idempotent: a digest with a final verdict is never
                 re-gated.
  2. shadow    — execute every ADMITTED model against today's basket in the
                 sandbox; seal the predictions into the shadow ledger BEFORE any
                 outcome exists.
  3. settle    — settle whatever (episode × horizon) rows have matured, score
                 them with the production scorer, fold into standings, publish
                 the day's signed receipt + accuracy document.

WHAT IT DELIBERATELY DOES NOT DO

  - It does NOT set weights on chain. The daily curve stays OFF; the bridge
    (last weekly vector + alpha hold) pays until real daily scores accumulate,
    exactly as the transition plan mandates. Turning it on before scores exist
    would commit an empty vector.
  - It does NOT anchor the feed root on chain (SN21_ANCHOR_COMMITS stays off).
  Both are deliberate future flips, gated on real settled scores, not on this
  pipeline running.

  The single run record under <ledger>/pipeline_runs/ lets a heartbeat check
  that the pipeline actually ran, the way the OBI daily pipeline is watched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest import bundle_corpus  # noqa: E402
from hope.backtest.execution_mode import basket_runner, executor_mode  # noqa: E402
from hope.backtest.gate_service import gate_submission  # noqa: E402
from hope.backtest.intake_runner import run_intake  # noqa: E402
from hope.backtest.shadow import ShadowModel, run_shadow_day  # noqa: E402


def log(msg):
    print(msg, flush=True)


# ---- 0. basket resolution over the operator data API -------------------------

def _api_base_and_key():
    url = (os.environ.get("HOPE_API_URL") or "").strip().rstrip("/")
    key = (os.environ.get("HOPE_API_KEY") or "").strip()
    if not url or not key:
        raise SystemExit("HOPE_API_URL and HOPE_API_KEY are required to fetch "
                         "the basket from the operator data API")
    return url, key


def _api_get(path: str):
    url, key = _api_base_and_key()
    req = urllib.request.Request(f"{url}/internal/bittensor/v1/{path.lstrip('/')}",
                                 headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _api_post(path: str, body: dict):
    url, key = _api_base_and_key()
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{url}/internal/bittensor/v1/{path.lstrip('/')}", data=data,
        method="POST",
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


class BasketNotReady(RuntimeError):
    """The day's own basket is not in the operator listing yet. TRANSIENT by
    definition — the upstream build may simply be late — so the caller must
    NOT write a run record for it (the daemon then retries next tick and the
    day self-heals when the basket lands)."""


def resolve_basket(explicit: str | None, day: date) -> str:
    """The basket release key. Explicit wins; else BD-<yesterday> (a basket is
    named for the day whose changes it holds and delivered the next morning),
    verified to exist in the operator listing.

    FAIL LOUDLY, NEVER FALL BACK (fixed 2026-08-25). This used to fall back to
    the newest BD- release when the day's basket was missing. Three incidents:
    a stale 11:53 run published a wrong-day report (22 Aug), and on 24+25 Aug —
    with the upstream basket build stalled — two consecutive runs silently
    re-predicted the SAME stale 5-episode basket, re-writing an already-locked
    shadow day. A wrong basket is strictly worse than a late one: predictions
    land against episodes nobody meant to serve, and the miss is invisible.
    """
    if explicit:
        return explicit
    candidate = f"BD-{day - timedelta(days=1)}"
    releases = {r.get("release_key") for r in _api_get("releases").get("releases", [])}
    if candidate in releases:
        return candidate
    newest = sorted(r for r in releases if str(r).startswith("BD-"))
    raise BasketNotReady(
        f"{candidate} is not in the operator listing (newest BD- present: "
        f"{newest[-1] if newest else 'none'}). Refusing to run against a stale "
        f"basket; will retry next tick. Pass --basket explicitly to override.")


def fetch_basket_payloads(release_key: str) -> list:
    """Full episode payloads (episode_id + v2.0 blocks) for the basket — the
    same package the validator serves to miners, no outcomes.

    The episode_id is on the OUTER episode object, not inside the payload
    (payload carries account_state / action_bundle / pre_window / … but no id).
    The model contract wants episode_id at the TOP LEVEL of the payload it
    reads on stdin, so we inject it — matching the training-bundle shape the
    models were built against. Getting this wrong drops every episode silently
    (observed 2026-08-12: 328 payloads → 0).
    """
    pkg = _api_get(f"releases/{release_key}/package")
    payloads = []
    for ep in pkg.get("episodes", []):
        payload = ep.get("payload")
        eid = ep.get("episode_id")
        if payload and eid:
            payload = dict(payload)
            payload["episode_id"] = str(eid)
            payloads.append(payload)
    return payloads


# ---- 1. intake ---------------------------------------------------------------

def stage_intake(ledger_root, corpus, key, timeout_s, limit):
    from scripts.run_model_intake import (
        _persist,
        _rewrite_admitted,
        commitments_from_chain,
    )
    from hope.backtest.chain_commitments import (
        as_single_reader,
        bulk_model_commitments,
    )

    import bittensor as bt
    netuid = int(os.environ.get("SN21_NETUID", "21"))
    st = bt.Subtensor(network=os.environ.get("SN21_NETWORK", "finney"))
    hotkeys = list(st.metagraph(netuid=netuid).hotkeys)
    commits = bulk_model_commitments(st, netuid, hotkeys)
    read = as_single_reader(commits)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    run_basket = basket_runner()

    def gate_runner(pinned_ref):
        return gate_submission(
            pinned_ref, corpus[0], corpus[1], generated_at=now,
            private_key=key, timeout_s=timeout_s,
            runner=lambda ref, eps, _t: run_basket(ref, eps))

    # Bound a single sweep so a run cannot be a hundred untrusted container
    # starts unattended. The bound applies to digests that still NEED a
    # verdict: it used to rank ALL commitments earliest-first, so once more
    # than `limit` digests existed on chain the same long-admitted earliest
    # window was selected every day and NEWLY committed digests were starved
    # forever — UID 136/138 (2026-08-24): fixed containers committed a day
    # earlier sat pending_admission while every sweep gated 0. Filtering the
    # already-verdicted out FIRST keeps the container-start cap and gates the
    # earliest-committed of the digests actually awaiting judgement.
    from hope.backtest.intake_runner import verdicted_digests
    commitments, _unparse = commitments_from_chain(hotkeys, read)
    _done = verdicted_digests(ledger_root)
    fresh = [c for c in commitments if c.digest not in _done]
    if limit and len(fresh) > limit:
        block_of = {c.digest: (commits.get(c.hotkey) or (0, ""))[0]
                    for c in fresh}
        fresh = sorted(fresh,
                       key=lambda c: block_of.get(c.digest, 0))[:limit]
    keep = {c.digest for c in fresh}
    reader = lambda hk: (read(hk) if _digest_of(read(hk)) in keep else None)  # noqa: E731

    # In sandbox mode the intake's OWN pull step must not use docker (there is
    # none). The namespace sandbox pulls each image by digest itself and
    # REFUSES any blob whose bytes do not match — the digest verification the
    # docker pre-pull did is not lost, it just moves into the runner. So pass a
    # no-op puller/inspector that let pull_by_digest hand the pinned ref
    # straight to the sandbox gate_runner.
    from hope.backtest.execution_mode import MODE_SANDBOX
    if executor_mode() == MODE_SANDBOX:
        def _sandbox_puller(_pinned):
            return True, ""              # the sandbox does the real pull+verify

        def _sandbox_inspector(pinned):
            return [pinned]             # pinned == repo@digest -> passes the check
        puller, inspector = _sandbox_puller, _sandbox_inspector
    else:
        puller = inspector = None       # docker path unchanged

    result = run_intake(ledger_root, hotkeys, reader, gate_runner,
                        puller=puller, inspector=inspector,
                        persist=_persist(ledger_root))
    admitted = _rewrite_admitted(ledger_root)
    # Per-model verdicts so a rejection sweep is legible: is the gate correctly
    # filtering weak models, or is something systematically wrong?
    reasons = [{"uid": None, "status": d.get("status"),
                "detail": (d.get("detail") or "")[:80]}
               for d in (result.details or [])]
    return {"gated": result.gated, "admitted": result.admitted,
            "rejected": result.rejected, "admitted_total": len(admitted),
            "verdicts": reasons}


def _digest_of(raw):
    if not raw:
        return None
    from hope.backtest.model_registry import parse_model_commitment
    p = parse_model_commitment(raw)
    return p.get("digest") if p else None


# ---- 2. shadow day -----------------------------------------------------------

def stage_shadow(ledger_root, basket_key, episodes, include_reference):
    from scripts.run_shadow_day_bd import (
        REFERENCE_HOTKEY,
        REFERENCE_IMAGE,
        admitted_models,
    )

    # PREDICTIONS ARE LOCKED (added 2026-08-25). A basket day that already
    # has a shadow run marker must never be executed again by the pipeline:
    # on 24+25 Aug the stale-basket fallback made two runs shadow the SAME
    # day, silently re-writing predictions that the contract says lock once.
    # An operator who truly must re-run a day removes the day's _run.json
    # deliberately — the pipeline never does it by accident.
    _guard_day = basket_key.replace("BD-", "")
    from hope.backtest import shadow as _shadow_store
    if _shadow_store.subnet_ran(ledger_root, _guard_day):
        return {"registry": None, "models_run": 0,
                "skipped": (f"shadow day {_guard_day} already ran — "
                            f"predictions are locked; refusing to re-run")}

    as_of = str(date.today() if not hasattr(date, "today") else datetime.now(
        timezone.utc).date())
    models, stats = admitted_models(
        ledger_root, os.environ.get("SN21_NETWORK", "finney"),
        int(os.environ.get("SN21_NETUID", "21")), as_of)
    if include_reference:
        models.append(ShadowModel(hotkey=REFERENCE_HOTKEY,
                                  image_digest=REFERENCE_IMAGE,
                                  admitted_at=as_of))

    day = basket_key.replace("BD-", "")
    run_basket = basket_runner()

    def runner(m, eps):
        return run_basket(m.image_digest, eps)

    summary = run_shadow_day(day, episodes, models, runner, ledger_root)
    return {"registry": stats, "models_run": summary.get("models_run"),
            "results": {hk: r.get("predictions")
                        for hk, r in summary.get("results", {}).items()}}


# ---- 3. settle + publish -----------------------------------------------------

COLDKEY_READ_ATTEMPTS = 3
COLDKEY_READ_BACKOFF_S = 5

# A hung read is not a slow read. bt.Subtensor().metagraph() has no timeout of
# its own, so when the websocket stalls instead of erroring it blocks forever —
# and a retry loop that only catches EXCEPTIONS never gets a turn. That is not
# hypothetical: the settle stage sat silent for eleven minutes mid-run, and the
# day's vector could not publish at all. Failing open needs the read to fail
# first, so it is given a deadline.
COLDKEY_READ_TIMEOUT_S = 90


def _coldkey_reader(attempts=COLDKEY_READ_ATTEMPTS,
                    backoff_s=COLDKEY_READ_BACKOFF_S,
                    timeout_s=COLDKEY_READ_TIMEOUT_S,
                    sleep=time.sleep):
    """hotkey_ss58 -> coldkey_ss58 from the CURRENT metagraph, so the settle's
    one-coldkey-one-seat cap (Layer 1) can group a principal's hotkeys and keep
    only its best-standing seat before the vector is published.

    Metagraph reads need no wallet, so the keyless executor can do this. A read
    failure returns None: daily_loop then applies NO cap, which is the fail-OPEN
    direction — an identity we could not read must not cost anyone a seat.

    RETRIED, because fail-open plus a single attempt is not a policy, it is a
    coin toss. A websocket keepalive timeout on one call is enough to disable
    the cap for a whole day, and the day still publishes and reports clean —
    the cap normally removes ~85 hotkeys, so its silent absence is the largest
    single-day change to who gets paid that this pipeline can make. One
    transient network fault must not decide that.

    An EMPTY map is treated as a failed read, not a successful one. A metagraph
    with no hotkeys is not a subnet with no miners, it is a bad read, and
    passing {} on would let the cap "run" over nothing and report success.
    """
    net = (os.environ.get("SN21_REG_INDEX_ARCHIVE_URL")
           or os.environ.get("BT_NETWORK") or "finney")
    netuid = int(os.environ.get("SN21_NETUID", "21"))
    delay = backoff_s
    last = None

    def _read():
        import bittensor as bt
        mg = bt.Subtensor(network=net).metagraph(netuid)
        return {str(mg.hotkeys[i]): str(mg.coldkeys[i])
                for i in range(len(mg.hotkeys))}

    for attempt in range(1, attempts + 1):
        try:
            # Run behind a deadline. NOT via `with ThreadPoolExecutor(...)`:
            # its __exit__ calls shutdown(wait=True), which blocks until the
            # hung worker finishes and so reinstates exactly the hang the
            # timeout exists to escape. The pool is shut down without waiting
            # and the stuck thread is left to its fate — it is a daemon
            # thread, and the process must not be held hostage by a socket
            # that will not close.
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                cmap = pool.submit(_read).result(timeout=timeout_s)
            finally:
                pool.shutdown(wait=False)
            if not cmap:
                raise ValueError("metagraph returned no hotkeys")
            log(f"[settle] coldkey map: {len(cmap)} hotkeys "
                f"(one-seat cap active, attempt {attempt}/{attempts})")
            return cmap
        except Exception as exc:   # noqa: BLE001 — fail OPEN, loudly
            last = exc
            if attempt < attempts:
                log(f"[settle] coldkey read attempt {attempt}/{attempts} "
                    f"failed ({exc}); retrying in {delay}s")
                sleep(delay)
                delay *= 2

    log(f"[settle] coldkey read failed after {attempts} attempts ({last}) — "
        f"one-seat cap NOT applied today")
    return None



def transition_key_map_from_payloads(payloads) -> dict:
    """episode_id -> transition_key, from the payloads exactly as fetched.

    The id used is the top-level `episode_id` fetch_basket_payloads injects —
    the SAME id the models echo in their predictions and the settle flow
    scores under, so the map matches the scorer's key space by construction.
    """
    out = {}
    for p in payloads:
        if not isinstance(p, dict):
            continue
        eid = p.get("episode_id")
        tkey = ((p.get("action_bundle") or {}).get("bundle_summary")
                or {}).get("transition_key")
        if eid is not None and tkey:
            out[str(eid)] = str(tkey)
    return out


def tkeys_dir(ledger_root: str) -> str:
    return os.path.join(ledger_root, "tkeys")


def write_transition_key_map(ledger_root: str, basket_key: str,
                             payloads) -> int:
    """Persist the basket's episode->transition_key map beside the ledger.

    WHY THIS EXISTS (2026-09-01). The accuracy-by-type page — promised to
    miners in miner_quickstart.md ("win/lose by change type") — showed every
    one of 31,073 scored entries as UNKNOWN. The old provider walked the
    shadow store looking for payload-shaped records, but the shadow store
    holds per-miner PREDICTION rows ({"hotkey", "predictions": {id: ...}})
    and the payloads themselves are fetched over HTTP each morning and were
    never written to disk. The provider scanned real files for a shape they
    do not have, found nothing, and failed soft to {} — so every entry
    bucketed as UNKNOWN and nothing reported a failure.

    The payloads are in memory at resolve time, so the map is captured here:
    one small JSON per basket (~1,500 ids), written before any model runs.
    An episode settles 15-36 days after its basket, so settle reads the map
    from the file written on the episode's OWN basket day — which is why
    these persist per-basket instead of living in the run's memory.
    """
    m = transition_key_map_from_payloads(payloads)
    if not m:
        return 0
    d = tkeys_dir(ledger_root)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{basket_key}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(m, fh, sort_keys=True)
    os.replace(tmp, path)
    return len(m)


def _transition_key_provider(ledger_root):
    """episode_id -> transition_key, from the per-basket maps in tkeys/.

    Reads the small map files write_transition_key_map persists at resolve
    time. Union across all baskets: an episode settling today came from a
    basket 15-36 days ago, so no single day's map is enough. Fails soft to
    partial/{} — daily_loop buckets missing ids as UNKNOWN rather than
    failing the stage, which also covers episodes from baskets that ran
    before this fix existed (backfill: scripts/backfill_transition_key_maps).
    """
    import json as _json

    def provider(episode_ids):
        wanted = {str(e) for e in episode_ids}
        out = {}
        root = tkeys_dir(ledger_root)
        if not os.path.isdir(root):
            return out
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fn)) as fh:
                    m = _json.load(fh)
            except Exception:  # noqa: BLE001 — one bad file must not kill the page
                continue
            if not isinstance(m, dict):
                continue
            for eid, tkey in m.items():
                if eid in wanted and tkey and eid not in out:
                    out[eid] = str(tkey)
            if wanted <= set(out):
                break
        return out

    return provider


def _type_weight_fn(ledger_root):
    """Change-type weight multiplier for new standing entries, or None.

    OFF unless SN21_TYPE_WEIGHTS_FILE names a table that load_table_for_scoring
    accepts — and that loader refuses anything not RATIFIED, so a draft table
    in the env cannot reach scoring. Missing/invalid config downgrades to
    None (weight 1.0 everywhere) with the reason logged: scoring must never
    be half-configured silently.

    The fn resolves episode -> transition_key through the same per-basket
    maps the by-type page uses, so the label an entry is weighted by and the
    label it is displayed under cannot disagree.
    """
    path = (os.environ.get("SN21_TYPE_WEIGHTS_FILE") or "").strip()
    if not path:
        return None
    try:
        from hope.scoring.type_weights import load_table_for_scoring
        table = load_table_for_scoring(path)
    except Exception as e:   # noqa: BLE001 — refuse loudly, run neutrally
        log(f"[type-weights] DISABLED — {e}")
        return None

    tkey_provider = _transition_key_provider(ledger_root)
    cache: dict = {}

    def fn(episode_id):
        eid = str(episode_id)
        if eid not in cache:
            cache[eid] = tkey_provider([eid]).get(eid)
        return table.weight_for(cache[eid])

    log(f"[type-weights] ACTIVE — {path} ({len(table.families)} families, "
        f"window {table.window_start}..{table.window_end})")
    return fn


def stage_settle(ledger_root, day):
    from scripts.run_daily_loop import (
        _basket_volume,
        _key_loader,
        _outcomes_provider,
    )
    from hope.validator.daily_loop import run_daily_loop

    key = _key_loader()
    summary = run_daily_loop(
        shadow_root=ledger_root,
        ledger_root=ledger_root,
        day=day,
        outcomes_provider=_outcomes_provider(),
        key_loader=(lambda: key) if key is not None else None,
        day_volume_provider=_basket_volume,
        chain_committer=None,     # NEVER anchor from this pipeline
        # The executor now computes the intended weight vector (daily-stream flag
        # on), so it is the weight path — apply the one-coldkey-one-seat cap here
        # so the PUBLISHED vector is already fully gated and the committer commits
        # it verbatim.
        coldkey_reader=_coldkey_reader,
        transition_key_provider=_transition_key_provider(ledger_root),
        type_weight_fn=_type_weight_fn(ledger_root),
    )
    # Trim the noisy nested prediction index out of the summary. Keep
    # absence_penalty: it moves standings and, on its first live days, "who was
    # charged and how much" must show in the run log and heartbeat rather than
    # being knowable only from the published penalty file.
    return {k: v for k, v in summary.items()
            if k in ("day", "settle", "receipt", "publish", "weights",
                     "collateral_floor_alpha", "absence_penalty")}


def stage_publish_weights(ledger_root, day):
    """Publish the intended daily weight vector to the operator API so the
    on-chain committer — which cannot see this disk — can fetch and commit it.

    The vector is written by the settle step's daily_loop as
    intended_weights_<day>.json when SN21_DAILY_STREAM_WEIGHTS is set. An absent
    file means nothing to publish (the flag is off, or the day was gated to hold
    the previous vector): a no-op, not an error.
    """
    path = os.path.join(ledger_root, f"intended_weights_{day}.json")
    if not os.path.exists(path):
        return {"published": False, "reason": "no intended_weights file"}
    with open(path) as f:
        intent = json.load(f)
    weights = intent.get("weights") or {}
    if not weights:
        return {"published": False, "reason": "empty vector (gated or no standings)"}
    resp = _api_post("daily/weights", {
        "day": str(day),
        "gated": bool(intent.get("gated", False)),
        "day_episode_volume": intent.get("day_episode_volume"),
        "earning_set_size": intent.get("earning_set_size"),
        "weights": weights,
        "meta": {"champion": intent.get("champion"),
                 "evicted": intent.get("evicted"),
                 # The rule working — one-payer suppression groups and the
                 # tenure-gated list — published with the vector so the
                 # docs' "evidence, not accusation" promise holds off-disk.
                 "collapse_audit": intent.get("collapse_audit") or {}},
    })
    return {"published": True, "hotkeys": len(weights),
            "gated": bool(intent.get("gated", False)),
            "api_ok": resp.get("success")}


def _report_publish_gate():
    """Return (allowed, reason). BOTH locks must be open before a daily report
    reaches the public CMS, so nothing appears on the site ahead of the
    published transition-plan reveal:

      1. SN21_DAILY_REPORT_PUBLISH must be truthy — the master switch, OFF by
         default. The stage runs every day and builds nothing observable until
         this is flipped.
      2. Today (UTC) must be on/after SN21_DAILY_REPORT_NOT_BEFORE (default
         2026-08-18, the first-settlement date on /sn21/daily). A date lock so
         even an early flag flip cannot reveal a day before the calendar says.
    """
    flag = os.environ.get("SN21_DAILY_REPORT_PUBLISH", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False, "publish flag off (SN21_DAILY_REPORT_PUBLISH)"
    not_before = os.environ.get("SN21_DAILY_REPORT_NOT_BEFORE", "2026-08-18")
    try:
        nb = date.fromisoformat(not_before)
    except ValueError:
        return False, f"bad SN21_DAILY_REPORT_NOT_BEFORE ({not_before!r})"
    today = datetime.now(timezone.utc).date()
    if today < nb:
        return False, f"before reveal date {nb.isoformat()}"
    return True, "open"


def _uid_by_hotkey():
    """hotkey_ss58 -> uid from the live metagraph. None on any read failure —
    the report stage then no-ops rather than publish rows with unknown UIDs."""
    try:
        import bittensor as bt
        net = os.environ.get("BT_NETWORK") or "finney"
        netuid = int(os.environ.get("SN21_NETUID", "21"))
        mg = bt.Subtensor(network=net).metagraph(netuid)
        return {str(mg.hotkeys[i]): int(mg.uids[i])
                for i in range(len(mg.hotkeys))}
    except Exception as exc:   # noqa: BLE001
        log(f"[report] metagraph read failed ({exc})")
        return None


def stage_publish_report(ledger_root, day):
    """Build the daily EpochReport from the settled standings and POST it to the
    CMS — the public leaderboard's data source. Reuses the SAME aggregate() ->
    post_payload pipe the weekly path uses (via build_daily_artifact), so the
    site needs no special case.

    GATED. Nothing is posted before the transition-plan reveal (see
    _report_publish_gate). The stage still runs daily so the wiring is exercised;
    it simply reports why it held.
    """
    allowed, reason = _report_publish_gate()
    if not allowed:
        return {"published": False, "gated": True, "reason": reason}

    path = os.path.join(ledger_root, f"intended_weights_{day}.json")
    if not os.path.exists(path):
        return {"published": False, "reason": "no intended_weights file"}
    with open(path) as f:
        intent = json.load(f)
    standings = intent.get("standings") or {}
    if not standings:
        return {"published": False, "reason": "no standings (gated day or empty)"}

    uid_by_hotkey = _uid_by_hotkey()
    if not uid_by_hotkey:
        # Publishing rows with uid=-1 would be rejected by the payload schema;
        # hold rather than post a broken table.
        return {"published": False, "reason": "metagraph unavailable"}

    api_key = os.environ.get("SN21_LEADERBOARD_API_KEY", "")
    if not api_key:
        return {"published": False, "reason": "SN21_LEADERBOARD_API_KEY unset"}

    from hope.reporting.aggregator import aggregate
    from hope.reporting.epoch_artifact import build_daily_artifact
    from scripts.post_epoch_report import DEFAULT_ENDPOINT, post_with_correction

    artifact = build_daily_artifact(
        standings={str(k): float(v) for k, v in standings.items()},
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=len(uid_by_hotkey),
        day=str(day),
    )
    # Accuracy-by-type: attach the day's PUBLIC cut when the 1c stage
    # produced it (fail-soft: a missing or unreadable artifact publishes
    # the report without the block, never blocks the report itself).
    _acc = None
    try:
        _acc_path = os.path.join(ledger_root, "accuracy_by_type", f"{day}.json")
        if os.path.exists(_acc_path):
            with open(_acc_path) as _f:
                _acc = json.load(_f)
    except Exception as _e:  # noqa: BLE001
        log(f"[publish-report] accuracy artifact unreadable ({_e}) — publishing without it")
    # The allocation audit travels with the report so each miner's row can
    # carry the reason a control acted on it. Without this the audit is
    # published only as fleet-level lists, and a miner has to search several
    # arrays — or the leaderboard reader has to guess — to learn why somebody
    # is not being paid.
    # Who is actually paid today. The report's tiers come from standings, which
    # the earning controls never touch, so without this a suppressed miner is
    # published as funded and the policy note under it reads as a contradiction.
    _weights = intent.get("weights") or {}
    _earning = {str(hk) for hk, w in _weights.items() if float(w) > 0}

    # A held day publishes no NEW vector; it does not stop anyone earning.
    # The previous vector stays on chain and the heartbeat keeps re-asserting
    # it, so the miners paid yesterday are still being paid today. Reporting
    # nobody as funded told them their income had stopped when it had not,
    # which is a worse error than the one it replaced.
    #
    # So a held day reports the vector that is actually live: the most recent
    # published one.
    _held = False
    if not _earning:
        import glob as _glob
        import re as _re
        prior = sorted(_glob.glob(os.path.join(ledger_root,
                                               "intended_weights_*.json")))
        for path in reversed(prior):
            m = _re.search(r"intended_weights_(\d{4}-\d{2}-\d{2})\.json$", path)
            if not m or m.group(1) >= str(day):
                continue
            try:
                with open(path) as fh:
                    older = (json.load(fh).get("weights") or {})
            except (OSError, ValueError):
                continue
            got = {str(hk) for hk, w in older.items() if float(w) > 0}
            if got:
                _earning, _held = got, True
                log(f"[publish-report] day held — reporting the live vector "
                    f"from {m.group(1)} ({len(got)} earning)")
                break
    # A gated day pays nobody, and that is a fact about the DAY — no per-miner
    # control can express it, so without saying it here the miners who would
    # otherwise have earned show "not funded" against no reason at all. Which
    # is the one thing every other row on the page now avoids.
    _gated_note = None
    if intent.get("gated") or _held:
        _gated_note = (
            "Today's basket was below the minimum size to score a new "
            "allocation, so no new weights were set. The previous "
            "allocation stays in force on chain and continues to pay — "
            "the rows below show who it pays. Nobody's earnings stopped, "
            "and every model kept running."
        )
    payload = aggregate(artifact, accuracy_by_type=_acc,
                        commentary_markdown=_gated_note,
                        collapse_audit=intent.get("collapse_audit") or {},
                        # An empty vector is passed through as an empty set,
                        # NOT as "leave the tiers alone".
                        #
                        # That was the earlier behaviour and it was wrong. On
                        # a gated day no weights publish and nobody is paid,
                        # so preserving the standings-derived tiers reported
                        # all 119 miners as funded while 96 of the same rows
                        # carried a note saying they were excluded. Nobody
                        # earning reads correctly as nobody funded; everybody
                        # funded is a claim about payment that never happened.
                        earning_set=_earning)
    endpoint = os.environ.get("SN21_LEADERBOARD_ENDPOINT") or DEFAULT_ENDPOINT

    # A re-run of an already-published day gets 409: the epoch is frozen
    # (IA D-13). Posting once and stopping there left the LEDGER and the chain
    # vector corrected while the public leaderboard still showed the
    # superseded numbers — the one surface everybody actually looks at was the
    # only one that did not get the correction. The successor flow already
    # existed for hand-driven posts; the daily path now uses the same one.
    # Report what is actually being published, not just that something was.
    # "miners: 123" is true of a report where every row is funded with no
    # reason on it and of a correct one, which is how a leaderboard that
    # contradicted the weight vector passed as healthy for several runs.
    _funded = sum(1 for m in payload.miner_results if m.tier)
    _reasoned = sum(1 for m in payload.miner_results if m.policies)
    _unexplained_rows = [m for m in payload.miner_results
                         if not m.tier and not m.policies]
    _unexplained = len(_unexplained_rows)
    if _unexplained:
        # Naming the two key spaces because every failure here so far has
        # been one identity written two ways, and the counts alone cannot
        # tell "nobody was acted on" apart from "the lookup missed".
        #
        # The sample must come from an UNEXPLAINED row. It used to be
        # miner_results[0] — the first row of the report, which on a healthy
        # day is a funded miner and tells you nothing about the rows the
        # message is complaining about. A diagnostic that names an unrelated
        # hotkey sends the reader looking in the wrong place, which is worse
        # than printing no hotkey at all.
        _audit = (intent.get("collapse_audit") or {}).get("suppressed") or []
        log(f"[publish-report] {_unexplained} row(s) unfunded with no reason "
            f"— report hotkey={_unexplained_rows[0].hotkey[:12]}.. "
            f"audit hotkey={(str(_audit[0])[:12] + '..') if _audit else 'none'} "
            f"earning hotkey={(sorted(_earning)[0][:12] + '..') if _earning else 'none'}")

    resp, posted_as = post_with_correction(
        artifact, payload, endpoint=endpoint, api_key=api_key)
    ok = 200 <= resp.status_code < 300
    out = {"published": ok, "epoch_id": posted_as,
           "miners": len(payload.miner_results), "funded": _funded,
           "with_reason": _reasoned, "unexplained": _unexplained,
           "status": resp.status_code}
    if posted_as != payload.epoch_id:
        out["supersedes"] = payload.epoch_id
    return out


# ---- run record --------------------------------------------------------------

def write_run_record(ledger_root, record):
    d = os.path.join(ledger_root, "pipeline_runs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{record['day']}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(record, f, indent=1, default=str)
    os.replace(path + ".tmp", path)
    return path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None, help="pipeline day (default: today UTC)")
    p.add_argument("--basket", default=None, help="basket key (default: BD-yesterday)")
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    p.add_argument("--corpus-size", type=int, default=200)
    p.add_argument("--intake-limit", type=int, default=0)
    p.add_argument("--gate-timeout-s", type=int, default=120)
    p.add_argument("--no-reference", action="store_true")
    p.add_argument("--skip-intake", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve + fetch + build corpus; run no models, write nothing")
    args = p.parse_args()

    day = (date.fromisoformat(args.day) if args.day
           else datetime.now(timezone.utc).date())
    os.makedirs(args.ledger_root, exist_ok=True)
    workdir = os.environ.get("SN21_EXECUTOR_WORKDIR", "/tmp/executor")
    os.makedirs(workdir, exist_ok=True)

    started = time.time()
    record = {"day": str(day), "mode": executor_mode(), "stages": {}}
    log("===PIPELINE-START===")
    log(f"[pipeline] day={day} mode={executor_mode()} ledger={args.ledger_root}")

    # 0. resolve + fetch basket
    try:
        basket_key = resolve_basket(args.basket, day)
        episodes = fetch_basket_payloads(basket_key)
        record["stages"]["resolve"] = {"basket": basket_key,
                                       "episodes": len(episodes)}
        log(f"[resolve] basket {basket_key}: {len(episodes)} full payloads")

        # Persist the episode -> transition_key map NOW, while the payloads
        # are in memory. Settle reads it 15-36 days from now to label each
        # scored entry's change type; without it the by-type page reads
        # UNKNOWN for everything (see write_transition_key_map).
        try:
            _n = write_transition_key_map(args.ledger_root, basket_key,
                                          episodes)
            log(f"[tkey-map] {basket_key}: {_n} transition keys persisted")
        except Exception as e:   # noqa: BLE001 — labelling, never fatal
            log(f"[tkey-map] skipped ({e})")

        # 0b. fingerprint indexes — build any missing ones NOW, while this
        # process is still light. The one-payer check at settle then reads
        # only the small index files; deriving them lazily at settle time
        # put a receipt-sized allocation on top of settle's peak.
        try:
            from hope.validator.daily_stream_weights import (
                ensure_fingerprint_indexes,
            )
            _built = ensure_fingerprint_indexes(args.ledger_root)
            log(f"[fingerprint-index] built={_built}")
        except Exception as e:   # noqa: BLE001 — an optimisation, never fatal
            log(f"[fingerprint-index] skipped ({e})")
    except BasketNotReady as e:
        # TRANSIENT: no run record, so the once-per-day daemon guard does not
        # trip and the next hourly tick retries — the day self-heals when the
        # late basket lands instead of burning on a stale one.
        log(f"[resolve] BASKET NOT READY — {e}")
        log("===PIPELINE-END=== (will retry next tick)")
        return 1
    except Exception as e:   # noqa: BLE001
        record["stages"]["resolve"] = {"error": str(e)}
        log(f"[resolve] ERROR {e}")
        write_run_record(args.ledger_root, record)
        log("===PIPELINE-END=== (resolve failed)")
        return 1

    if not episodes:
        log("[resolve] empty basket — nothing to run today (not a failure)")
        record["stages"]["resolve"]["empty"] = True
        write_run_record(args.ledger_root, record)
        log("===PIPELINE-END===")
        return 0

    # corpus for admission
    from scripts.run_daily_loop import _key_loader
    key = _key_loader()
    bundle = bundle_corpus.fetch_bundle(workdir)
    corpus = bundle_corpus.build_from_bundle(bundle, args.corpus_size)
    log(f"[corpus] {len(corpus[0])} episodes, {len(corpus[1])} outcome rows "
        f"(public bundle — mechanics gate)")

    if args.dry_run:
        log("[pipeline] DRY RUN — nothing executed or written past this point")
        log("===PIPELINE-END===")
        return 0

    # 1. intake
    if not args.skip_intake:
        try:
            s = stage_intake(args.ledger_root, corpus, key,
                             args.gate_timeout_s, args.intake_limit)
            record["stages"]["intake"] = s
            log(f"[intake] gated={s['gated']} admitted={s['admitted']} "
                f"rejected={s['rejected']} admitted_total={s['admitted_total']}")
            for v in s.get("verdicts", []):
                log(f"[intake]   verdict {v['status']}: {v['detail']}")
        except Exception as e:   # noqa: BLE001
            record["stages"]["intake"] = {"error": str(e)}
            log(f"[intake] ERROR {e}")

    # 2. shadow day
    try:
        s = stage_shadow(args.ledger_root, basket_key, episodes,
                         include_reference=not args.no_reference)
        record["stages"]["shadow"] = {"registry": s["registry"],
                                      "models_run": s["models_run"],
                                      **({"skipped": s["skipped"]}
                                         if "skipped" in s else {})}
        if "skipped" in s:
            log(f"[shadow] SKIPPED — {s['skipped']}")
        else:
            log(f"[shadow] registry={s['registry']} models_run={s['models_run']}")
    except Exception as e:   # noqa: BLE001
        record["stages"]["shadow"] = {"error": str(e)}
        log(f"[shadow] ERROR {e}")

    # 3. settle + publish
    try:
        s = stage_settle(args.ledger_root, day)
        record["stages"]["settle"] = s
        log(f"[settle] {json.dumps(s, default=str)[:400]}")
        # The 400-char cap above can cut the absence-penalty summary off the
        # log tail; a money-moving rule's charges get their own line.
        if isinstance(s, dict) and "absence_penalty" in s:
            log(f"[absence] {json.dumps(s['absence_penalty'], default=str)[:400]}")
    except Exception as e:   # noqa: BLE001
        record["stages"]["settle"] = {"error": str(e)}
        log(f"[settle] ERROR {e}")

    # 4. publish the intended weight vector for the on-chain committer
    try:
        s = stage_publish_weights(args.ledger_root, day)
        record["stages"]["publish_weights"] = s
        log(f"[publish-weights] {json.dumps(s, default=str)}")
    except Exception as e:   # noqa: BLE001
        record["stages"]["publish_weights"] = {"error": str(e)}
        log(f"[publish-weights] ERROR {e}")

    # 5. publish the daily leaderboard report to the CMS (GATED — nothing is
    #    posted before the transition-plan reveal date; see _report_publish_gate)
    try:
        s = stage_publish_report(args.ledger_root, day)
        record["stages"]["publish_report"] = s
        log(f"[publish-report] {json.dumps(s, default=str)}")
    except Exception as e:   # noqa: BLE001
        record["stages"]["publish_report"] = {"error": str(e)}
        log(f"[publish-report] ERROR {e}")

    # 6. sync the verification feeds to the operator API mirror. The public
    #    validator API serves a different host's ledger, so without this push
    #    the receipts and accuracy documents exist but nobody can fetch them
    #    (found 21 Aug: every daily feed endpoint answered "never published"
    #    while attested documents sat on this disk). Best-effort: a mirror
    #    failure must never fail the pipeline; the next run re-syncs
    #    everything anyway because proofs and the root are re-rendered in
    #    full each time.
    try:
        from hope.publication.mirror_sync import sync_mirror
        _api_url = (os.environ.get("HOPE_API_URL") or "").strip()
        _api_key = (os.environ.get("HOPE_API_KEY") or "").strip()
        _mirror_url = (os.environ.get("SN21_MIRROR_API_URL") or "").strip()
        if _mirror_url:
            _api_url = _mirror_url
        if _api_url and _api_key:
            # Recent days only: receipts and accuracy docs are immutable,
            # so the daily run re-ships a short window plus the always-
            # changing proofs/index/root. Backfills use recent_days=None.
            s = sync_mirror(args.ledger_root, _api_url, _api_key,
                            recent_days=int(os.environ.get(
                                "SN21_MIRROR_SYNC_DAYS", "10")))
            record["stages"]["mirror_sync"] = s
            log(f"[mirror-sync] {json.dumps(s, default=str)}")
        else:
            record["stages"]["mirror_sync"] = {
                "skipped": "HOPE_API_URL/HOPE_API_KEY unset"}
    except Exception as e:   # noqa: BLE001
        record["stages"]["mirror_sync"] = {"error": str(e)}
        log(f"[mirror-sync] ERROR (non-fatal) {e}")

    record["elapsed_s"] = round(time.time() - started, 1)
    path = write_run_record(args.ledger_root, record)
    log(f"[pipeline] run record -> {path}")

    # 5. publish a heartbeat so a watcher that is NOT this box can see the run
    #    happened and whether any stage failed. Best-effort — a heartbeat POST
    #    failure must never fail the pipeline itself.
    try:
        h = publish_pipeline_heartbeat(str(day), record)
        log(f"[heartbeat] {json.dumps(h, default=str)}")
    except Exception as e:   # noqa: BLE001
        log(f"[heartbeat] publish failed (non-fatal): {e}")

    log("===PIPELINE-END===")
    return 0


def publish_pipeline_heartbeat(day, record):
    """POST a compact run summary to the operator API so an independent watcher
    can detect a missed day or a failed stage. `ok` = no stage errored."""
    stages = record.get("stages", {})
    failed = [name for name, s in stages.items()
              if isinstance(s, dict) and s.get("error")]
    ok = not failed
    summary = {
        "mode": record.get("mode"),
        "elapsed_s": record.get("elapsed_s"),
        "failed_stages": failed,
        # keep the summary compact but diagnostic: per-stage keys, errors verbatim
        "stages": {name: (s if isinstance(s, dict) else {"value": s})
                   for name, s in stages.items()},
    }
    resp = _api_post("daily/pipeline-heartbeat",
                     {"day": day, "ok": ok, "summary": summary})
    return {"ok": ok, "failed_stages": failed, "api_ok": resp.get("success")}


if __name__ == "__main__":
    raise SystemExit(main())
