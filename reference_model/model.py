#!/usr/bin/env python3
"""SN21 reference model — M0 contract implementation (stdlib only, no network).

Reads one episode payload JSON per line on stdin; writes one prediction JSON
per line on stdout. Strategy: shrunk persistence with corpus-calibrated
spreads, leaning the cost prediction toward budget-change magnitude when
present (the published onboarding recipe: beats the naive baseline on the
held-out corpus by direction skill from action magnitudes).
"""
import json
import sys

# Corpus-calibrated constants (W28-W30 held-out stats, baked at build time —
# the image must be self-contained; no network inside the sandbox).
SPREAD = {"cost_delta_pct": 0.368, "conversions_delta_pct": 0.455, "efficiency_delta_pct": 0.39}
LEAN = {"cost_delta_pct": 0.012, "conversions_delta_pct": 0.008, "efficiency_delta_pct": -0.006}
METRICS = tuple(SPREAD)
HORIZONS = ("7", "14", "28")


def predict(episode: dict) -> dict:
    lean = dict(LEAN)
    if episode.get("action_type") == "BUDGET_CHANGE":
        try:
            f, t = float(episode.get("from_value")), float(episode.get("to_value"))
            if f > 0:
                lean["cost_delta_pct"] = max(min((t - f) / f * 0.5, 2.0), -0.9)
        except (TypeError, ValueError):
            pass
    return {
        h: {
            m: {"p10": round(lean[m] - SPREAD[m] * 0.8, 6),
                "p50": round(lean[m], 6),
                "p90": round(lean[m] + SPREAD[m] * 0.8, 6)}
            for m in METRICS
        } | {"goal_miss_probability": 0.5, "instability_risk": 0.3}
        for h in HORIZONS
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            ep = json.loads(line)
            out = {"episode_id": ep.get("episode_id"), "horizons": predict(ep)}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        except Exception:
            continue  # abstain on malformed input, never crash the basket


if __name__ == "__main__":
    main()
