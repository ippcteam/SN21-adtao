"""W1 — the serving API, and the miner's rerun THROUGH it.

The plan's acceptance for W1 is not "the routes return 200". It is: a miner on
another machine reruns a day with one command against the public URL and gets
PASS, and every adversarial case still fails correctly when the documents
arrive over HTTP instead of off our disk. These tests run a real ASGI app in
process and drive verify_day --url against it.
"""

import json
import os
import sys
import threading
import time
from datetime import date

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from hope.publication.daily_accuracy_runner import publish_day
from hope.publication.receipt_feed import receipt_path, run_daily_receipt
from hope.scoring.daily_score_flow import HorizonResult
from hope.scoring.settle_day_flow import SettledHorizon, score_entry_v2
from hope.validator.api.daily import router as daily_router

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from verify_day import verify_day  # noqa: E402

KEY = Ed25519PrivateKey.generate()
DAY = date(2026, 8, 18)
V2 = {"SN21_SETTLE_SCORING_V2": "true"}
METRICS = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")


def _pred(p50=0.4, spread=0.3):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
            for m in METRICS}


def _seed(root, day=DAY, n_eps=4):
    outcomes = [SettledHorizon(f"ep{i}", 7, 0.4, 0.2, -0.1, day)
                for i in range(n_eps)]
    index = {f"ep{i}": {"alice": {"7": _pred()}, "bob": {"7": _pred(0.1)}}
             for i in range(n_eps)}
    results = []
    for o in outcomes:
        actual = {m: getattr(o, m) for m in METRICS}
        for miner in ("alice", "bob"):
            results.append(HorizonResult(
                o.episode_id, 7, miner,
                score_entry_v2(index[o.episode_id][miner]["7"], actual), day))
    rec = run_daily_receipt(root, day, outcomes, index, results, {}, KEY,
                            f"{day}T00:00:00Z", environ=V2)
    publish_day(root, day, results, KEY, f"{day}T00:00:00Z",
                receipt_sha256=rec.sha256)
    return rec


@pytest.fixture
def served(tmp_path):
    """A real HTTP server over a real ledger root. Yields (base_url, root)."""
    root = str(tmp_path)
    _seed(root)
    app = FastAPI()
    app.state.validator = {"ledger_root": root}
    app.include_router(daily_router, prefix="/v1/daily")

    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started, "test server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", root
    server.should_exit = True
    t.join(timeout=5)


# ---- THE ACCEPTANCE TEST -----------------------------------------------------

def test_a_miner_reruns_the_day_over_http_and_gets_pass(served):
    """W1's acceptance, literally: one command, public URL, PASS."""
    url, _ = served
    v = verify_day(url=url, day=str(DAY))
    assert v["ok"], v
    assert v["checks"]["score_reproduction"]["entries_recomputed"] == 8
    assert v["checks"]["score_reproduction"]["mismatches"] == 0


def test_url_and_root_reach_the_same_verdict(served):
    """One code path, two loaders. If these ever diverge, the miner and the
    operator are verifying different things — which is the whole failure this
    endpoint exists to prevent."""
    url, root = served
    a = verify_day(url=url, day=str(DAY))
    b = verify_day(root=root, day=str(DAY))
    assert a["ok"] == b["ok"] is True
    assert a["checks"]["score_reproduction"] == b["checks"]["score_reproduction"]
    assert a["checks"]["attestation"]["sha256_matches_document"] is True
    assert b["checks"]["attestation"]["sha256_matches_document"] is True


def test_tampered_score_is_caught_over_http_too(served):
    """The adversarial case through the wire: served bytes are verified, not
    trusted."""
    url, root = served
    p = receipt_path(root, str(DAY))
    env = json.load(open(p))
    env["document"]["metrics"]["entries"][0]["score"] = 0.999999
    json.dump(env, open(p, "w"))
    v = verify_day(url=url, day=str(DAY))
    assert not v["ok"]
    assert v["checks"]["attestation"]["verdict"] == "FAIL"
    assert v["checks"]["score_reproduction"]["verdict"] == "FAIL"
    assert v["diffs"][0]["published"] == 0.999999


def test_expect_anchor_over_http(served):
    """The full loop a miner runs: read the root from chain, pass it in."""
    url, _root = served
    _status, rootdoc = _get(url, "/v1/daily/root")
    assert verify_day(url=url, day=str(DAY),
                      expect_anchor=rootdoc["feed_root"])["ok"]
    bad = verify_day(url=url, day=str(DAY), expect_anchor="ab" * 32)
    assert bad["checks"]["feed_root"]["verdict"] == "FAIL"
    assert "different history than it anchored" in bad["checks"]["feed_root"]["note"]


