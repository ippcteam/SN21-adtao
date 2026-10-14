"""Standing index (14 Oct 2026): standings read seven fields per receipt entry
from a small per-receipt index instead of parsing every whole receipt.

The index must never change a standing: same entries, same values, and a
rewritten receipt is re-read rather than served stale."""
from __future__ import annotations

import json
import os
from datetime import date

from hope.scoring import standing_method as sm

ENV = {"SN21_STANDING_MODE": "episode_relative", "SN21_STANDING_EFFECTIVE_FROM": "2026-09-01"}


def _receipt(root, day, entries):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{day}.json")
    with open(p, "w") as f:
        json.dump({"document": {"day": day, "metrics": {"entries": entries}}, "sha256": "x"}, f)
    return p


def _e(miner, ep, h, score, fo, **extra):
    return {"miner": miner, "episode_id": ep, "horizon_days": h, "score": score,
            "finalized_on": fo, "prediction": {"cost_delta_pct": {"p50": 0.1}},
            "components": {"quantile": 0.5}, **extra}


def _ledger(root):
    _receipt(root, "2026-10-01", [_e("A", "e1", 7, 0.6, "2026-10-01", weight=0.35),
                                  _e("B", "e1", 7, 0.4, "2026-10-01", weight=0.35)])
    _receipt(root, "2026-10-02", [_e("A", "e2", 14, 0.7, "2026-10-02", predicted_on="2026-09-15"),
                                  _e("B", "e2", 14, 0.2, "2026-10-02", predicted_on="2026-09-15")])


def _flat(by_hotkey):
    return {hk: sorted((e.score, e.scored_on, e.weight, e.predicted_on, e.aged_from) for e in v)
            for hk, v in by_hotkey.items()}


class TestSameStandings:
    def test_indexed_read_equals_full_parse(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _ledger(root)
        day = date(2026, 10, 3)
        sm._CACHE.clear()
        monkeypatch.setattr(sm, "_entries_of", lambda p: sm._parse_receipt_entries(p))
        full = _flat(sm.load_relative_entries(root, day, 28, environ=ENV))
        monkeypatch.undo()
        sm._CACHE.clear()
        assert sm.ensure_standing_indexes(root) == 2
        indexed = _flat(sm.load_relative_entries(root, day, 28, environ=ENV))
        assert full and indexed == full

    def test_entries_carry_only_the_standing_fields(self, tmp_path):
        root = str(tmp_path)
        _ledger(root)
        got = sm._entries_of(os.path.join(root, "receipts", "2026-10-02.json"))
        assert set(got[0]) == set(sm.STANDING_FIELDS)
        assert got[0]["predicted_on"] == "2026-09-15" and got[0]["score"] == 0.7


class TestIndexFreshness:
    def test_reads_never_write_the_index(self, tmp_path):
        root = str(tmp_path)
        _ledger(root)
        sm._entries_of(os.path.join(root, "receipts", "2026-10-01.json"))
        assert not os.path.exists(os.path.join(root, "standing_index"))

    def test_a_built_index_is_used(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _ledger(root)
        p = os.path.join(root, "receipts", "2026-10-01.json")
        sm.ensure_standing_indexes(root)
        calls = []
        real = sm._parse_receipt_entries
        monkeypatch.setattr(sm, "_parse_receipt_entries", lambda q: calls.append(q) or real(q))
        sm._entries_of(p)
        assert calls == []

    def test_rewritten_receipt_is_reindexed(self, tmp_path):
        root = str(tmp_path)
        p = _receipt(root, "2026-10-01", [_e("A", "e1", 7, 0.6, "2026-10-01")])
        sm.ensure_standing_indexes(root)
        assert sm._entries_of(p)[0]["score"] == 0.6
        _receipt(root, "2026-10-01", [_e("A", "e1", 7, 0.9, "2026-10-01"),
                                      _e("B", "e1", 7, 0.1, "2026-10-01")])
        os.utime(p, ns=(1, 1))  # a different stamp even on a coarse clock
        got = sm._entries_of(p)
        assert [g["score"] for g in got] == [0.9, 0.1]

    def test_corrupt_index_falls_back_to_the_receipt(self, tmp_path):
        root = str(tmp_path)
        _ledger(root)
        p = os.path.join(root, "receipts", "2026-10-01.json")
        sm.ensure_standing_indexes(root)
        with open(sm._standing_index_path(p), "w") as f:
            f.write("{not json")
        assert [g["miner"] for g in sm._entries_of(p)] == ["A", "B"]

    def test_unwritable_index_still_builds_nothing_and_answers(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _ledger(root)
        monkeypatch.setattr(sm.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("read-only")))
        sm.ensure_standing_indexes(root)
        got = sm._entries_of(os.path.join(root, "receipts", "2026-10-01.json"))
        assert len(got) == 2


class TestEnsure:
    def test_builds_missing_then_nothing(self, tmp_path):
        root = str(tmp_path)
        _ledger(root)
        assert sm.ensure_standing_indexes(root) == 2
        assert sm.ensure_standing_indexes(root) == 0

    def test_no_receipts_dir(self, tmp_path):
        assert sm.ensure_standing_indexes(str(tmp_path)) == 0

    def test_index_dir_is_not_read_as_receipts(self, tmp_path):
        root = str(tmp_path)
        _ledger(root)
        sm.ensure_standing_indexes(root)
        assert [d for d, _ in sm._receipt_files(root, date(2026, 10, 3), 28)] == [
            date(2026, 10, 1), date(2026, 10, 2)]
