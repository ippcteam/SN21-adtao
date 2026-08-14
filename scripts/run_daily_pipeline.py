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


def resolve_basket(explicit: str | None, day: date) -> str:
    """The basket release key. Explicit wins; else BD-<yesterday> (a basket is
    named for the day whose changes it holds and delivered the next morning),
    verified to exist in the operator listing."""
    if explicit:
        return explicit
    candidate = f"BD-{day - timedelta(days=1)}"
    releases = {r.get("release_key") for r in _api_get("releases").get("releases", [])}
    if candidate in releases:
        return candidate
    # Fall back to the newest BD- release the operator lists.
    bd = sorted(r for r in releases if str(r).startswith("BD-"))
    if not bd:
        raise SystemExit("no BD- daily basket found in the operator listing")
    return bd[-1]


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
    # starts unattended. Select the EARLIEST-committed digests (by block) —
    # the most-established models — rather than an arbitrary hotkey sort, so a
    # bounded sweep gates the models most likely to be real.
    commitments, _unparse = commitments_from_chain(hotkeys, read)
    if limit and len(commitments) > limit:
        block_of = {c.digest: (commits.get(c.hotkey) or (0, ""))[0]
                    for c in commitments}
        keep = {c.digest for c in
                sorted(commitments, key=lambda c: block_of.get(c.digest, 0))[:limit]}
        reader = lambda hk: (read(hk) if _digest_of(read(hk)) in keep else None)  # noqa: E731
    else:
        reader = read

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
    )
    # Trim the noisy nested prediction index out of the summary.
    return {k: v for k, v in summary.items()
            if k in ("day", "settle", "receipt", "publish", "weights",
                     "collateral_floor_alpha")}


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
                 "evicted": intent.get("evicted")},
    })
    return {"published": True, "hotkeys": len(weights),
            "gated": bool(intent.get("gated", False)),
            "api_ok": resp.get("success")}


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
                                      "models_run": s["models_run"]}
        log(f"[shadow] registry={s['registry']} models_run={s['models_run']}")
    except Exception as e:   # noqa: BLE001
        record["stages"]["shadow"] = {"error": str(e)}
        log(f"[shadow] ERROR {e}")

    # 3. settle + publish
    try:
        s = stage_settle(args.ledger_root, day)
        record["stages"]["settle"] = s
        log(f"[settle] {json.dumps(s, default=str)[:400]}")
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

    record["elapsed_s"] = round(time.time() - started, 1)
    path = write_run_record(args.ledger_root, record)
    log(f"[pipeline] run record -> {path}")
    log("===PIPELINE-END===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
