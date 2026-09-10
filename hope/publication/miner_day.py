"""One miner's view of a day's receipt: the body of GET /v1/daily/{day}/miner/{hotkey}.

Pure and shared. The validator route serves it, and the mirror sync renders it
for days whose receipt is published to object storage — where there is no
parsed copy for the mirror to filter — so both hosts answer with the same body.
"""

from __future__ import annotations

NO_ENTRIES_NOTE = ("no scored entries for this hotkey on this day — the "
                   "day WAS scored, this miner has no entries in it")


def _document(envelope: dict, metrics: dict, day: str, hotkey: str,
              mine: list[dict]) -> dict:
    if not mine:
        # Present-but-empty is a REAL answer, and a different one from 404:
        # the day exists and this hotkey scored nothing in it (delivered no
        # predictions, or everything it predicted was censored).
        return {"day": day, "miner": hotkey, "entries": [], "entries_total": 0,
                "receipt_sha256": envelope.get("sha256"),
                "note": NO_ENTRIES_NOTE,
                "miners_scored_that_day": metrics.get("miners")}
    scores = [e["score"] for e in mine]
    return {
        "day": day, "miner": hotkey,
        "entries": mine,
        "entries_total": len(mine),
        "mean_score": round(sum(scores) / len(scores), 6),
        "receipt_sha256": envelope.get("sha256"),
        "formula": metrics.get("formula"),
        "how_to_verify": (
            f"python scripts/verify_day.py --url <this validator> "
            f"--day {day} --miner {hotkey}"
        ),
    }


def miner_day_document(envelope: dict, day: str, hotkey: str) -> dict:
    """The route body for one hotkey."""
    metrics = envelope.get("document", {}).get("metrics", {})
    mine = [e for e in metrics.get("entries", []) if e.get("miner") == hotkey]
    return _document(envelope, metrics, day, hotkey, mine)


def miner_day_documents(envelope: dict, day: str) -> dict[str, dict]:
    """The route body for every hotkey with entries, in one pass over the
    entries — a receipt carries more than a hundred thousand of them."""
    metrics = envelope.get("document", {}).get("metrics", {})
    by_miner: dict[str, list[dict]] = {}
    for entry in metrics.get("entries", []):
        miner = entry.get("miner")
        if isinstance(miner, str) and miner:
            by_miner.setdefault(miner, []).append(entry)
    return {hotkey: _document(envelope, metrics, day, hotkey, mine)
            for hotkey, mine in by_miner.items()}
