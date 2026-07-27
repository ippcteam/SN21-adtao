"""W3 — D11 daily accuracy-feed runner: self-clocking, chained, attested."""

from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.publication.daily_accuracy_runner import (
    load_feed_documents,
    load_head,
    publish_day,
)
from hope.publication.rail import chain_ok, document_sha256
from hope.scoring.daily_score_flow import HorizonResult

DAY = date(2026, 7, 27)
KEY = Ed25519PrivateKey.generate()


def _results(day, n=5, miner="alpha"):
    return [
        HorizonResult(f"ep{i}", 7, miner, 0.8, day)
        for i in range(n)
    ]


def test_pre_maturity_skips_entirely(tmp_path):
    out = publish_day(str(tmp_path), DAY, [], KEY, "2026-07-27T09:00:00Z")
    assert not out.published
    assert out.skipped_reason == "pre_maturity"
    assert load_head(str(tmp_path)) is None


def test_first_publication_starts_chain(tmp_path):
    root = str(tmp_path)
    out = publish_day(root, DAY, _results(DAY), KEY, "2026-07-27T09:00:00Z")
    assert out.published and not out.zero_day
    docs = load_feed_documents(root)
    assert len(docs) == 1
    assert docs[0]["prev_sha256"] is None
    assert out.anchor_sha256 == document_sha256(docs[0])


def test_zero_day_after_maturity_publishes_explicitly(tmp_path):
    root = str(tmp_path)
    publish_day(root, DAY, _results(DAY), KEY, "t1")
    out = publish_day(root, DAY + timedelta(days=1), [], KEY, "t2")
    assert out.published and out.zero_day
    docs = load_feed_documents(root)
    assert docs[1]["metrics"]["zero_day"] is True
    assert docs[1]["metrics"]["results_total"] == 0


def test_chain_links_across_days_and_verifies(tmp_path):
    root = str(tmp_path)
    for i in range(4):
        d = DAY + timedelta(days=i)
        publish_day(root, d, _results(d, n=3 + i), KEY, f"t{i}")
    docs = load_feed_documents(root)
    assert len(docs) == 4
    assert chain_ok(docs)
    # head tracks the latest anchor
    assert load_head(root)["sha256"] == document_sha256(docs[-1])


def test_republish_same_day_raises(tmp_path):
    root = str(tmp_path)
    publish_day(root, DAY, _results(DAY), KEY, "t1")
    with pytest.raises(FileExistsError):
        publish_day(root, DAY, _results(DAY), KEY, "t2")


def test_metrics_aggregate_without_miner_identities(tmp_path):
    root = str(tmp_path)
    results = _results(DAY, n=4, miner="alpha") + _results(DAY, n=4, miner="beta")
    publish_day(root, DAY, results, KEY, "t1")
    doc = load_feed_documents(root)[0]
    m = doc["metrics"]
    assert m["miners_scored"] == 2
    assert "alpha" not in str(m) and "beta" not in str(m)  # aggregates only
