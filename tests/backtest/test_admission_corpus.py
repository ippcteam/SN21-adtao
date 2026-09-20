"""The pipeline gates on the served held-out corpus, cached by sha.

Fetch once, verify the sha, read from disk after that; refuse a document
whose bytes do not match what was announced; fall back to the public
bundle only when nothing else is available — and always say which.
"""

import gzip
import hashlib
import json


from hope.backtest import admission_corpus as ac


def _record(eid, day="2026-08-03"):
    return {"input": {"episode_id": eid,
                      "episode_metadata": {"episode_id": eid, "action_window_start": day}},
            "settled_outcomes": {"7": {"cost_delta_pct": 0.1, "conversions_delta_pct": 0.0,
                                       "cpa_delta_pct": 0.05}}}


def _document(n=4):
    lines = [json.dumps({"_manifest": {"corpus_key": "HO-1"}})]
    lines += [json.dumps(_record(f"e{i}")) for i in range(n)]
    return ("\n".join(lines) + "\n").encode()


class Api:
    def __init__(self, doc: bytes | None, key="HO-1", sha=None, meta_fails=False, doc_fails=False):
        self.doc, self.key, self.meta_fails, self.doc_fails = doc, key, meta_fails, doc_fails
        self.sha = sha or (hashlib.sha256(doc).hexdigest() if doc else None)
        self.json_calls, self.bytes_calls = 0, 0

    def get_json(self, path):
        self.json_calls += 1
        if self.meta_fails:
            raise OSError("api down")
        if not self.sha:
            return {"success": False}
        return {"success": True, "corpus": {"corpus_key": self.key, "document_sha256": self.sha,
                                            "cutoff_date": "2026-07-17", "episode_count": 4}}

    def get_bytes(self, path):
        self.bytes_calls += 1
        if self.doc_fails:
            raise OSError("api down")
        assert path == f"admission/corpus/{self.key}/document"
        return gzip.compress(self.doc)


def test_first_run_fetches_verifies_and_caches(tmp_path):
    api = Api(_document())
    eps, outs, info = ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes,
                                     str(tmp_path / "w"), log=lambda *_: None)
    assert info["source"] == "held-out" and info["key"] == "HO-1"
    assert info["sha256"] == api.sha and info["episodes"] == 4 and info["outcome_rows"] == 4
    assert info["cutoff"] == "2026-07-17"
    assert [e["episode_id"] for e in eps] == ["e0", "e1", "e2", "e3"]
    cached = tmp_path / "admission_corpus" / "HO-1.jsonl"
    assert cached.read_bytes() == api.doc
    assert (tmp_path / "admission_corpus" / "HO-1.meta.json").exists()


def test_second_run_reads_from_disk_without_refetching(tmp_path):
    api = Api(_document())
    ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w", log=lambda *_: None)
    assert api.bytes_calls == 1
    ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w", log=lambda *_: None)
    assert api.bytes_calls == 1          # sha matched — no second download


def test_a_new_corpus_key_is_fetched_beside_the_old_one(tmp_path):
    api = Api(_document(4))
    ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w", log=lambda *_: None)
    api2 = Api(_document(6), key="HO-2")
    _e, _o, info = ac.load_corpus(str(tmp_path), 250, api2.get_json, api2.get_bytes, "w",
                                  log=lambda *_: None)
    assert info["key"] == "HO-2" and info["episodes"] == 6
    assert (tmp_path / "admission_corpus" / "HO-1.jsonl").exists()
    assert (tmp_path / "admission_corpus" / "HO-2.jsonl").exists()


def test_a_document_that_does_not_match_its_sha_is_refused(tmp_path, monkeypatch):
    api = Api(_document(), sha="0" * 64)
    logs = []
    monkeypatch.setattr(ac.bundle_corpus, "fetch_bundle", lambda wd: str(_bundle(tmp_path)))
    _e, _o, info = ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w",
                                  log=logs.append)
    assert not (tmp_path / "admission_corpus" / "HO-1.jsonl").exists()
    assert info["source"] == "public-bundle"
    assert any("refusing" in line for line in logs)


def test_cached_corpus_survives_an_api_outage(tmp_path):
    api = Api(_document())
    ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w", log=lambda *_: None)
    down = Api(_document(), meta_fails=True)
    _e, _o, info = ac.load_corpus(str(tmp_path), 250, down.get_json, down.get_bytes, "w",
                                  log=lambda *_: None)
    assert info["source"] == "held-out" and info["key"] == "HO-1"


def _bundle(tmp_path):
    p = tmp_path / "bundle.jsonl"
    rec = {"episode_id": "b1", "input": {"episode_metadata": {"action_window_start": "2026-05-01"}},
           "labels": {"7": {"cost_delta_pct": 0.2, "conversions_delta_pct": 0.1, "cpa_delta_pct": 0.1}}}
    p.write_text(json.dumps(rec) + "\n")
    return p


def test_nothing_served_and_nothing_cached_falls_back_to_the_bundle_and_says_so(tmp_path, monkeypatch):
    api = Api(None)
    logs = []
    monkeypatch.setattr(ac.bundle_corpus, "fetch_bundle", lambda wd: str(_bundle(tmp_path)))
    eps, outs, info = ac.load_corpus(str(tmp_path), 250, api.get_json, api.get_bytes, "w",
                                     log=logs.append)
    assert info["source"] == "public-bundle" and info["key"] is None
    assert info["episodes"] == 1 and len(outs) == 1
    assert any("WARNING" in line and "bundle" in line for line in logs)


def test_corpus_size_caps_the_held_out_selection(tmp_path):
    api = Api(_document(10))
    eps, _o, info = ac.load_corpus(str(tmp_path), 4, api.get_json, api.get_bytes, "w",
                                   log=lambda *_: None)
    assert len(eps) == 4 and info["episodes"] == 4


def test_gate_submission_stamps_the_corpus_into_the_attested_verdict():
    from hope.backtest.container_runner import RunResult
    from hope.backtest.gate import OutcomeRow
    from hope.backtest.gate_service import gate_submission

    outs = [OutcomeRow(episode_id="e0", horizon_days=7, cost_delta_pct=0.1,
                       conversions_delta_pct=0.0, efficiency_delta_pct=0.05)]
    trio = {"p10": 0.0, "p50": 0.1, "p90": 0.2}
    preds = {"e0": {"7": {"cost_delta_pct": trio, "conversions_delta_pct": trio,
                          "efficiency_delta_pct": trio}}}

    def runner(_image, episodes, _t):
        return RunResult(ok=True, predictions=preds, episodes_in=len(episodes),
                         predictions_out=1)

    info = {"source": "held-out", "key": "HO-1", "sha256": "ab" * 32,
            "cutoff": "2026-07-17", "episodes": 1, "outcome_rows": 1}
    res = gate_submission("repo@sha256:" + "0" * 64, [{"episode_id": "e0"}], outs,
                          generated_at="2026-09-21T00:00:00Z", runner=runner,
                          determinism_sample=0, corpus_info=info)
    assert res["verdict"]["corpus"] == info
    assert res["document"]["metrics"]["verdict"]["corpus"] == info
    # and without it, nothing is invented
    res2 = gate_submission("repo@sha256:" + "0" * 64, [{"episode_id": "e0"}], outs,
                           generated_at="2026-09-21T00:00:00Z", runner=runner,
                           determinism_sample=0)
    assert "corpus" not in res2["verdict"]
