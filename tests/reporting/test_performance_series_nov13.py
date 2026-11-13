"""The 30-day Prediction Performance series the website charts (13 Nov 2026)."""
import json

from hope.reporting.performance_series import build_series


def _doc(day, mean, eps):
    return {"as_of": day, "horizons": ["7", "14"], "groups": [
        {"key": "budget", "label": "Budget", "rows": [{"key": "BUDGET:up", "cells": {}}],
         "cells": {"7": {"field_mean": mean, "episodes": eps, "solid": True, "n": 9, "components": {}},
                   "14": {"field_mean": None, "episodes": 0, "solid": False}}}]}


def test_the_series_keeps_a_few_numbers_per_group_and_day(tmp_path):
    for i, d in enumerate(["2026-11-11", "2026-11-12", "2026-11-13"]):
        (tmp_path / f"{d}.json").write_text(json.dumps(_doc(d, 0.5 + i / 100, 100 + i)))
    s = build_series(str(tmp_path), days=2)
    assert s["days"] == ["2026-11-12", "2026-11-13"] and s["as_of"] == "2026-11-13"
    g = s["groups"][0]
    assert g["key"] == "budget" and g["label"] == "Budget" and "rows" not in g
    assert g["series"]["7"] == {"field_mean": [0.51, 0.52], "episodes": [101, 102], "solid": [True, True]}
    assert g["series"]["14"]["solid"] == [False, False]


def test_a_group_missing_on_a_day_is_none_that_day(tmp_path):
    (tmp_path / "2026-11-12.json").write_text(json.dumps({"horizons": ["7"], "groups": []}))
    (tmp_path / "2026-11-13.json").write_text(json.dumps(_doc("2026-11-13", 0.6, 5)))
    g = build_series(str(tmp_path))["groups"][0]
    assert g["series"]["7"]["field_mean"] == [None, 0.6]


def test_no_documents_no_series(tmp_path):
    assert build_series(str(tmp_path / "missing")) is None
    assert build_series(str(tmp_path)) is None


def test_each_change_family_points_to_its_group(tmp_path):
    doc = _doc("2026-11-13", 0.6, 5)
    doc["groups"].append({"key": "combined", "label": "Combined", "cells": {},
                          "rows": [{"key": "BUDGET:up+BID:x"}, {"key": "LOCATION:add"}]})
    (tmp_path / "2026-11-13.json").write_text(json.dumps(doc))
    assert build_series(str(tmp_path))["family_group"] == {"BUDGET": "budget", "LOCATION": "combined"}
