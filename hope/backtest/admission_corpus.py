"""The admission corpus the daily pipeline gates new images on.

WHERE IT COMES FROM

    The operator API serves one frozen held-out corpus at a time: episodes
    never served in any basket, with settled outcomes never published, with
    action windows after the last window in the published training bundle.
    `/admission/corpus` says which corpus is current (key, cutoff, counts,
    sha256); `/admission/corpus/<key>/document` is the gzip'd JSONL the
    held-out builder reads. The pipeline keeps a copy on the ledger disk and
    re-downloads only when the served sha changes.

WHY IT IS CACHED BY SHA

    Every image must be judged on identical input, and a verdict must be
    recomputable later. The document is fetched once per corpus, verified
    against the served sha, and read from disk on every later run; the
    verdict records the key and sha it was judged on.

FALLBACK

    If the API cannot say what the corpus is and no cached copy exists,
    the gate still runs — on the public training bundle, the source it used
    before the held-out corpus was served — and says so in the log and in
    the run record. A day's intake must not stall on a metadata lookup, but
    it must never quietly judge on a different set than it reports.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os

from hope.backtest import bundle_corpus, heldout_corpus

CACHE_DIRNAME = "admission_corpus"
SOURCE_HELDOUT = "held-out"
SOURCE_BUNDLE = "public-bundle"


def cache_dir(ledger_root: str) -> str:
    return os.path.join(ledger_root, CACHE_DIRNAME)


def _cache_path(ledger_root: str, key: str) -> str:
    return os.path.join(cache_dir(ledger_root), f"{key}.jsonl")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_meta(ledger_root: str, meta: dict) -> None:
    path = os.path.join(cache_dir(ledger_root), f"{meta['corpus_key']}.meta.json")
    with open(path + ".tmp", "w") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def _latest_cached(ledger_root: str) -> dict | None:
    """The newest cached corpus's metadata, if any (by key order)."""
    d = cache_dir(ledger_root)
    if not os.path.isdir(d):
        return None
    metas = sorted(n for n in os.listdir(d) if n.endswith(".meta.json"))
    for name in reversed(metas):
        try:
            with open(os.path.join(d, name)) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        path = _cache_path(ledger_root, meta.get("corpus_key", ""))
        if os.path.exists(path) and _sha256_file(path) == meta.get("document_sha256"):
            return meta
    return None


def ensure_cached(ledger_root: str, api_get_json, api_get_bytes, log=print) -> dict | None:
    """Make sure the current corpus is on disk; return its metadata.

    api_get_json(path) -> dict, api_get_bytes(path) -> bytes. Returns None
    when the API cannot name a corpus and nothing usable is cached.
    """
    os.makedirs(cache_dir(ledger_root), exist_ok=True)
    try:
        meta = (api_get_json("admission/corpus") or {}).get("corpus") or {}
    except Exception as e:                                       # noqa: BLE001
        log(f"[corpus] metadata unavailable ({type(e).__name__}: {e}); "
            "using the cached corpus if there is one")
        return _latest_cached(ledger_root)
    key, sha = meta.get("corpus_key"), meta.get("document_sha256")
    if not key or not sha:
        log("[corpus] no active corpus served; using the cached corpus if there is one")
        return _latest_cached(ledger_root)

    path = _cache_path(ledger_root, key)
    if os.path.exists(path) and _sha256_file(path) == sha:
        _write_meta(ledger_root, meta)
        return meta
    try:
        raw = gzip.decompress(api_get_bytes(f"admission/corpus/{key}/document"))
    except Exception as e:                                       # noqa: BLE001
        log(f"[corpus] document fetch failed ({type(e).__name__}: {e}); "
            "using the cached corpus if there is one")
        return _latest_cached(ledger_root)
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        log(f"[corpus] served document sha {got[:12]} != announced {sha[:12]}; "
            "refusing it and using the cached corpus if there is one")
        return _latest_cached(ledger_root)
    with open(path + ".tmp", "wb") as fh:
        fh.write(raw)
    os.replace(path + ".tmp", path)
    _write_meta(ledger_root, meta)
    log(f"[corpus] fetched {key} ({len(raw)} bytes, sha {sha[:12]})")
    return meta


def load_corpus(ledger_root: str, corpus_size: int, api_get_json, api_get_bytes,
                workdir: str, log=print) -> tuple[list[dict], list, dict]:
    """(episodes, outcome rows, info) for the gate.

    info names what the gate ran on: source, key, sha256, cutoff, episode
    and outcome-row counts. The same dict goes into the run record and into
    every verdict judged with it.
    """
    meta = ensure_cached(ledger_root, api_get_json, api_get_bytes, log=log)
    if meta:
        built = heldout_corpus.build(_cache_path(ledger_root, meta["corpus_key"]),
                                     cutoff=str(meta.get("cutoff_date") or "")[:10],
                                     limit=corpus_size)
        info = {"source": SOURCE_HELDOUT, "key": meta["corpus_key"],
                "sha256": meta["document_sha256"], "cutoff": built["cutoff"],
                "episodes": built["episode_count"],
                "outcome_rows": built["outcome_row_count"]}
        return built["episodes"], built["outcomes"], info

    bundle = bundle_corpus.fetch_bundle(workdir)
    episodes, outcomes = bundle_corpus.build_from_bundle(bundle, corpus_size)
    info = {"source": SOURCE_BUNDLE, "key": None, "sha256": _sha256_file(bundle),
            "cutoff": None, "episodes": len(episodes), "outcome_rows": len(outcomes)}
    log("[corpus] WARNING: no held-out corpus available — gating on the public "
        "training bundle for this run")
    return episodes, outcomes, info
