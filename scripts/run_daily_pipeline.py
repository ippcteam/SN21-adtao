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

def _coldkey_reader():
    """hotkey_ss58 -> coldkey_ss58 from the CURRENT metagraph, so the settle's
    one-coldkey-one-seat cap (Layer 1) can group a principal's hotkeys and keep
    only its best-standing seat before the vector is published.

    Metagraph reads need no wallet, so the keyless executor can do this. A read
    failure returns None: daily_loop then applies NO cap, which is the fail-OPEN
    direction — an identity we could not read must not cost anyone a seat.
    """
    try:
        import bittensor as bt
        net = (os.environ.get("SN21_REG_INDEX_ARCHIVE_URL")
               or os.environ.get("BT_NETWORK") or "finney")
        netuid = int(os.environ.get("SN21_NETUID", "21"))
        mg = bt.Subtensor(network=net).metagraph(netuid)
        cmap = {str(mg.hotkeys[i]): str(mg.coldkeys[i])
                for i in range(len(mg.hotkeys))}
        log(f"[settle] coldkey map: {len(cmap)} hotkeys (one-seat cap active)")
        return cmap
    except Exception as exc:   # noqa: BLE001 — fail OPEN, loudly
        log(f"[settle] coldkey read failed ({exc}) — one-seat cap NOT applied today")
        return None



def _transition_key_provider(ledger_root):
    """episode_id -> transition_key, from payloads already on this disk.

    The executor holds every episode payload it runs (shadow store), so the
    accuracy-by-type stage needs no network and no credentials. Tolerant of
    layout (json/jsonl, basket lines or payload dicts), early-exits once all
    wanted ids are found, and fails soft to {} — daily_loop buckets missing
    rows as UNKNOWN rather than failing the stage.
    """
    import json as _json

    def _extract(d):
        if not isinstance(d, dict):
            return None, None
        eid = d.get("episode_id") or d.get("episode_candidate_id")
        inp = d.get("input") if isinstance(d.get("input"), dict) else d
        tkey = inp.get("transition_key")
        if tkey is None and isinstance(inp.get("payload"), dict):
            pay = inp["payload"]
            eid = eid or (pay.get("episode_metadata") or {}).get("episode_id")
            tkey = ((pay.get("action_bundle") or {}).get("bundle_summary")
                    or {}).get("transition_key")
        return (str(eid) if eid is not None else None), tkey

    def provider(episode_ids):
        wanted = {str(e) for e in episode_ids}
        out = {}
        root = os.path.join(ledger_root, "shadow")
        if not os.path.isdir(root):
            return out
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith((".json", ".jsonl")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > 64 * 1024 * 1024:
                        continue
                    with open(p) as fh:
                        docs = ([_json.loads(ln) for ln in fh if ln.strip()]
                                if fn.endswith(".jsonl") else [_json.load(fh)])
                except Exception:  # noqa: BLE001 — unreadable file is not our stage failing
                    continue
                for d in docs if isinstance(docs, list) else []:
                    eid, tkey = _extract(d)
                    if eid in wanted and tkey and eid not in out:
                        out[eid] = str(tkey)
                if wanted <= set(out):
                    return out
        return out

    return provider


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
    from scripts.post_epoch_report import DEFAULT_ENDPOINT, post_payload

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
    payload = aggregate(artifact, accuracy_by_type=_acc)
    endpoint = os.environ.get("SN21_LEADERBOARD_ENDPOINT") or DEFAULT_ENDPOINT
    resp = post_payload(payload, endpoint=endpoint, api_key=api_key)
    ok = 200 <= resp.status_code < 300
    return {"published": ok, "epoch_id": payload.epoch_id,
            "miners": len(payload.miner_results), "status": resp.status_code}


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
