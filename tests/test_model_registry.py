"""Model registry — commitment parsing strictness + latest-wins + admission set."""
from hope.backtest.model_registry import (
    MODEL_COMMIT_PREFIX, build_registry, parse_model_commitment,
)

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


class TestParsing:
    def test_valid_digest_only_legacy(self):
        assert parse_model_commitment(MODEL_COMMIT_PREFIX + D1) == {
            "image_ref": None, "digest": D1}

    def test_valid_bundle_repo_at_digest(self):
        raw = MODEL_COMMIT_PREFIX + "ghcr.io/acme/model@" + D1
        assert parse_model_commitment(raw) == {
            "image_ref": "ghcr.io/acme/model", "digest": D1}

    def test_bundle_rejects_bad_repo(self):
        for repo in ("GHCR.io/x", "-lead/x", "a b/x", "repo:tag", "x/" + "y" * 120):
            assert parse_model_commitment(
                MODEL_COMMIT_PREFIX + repo + "@" + D1) is None

    def test_garbage_never_crashes(self):
        for raw in (None, 42, "", "hello", MODEL_COMMIT_PREFIX,
                    MODEL_COMMIT_PREFIX + "sha256:short",
                    MODEL_COMMIT_PREFIX + "sha256:" + "z" * 64,
                    "sn21-prediction:v1:" + D1):
            assert parse_model_commitment(raw) is None


class TestRegistry:
    def test_latest_valid_wins_and_admission_split(self):
        chain = {
            "hk1": [(100, MODEL_COMMIT_PREFIX + D1), (200, MODEL_COMMIT_PREFIX + D2)],
            "hk2": [(50, MODEL_COMMIT_PREFIX + D1)],
            "hk3": [(70, "garbage")],
        }
        r = build_registry(["hk1", "hk2", "hk3"], lambda hk: chain.get(hk),
                           admitted_digests={D2}, as_of_iso="2026-07-27")
        assert r["active_count"] == 1 and r["active"]["hk1"].image_digest == D2
        assert r["pending_admission"] == {"hk2": D1}
        assert "hk3" not in r["active"] and "hk3" not in r["pending_admission"]

    def test_garbage_between_valid_commits_ignored(self):
        chain = {"hk1": [(100, MODEL_COMMIT_PREFIX + D1), (300, "junk"), (200, MODEL_COMMIT_PREFIX + D2)]}
        r = build_registry(["hk1"], lambda hk: chain.get(hk), {D1, D2}, "2026-07-27")
        assert r["active"]["hk1"].image_digest == D2  # block 200 > 100; junk at 300 ignored
