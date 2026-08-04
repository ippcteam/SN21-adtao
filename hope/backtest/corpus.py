"""Corpus loader for the backtest gate — reads the exported release JSONs
(episodes + settled outcomes) into OutcomeRow truth + episode payload stubs.
The efficiency metric maps to cpa_delta_pct in the outcome tables."""

from __future__ import annotations

import json
from hope.backtest.gate import OutcomeRow


def load_outcomes(path: str) -> list[OutcomeRow]:
    d = json.load(open(path))
    rows: list[OutcomeRow] = []
    for rel in d.get("releases", {}).values():
        for ep in rel.get("episodes", []):
            for o in ep.get("outcomes", []):
                if o.get("cost_delta_pct") is None:
                    continue
                rows.append(OutcomeRow(
                    episode_id=str(ep.get("candidate_key") or ep.get("id")),
                    horizon_days=int(o["horizon_days"]),
                    cost_delta_pct=float(o["cost_delta_pct"]),
                    conversions_delta_pct=float(o["conversions_delta_pct"] or 0),
                    efficiency_delta_pct=float(o["cpa_delta_pct"] or 0),
                ))
    return rows
