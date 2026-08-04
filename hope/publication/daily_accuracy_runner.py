"""D11 Feed 1 — the daily publication runner (wiring for accuracy_feed).

Stitches the built pieces into the daily job: the day's finalised
(episode,horizon) results -> build_accuracy_metrics -> rail document
(hash-chained to the feed's previous document) -> ed25519 attestation ->
file store, returning the anchor digest for the existing on-chain
commitment machinery (submit_sha256_commit is invoked by the CALLER —
this module stays chain-free, like the rail).

Clocks (v0.5 §13): "published daily from stream maturity … the stream's
own clock, no extra scheduling." That self-clocking is implemented as:

  - no finalised results AND the feed has never published -> SKIP
    (pre-maturity: days 1-14 produce nothing, so nothing publishes)
  - no finalised results but the feed HAS published before -> publish an
    explicit zero-day document (an honest gap-proof beats a silent hole;
    the chain stays unbroken through thin weekends)
  - results present -> publish the aggregation

Storage: <root>/accuracy/<day>.json (document + attestation envelope),
<root>/accuracy/_head.json (chain head pointer). Append-only by
construction — a day once published is never rewritten; republishing the
same day raises rather than forks the chain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.publication.accuracy_feed import (
    ACCURACY_FEED_NAME,
    build_accuracy_metrics,
)
from hope.publication.rail import attest, build_document, document_sha256
from hope.scoring.daily_score_flow import HorizonResult


def feed_dir(root: str) -> str:
    return os.path.join(root, "accuracy")


def _head_path(root: str) -> str:
    return os.path.join(feed_dir(root), "_head.json")


def _day_path(root: str, day: date) -> str:
    return os.path.join(feed_dir(root), f"{day}.json")


def load_head(root: str) -> Optional[dict]:
    path = _head_path(root)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


@dataclass(frozen=True)
class PublishedDay:
    day: date
    published: bool
    skipped_reason: Optional[str] = None
    anchor_sha256: Optional[str] = None   # what the caller commits on-chain
    path: Optional[str] = None
    zero_day: bool = False


def publish_day(
    root: str,
    day: date,
    results: Iterable[HorizonResult],
    private_key: Ed25519PrivateKey,
    generated_at: str,
) -> PublishedDay:
    """Publish one day of the accuracy feed. Idempotence: a day already on
    disk raises (the feed is append-only; a rewrite would fork the hash
    chain and void every later attestation)."""
    results = list(results)
    head = load_head(root)

    if not results and head is None:
        return PublishedDay(day, False, skipped_reason="pre_maturity")

    if os.path.exists(_day_path(root, day)):
        raise FileExistsError(
            f"accuracy feed already published for {day}; the feed is "
            "append-only (a rewrite forks the hash chain)"
        )

    if head is not None and str(day) <= head["day"]:
        raise ValueError(
            f"accuracy feed head is {head['day']}; publishing {day} out of "
            "order would fork the hash chain (audit 2026-07-29: the "
            "already-published guard alone let an OLDER unpublished day "
            "splice in before the head and break chain_ok for the feed)"
        )

    zero_day = not results
    metrics = build_accuracy_metrics(results)
    if zero_day:
        metrics["zero_day"] = True

    doc = build_document(
        feed=ACCURACY_FEED_NAME,
        as_of=str(day),
        metrics=metrics,
        generated_at=generated_at,
        prev_sha256=head["sha256"] if head else None,
    )
    att = attest(doc, private_key)

    os.makedirs(feed_dir(root), exist_ok=True)
    envelope = {
        "document": doc,
        "sha256": att.sha256,
        "signature": att.signature_hex,
        "public_key": att.public_key_hex,
    }
    path = _day_path(root, day)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(envelope, f, indent=1)
    os.replace(tmp, path)

    head_tmp = _head_path(root) + ".tmp"
    with open(head_tmp, "w") as f:
        json.dump({"day": str(day), "sha256": att.sha256}, f)
    os.replace(head_tmp, _head_path(root))

    return PublishedDay(
        day, True, anchor_sha256=att.sha256, path=path, zero_day=zero_day
    )


def load_feed_documents(root: str) -> list[dict]:
    """Every published document in day order — chain_ok()'s input."""
    d = feed_dir(root)
    if not os.path.isdir(d):
        return []
    docs = []
    for name in sorted(os.listdir(d)):
        if name.startswith("_") or not name.endswith(".json"):
            continue
        with open(os.path.join(d, name)) as f:
            docs.append(json.load(f)["document"])
    return docs
