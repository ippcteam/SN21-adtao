"""The dry-run command writes the preview file and changes nothing else."""

import json
import os
from datetime import date


def _receipt(root, day, entries):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{day}.json"), "w") as f:
        json.dump({"document": {"day": day, "metrics": {"entries": entries}}, "sha256": "x"}, f)


def test_it_writes_the_preview_and_prints_the_summary(tmp_path, monkeypatch, capsys):
    import scripts.standing_preview as sp
    root = str(tmp_path)
    entries = []
    for i in range(60):
        entries.append({"miner": "a", "episode_id": f"n{i}", "horizon_days": 7, "score": 0.6,
                        "finalized_on": "2026-09-13", "predicted_on": "2026-09-05", "weight": 1.0})
        entries.append({"miner": "b", "episode_id": f"n{i}", "horizon_days": 7, "score": 0.4,
                        "finalized_on": "2026-09-13", "predicted_on": "2026-09-05", "weight": 1.0})
    _receipt(root, "2026-09-13", entries)
    with open(os.path.join(root, "model_since.json"), "w") as f:
        json.dump({"a": {"digest": "sha256:1", "since": "2026-09-01"}}, f)
    for k, v in {"SN21_STANDING_MODE": "episode_relative", "SN21_STANDING_HALF_LIFE_DAYS": "7",
                 "SN21_STANDING_WINDOW_DAYS": "28", "SN21_STANDING_PRIOR_MASS": "250",
                 "SN21_PLACEMENT_FLOOR_PREDICTIONS": "50"}.items():
        monkeypatch.setenv(k, v)
    rc = sp.main(["--day", "2026-09-14", "--ledger-root", root, "--top", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "===PREVIEW-END===" in out and "top-20 seats changed" in out
    path = os.path.join(root, "standing_preview", "2026-09-14.json")
    doc = json.load(open(path))
    assert doc["preview"]["age_basis"] == "prediction_day"
    assert doc["preview"]["model_since"] == {"a": "2026-09-01"}
    # nothing else appeared on the disk
    assert sorted(os.listdir(root)) == ["model_since.json", "receipts", "standing_preview"]
