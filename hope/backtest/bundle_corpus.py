"""The admission corpus, built from the published training bundle.

WHY THIS SOURCE

    Admission asks one question: does this image beat the naive baseline on
    real episodes with real settled outcomes? The published training bundle
    carries exactly that — real episodes, real labels — and it is already in
    miners' hands, so using it as the corpus leaks nothing.

    It is NOT a held-out set in the anti-memorisation sense (a miner who
    memorised the bundle would pass). That is a known limitation, tightened
    later by drawing a held-out corpus from settled outcomes the miner never
    saw. For the mechanics gate — "is this a real model that clears the naive
    baseline, and is it deterministic" — the bundle is the right, leak-free
    substrate, and it is what the dry run validated.

SHAPE

    Bundle records are {episode_id, input, labels}. This lifts `input` to the
    live-contract payload (episode_id at the top level) and turns `labels`
    into gate OutcomeRow truth. `efficiency` maps to CPA, as everywhere else.
"""

from __future__ import annotations

import json
import os
import urllib.request

from hope.backtest.gate import OutcomeRow

BUNDLE_URL = ("https://github.com/ippcteam/SN21-adtao/releases/download/"
              "training-bundle-2026-08/SN21_training_bundle.jsonl")
EFFICIENCY_LABEL = "cpa_delta_pct"


def fetch_bundle(dest_dir: str, url: str = BUNDLE_URL) -> str:
    """Download the public bundle once; reuse it if already on disk."""
    path = os.path.join(dest_dir, "training_bundle.jsonl")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    os.makedirs(dest_dir, exist_ok=True)
    tmp = path + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, path)
    return path


def build_from_bundle(bundle_path: str, limit: int = 250):
    """(episodes, outcomes) for the admission gate, from the public bundle.

    episodes: payloads in the live contract shape (episode_id at top level).
    outcomes: gate OutcomeRow list from the bundle labels.
    """
    episodes, outcomes = [], []
    with open(bundle_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_manifest" in rec or "input" not in rec:
                continue
            eid = str(rec.get("episode_id"))
            payload = {"episode_id": eid}
            payload.update(rec["input"])
            labels = rec.get("labels") or {}
            got_label = False
            for horizon, vals in labels.items():
                if not isinstance(vals, dict) or vals.get("cost_delta_pct") is None:
                    continue
                try:
                    h = int(horizon)
                except (TypeError, ValueError):
                    continue
                outcomes.append(OutcomeRow(
                    episode_id=eid, horizon_days=h,
                    cost_delta_pct=float(vals["cost_delta_pct"]),
                    conversions_delta_pct=float(vals.get("conversions_delta_pct") or 0),
                    efficiency_delta_pct=float(vals.get(EFFICIENCY_LABEL) or 0)))
                got_label = True
            if got_label:
                episodes.append(payload)
            if len(episodes) >= limit:
                break
    return episodes, outcomes