def test_proof_and_root_endpoints_agree(served):
    url, _ = served
    _s1, proof = _get(url, f"/v1/daily/{DAY}/proof")
    _s2, rootdoc = _get(url, "/v1/daily/root")
    assert proof["feed_root"] == rootdoc["feed_root"]
    assert proof["leaf_count"] == rootdoc["leaf_count"] == 1


def test_a_day_not_in_the_feed_has_no_proof(served):
    url, _ = served
    status, body = _get(url, "/v1/daily/2026-12-25/proof")
    assert status == 404
    assert body["detail"]["reason"] in ("day_not_in_feed", "feed_empty")


def test_old_days_stay_provable_as_the_feed_grows(tmp_path):
    """THE PROPERTY OPTION B EXISTS FOR. Publish several days, then confirm
    the FIRST day still verifies against the CURRENT root — no archive node,
    no block-pinned read. Option A failed exactly here."""
    from datetime import timedelta
    from hope.publication.feed_root import day_proof, feed_root
    from hope.publication.merkle import verify_proof
    root = str(tmp_path)
    days = [DAY + timedelta(days=i) for i in range(5)]
    for d in days:
        _seed(root, day=d)
    current = feed_root(root)
    for d in days:
        p = day_proof(root, str(d))
        assert verify_proof(p["document_sha256"], p["proof"], current), \
            f"{d} stopped proving after later days landed"


# ---- the endpoints themselves ------------------------------------------------

def _get(url, path):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url}{path}", timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_index_walks_the_chain(served):
    url, _ = served
    status, body = _get(url, "/v1/daily/index")
    assert status == 200
    assert body["count"] == 1
    row = body["days"][0]
    assert row["day"] == str(DAY)
    assert row["receipt_available"] is True
    assert row["receipt_sha256"]


def test_absent_day_explains_itself_rather_than_bare_404(served):
    """A bare 404 makes a miner assume the worst. Every absence names a
    reason — this is the one that would otherwise generate Discord traffic."""
    url, _ = served
    status, body = _get(url, "/v1/daily/2026-12-25/receipt")
    assert status == 404
    d = body["detail"]
    assert d["reason"] in ("out_of_range", "no_scored_results", "pre_maturity",
                           "not_yet_published")
    assert d["day"] == "2026-12-25"
    assert "detail" in d


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "..%2f..%2fetc", "2026-08-18/../../secret",
    "not-a-date", "2026-13-45", "", "2026-08-18.json",
])
def test_path_traversal_and_junk_are_rejected(served, bad):
    """`day` reaches a filesystem path on a PUBLIC endpoint. Anything that is
    not a strict ISO date must be refused before it touches the disk."""
    url, _ = served
    status, _body = _get(url, f"/v1/daily/{bad}/receipt")
    assert status in (400, 404), f"{bad!r} returned {status}"


def test_unset_ledger_root_says_so_instead_of_500(tmp_path):
    """An operator who forgets SN21_LEDGER_ROOT gets an actionable message,
    not a stack trace."""
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.state.validator = {}
    app.include_router(daily_router, prefix="/v1/daily")
    old = os.environ.pop("SN21_LEDGER_ROOT", None)
    try:
        r = TestClient(app, raise_server_exceptions=False).get(
            "/v1/daily/2026-08-18/receipt")
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "ledger_root_unset"
    finally:
        if old is not None:
            os.environ["SN21_LEDGER_ROOT"] = old


# ---- W4: the docs must not promise endpoints or fields that do not exist ----

def test_every_endpoint_the_verify_doc_advertises_actually_exists(served):
    """A trust document that prints a 404 destroys the trust it is selling.
    Extracts the /v1/daily paths from SN21_VERIFYING.md and hits each one."""
    import re
    url, _ = served
    doc = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "docs", "SN21_VERIFYING.md")).read()
    paths = set(re.findall(r"/v1/daily/[\w\-{}<>/.]+", doc))
    assert paths, "doc advertises no endpoints — did the format change?"
    for p in paths:
        concrete = (p.replace("2026-08-18", str(DAY))
                     .replace("<your-hotkey>", "alice"))
        status, _body = _get(url, concrete)
        assert status == 200, f"{p} -> {concrete} returned {status}"


def test_the_diffs_field_the_doc_tells_miners_to_post_is_real(served):
    """The doc says: on a score mismatch, post the `diffs` array naming the
    episode, horizon, miner, published and recomputed score. Pin that shape —
    if it changes, the doc's failure instructions become wrong."""
    url, root = served
    p = receipt_path(root, str(DAY))
    env = json.load(open(p))
    env["document"]["metrics"]["entries"][0]["score"] = 0.5
    json.dump(env, open(p, "w"))
    v = verify_day(url=url, day=str(DAY))
    assert v["diffs"], "no diffs produced for a mismatched score"
    d = v["diffs"][0]
    for field in ("episode_id", "horizon_days", "miner", "published",
                  "recomputed"):
        assert field in d, f"diffs entry missing {field} (the doc names it)"
