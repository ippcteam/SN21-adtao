"""The public admission-verdict feed answers "admitted or skipped?"."""

from __future__ import annotations

import json
import os

from hope.backtest.intake_runner import verdict_dir
from hope.publication.verdict_feed import build_verdicts_document


def write_verdict(root, name, body, attested=False):
    d = verdict_dir(str(root))
    os.makedirs(d, exist_ok=True)
    doc = {"document": {"metrics": body}} if attested else body
    with open(os.path.join(d, name), "w") as f:
        json.dump(doc, f)


class TestVerdictFeed:
    def test_reads_both_envelope_shapes(self, tmp_path):
        write_verdict(tmp_path, "a.json",
                      {"hotkey": "5A", "digest": "sha256:aa",
                       "status": "admitted", "detail": "beats_baseline"})
        write_verdict(tmp_path, "b.json",
                      {"hotkey": "5B", "image_digest": "sha256:bb",
                       "status": "rejected_gate", "detail": "run_failed: exit=1"},
                      attested=True)
        doc = build_verdicts_document(str(tmp_path))
        assert doc["total"] == 2
        assert {v["digest"] for v in doc["verdicts"]} == {"sha256:aa",
                                                          "sha256:bb"}

    def test_rejections_carry_trimmed_detail_admissions_do_not(self, tmp_path):
        write_verdict(tmp_path, "a.json",
                      {"hotkey": "5A", "digest": "sha256:aa",
                       "status": "admitted", "detail": "beats_baseline"})
        write_verdict(tmp_path, "b.json",
                      {"hotkey": "5B", "digest": "sha256:bb",
                       "status": "rejected_gate", "detail": "x" * 500})
        by = {v["digest"]: v for v in
              build_verdicts_document(str(tmp_path))["verdicts"]}
        assert "detail" not in by["sha256:aa"]
        assert len(by["sha256:bb"]["detail"]) == 200

    def test_control_files_and_garbage_are_skipped(self, tmp_path):
        write_verdict(tmp_path, "_admitted_digests.json",
                      {"admitted": ["sha256:zz"]})
        d = verdict_dir(str(tmp_path))
        with open(os.path.join(d, "broken.json"), "w") as f:
            f.write("{not json")
        doc = build_verdicts_document(str(tmp_path))
        assert doc["total"] == 0

    def test_empty_dir_publishes_an_empty_list_not_an_error(self, tmp_path):
        doc = build_verdicts_document(str(tmp_path))
        assert doc["verdicts"] == [] and doc["total"] == 0

    def test_the_gate_numbers_travel_when_the_record_kept_them(self, tmp_path):
        gate = {"admitted": True, "model_gate_score": 0.70, "baseline_gate_score": 0.42,
                "required_gate_score": 0.441, "margin": 0.28, "coverage_ok": True,
                "by_horizon": {"7": {"model": {"gate_score": 0.71}, "baseline": {"gate_score": 0.40}, "cells": 250}},
                "model_detail": {"gate_score": 0.70}, "predictions_out": 250}
        write_verdict(tmp_path, "a.json",
                      {"hotkey": "5A", "digest": "sha256:aa", "status": "admitted",
                       "detail": "beats_baseline", "gate": gate})
        write_verdict(tmp_path, "b.json",
                      {"hotkey": "5B", "digest": "sha256:bb", "status": "admitted"})
        by = {v["digest"]: v for v in build_verdicts_document(str(tmp_path))["verdicts"]}
        assert by["sha256:aa"]["gate"] == {
            "model_gate_score": 0.70, "baseline_gate_score": 0.42, "required_gate_score": 0.441,
            "margin": 0.28, "coverage_ok": True,
            "by_horizon": {"7": {"model": {"gate_score": 0.71}, "baseline": {"gate_score": 0.40}, "cells": 250}}}
        assert "model_detail" not in by["sha256:aa"]["gate"]
        assert "gate" not in by["sha256:bb"]


class TestVerdictCorpus:
    def test_the_corpus_identity_travels_when_the_record_kept_it(self, tmp_path):
        write_verdict(tmp_path, "a.json",
                      {"hotkey": "5A", "image_digest": "sha256:aa", "status": "admitted",
                       "corpus": {"source": "held-out", "key": "HO-2026-09-20",
                                  "sha256": "ab" * 32, "cutoff": "2026-07-17",
                                  "episodes": 250, "outcome_rows": 750,
                                  "private_note": "never published"}},
                      attested=True)
        write_verdict(tmp_path, "b.json",
                      {"hotkey": "5B", "image_digest": "sha256:bb", "status": "admitted"})
        doc = build_verdicts_document(str(tmp_path))
        by = {v["digest"]: v for v in doc["verdicts"]}
        assert by["sha256:aa"]["corpus"] == {
            "source": "held-out", "key": "HO-2026-09-20", "sha256": "ab" * 32,
            "cutoff": "2026-07-17", "episodes": 250, "outcome_rows": 750}
        assert "corpus" not in by["sha256:bb"]          # predates the field
        assert "held-out" in doc["note"] and "public-bundle" in doc["note"]


class TestRealIntakeRecordShape:
    """The intake persists the WHOLE gate result under `gate` — verdict plus
    attested document — not the verdict's numbers flat. The feed must read
    that shape, or the gate and corpus blocks publish for nobody."""

    def _real_record(self):
        from hope.backtest.container_runner import RunResult
        from hope.backtest.gate import OutcomeRow
        from hope.backtest.gate_service import gate_submission
        outs = [OutcomeRow(episode_id="e0", horizon_days=7, cost_delta_pct=0.1,
                           conversions_delta_pct=0.0, efficiency_delta_pct=0.05)]
        trio = {"p10": 0.0, "p50": 0.1, "p90": 0.2}
        preds = {"e0": {"7": {"cost_delta_pct": trio, "conversions_delta_pct": trio,
                              "efficiency_delta_pct": trio}}}
        res = gate_submission(
            "repo@sha256:" + "1" * 64, [{"episode_id": "e0"}], outs,
            generated_at="2026-09-21T08:02:53+00:00",
            runner=lambda _i, eps, _t: RunResult(ok=True, predictions=preds,
                                                 episodes_in=len(eps), predictions_out=1),
            determinism_sample=0,
            corpus_info={"source": "held-out", "key": "HO-2026-09-20", "sha256": "ab" * 32,
                         "cutoff": "2026-07-17", "episodes": 250, "outcome_rows": 750})
        # exactly what intake_runner.run_intake persists per digest
        return {"hotkey": "5A", "digest": "sha256:" + "1" * 64, "status": "admitted",
                "detail": res["verdict"].get("reason"), "gate": res}

    def test_gate_and_corpus_publish_from_the_record_as_the_intake_writes_it(self, tmp_path):
        write_verdict(tmp_path, "a.json", self._real_record())
        rec = build_verdicts_document(str(tmp_path))["verdicts"][0]
        assert rec["status"] == "admitted"
        assert rec["judged_at"] == "2026-09-21T08:02:53+00:00"
        g = rec["gate"]
        assert set(g) >= {"model_gate_score", "baseline_gate_score", "required_gate_score",
                          "margin", "coverage_ok", "by_horizon"}
        assert g["model_gate_score"] > g["baseline_gate_score"]
        assert "7" in g["by_horizon"]
        assert rec["corpus"] == {"source": "held-out", "key": "HO-2026-09-20", "sha256": "ab" * 32,
                                 "cutoff": "2026-07-17", "episodes": 250, "outcome_rows": 750}
        # nothing private leaks: the attested document and signature stay on disk
        assert "document" not in rec and "sha256" not in rec
