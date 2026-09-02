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
