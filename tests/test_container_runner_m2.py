"""M2 runner — contract flags, output parsing, callable mode, failure paths."""
import json

from hope.backtest.container_runner import (
    RunResult, _parse_output, docker_command, run_basket_callable,
)


class TestIsolationContract:
    def test_flags_are_the_published_contract(self):
        cmd = docker_command("sha256:abc")
        assert not any(f.startswith("--name=") for f in cmd)  # unnamed by default
        named = docker_command("sha256:abc", name="sn21-run-x")
        assert "--name=sn21-run-x" in named
        joined = " ".join(cmd)
        for flag in ("--network=none", "--memory=1g", "--memory-swap=1g",
                     "--cpus=1", "--pids-limit=256", "--read-only",
                     "--security-opt=no-new-privileges", "--rm", "-i"):
            assert flag in joined, flag
        assert cmd[-1] == "sha256:abc"


class TestOutputParsing:
    def test_valid_lines_parsed_chatter_ignored(self):
        out = "\n".join([
            "starting model v3...",
            json.dumps({"episode_id": "e1", "horizons": {"7": {}}}),
            "{broken json",
            json.dumps({"episode_id": "unknown", "horizons": {"7": {}}}),
            json.dumps({"episode_id": "e2", "horizons": {"14": {}}}),
        ])
        preds = _parse_output(out, {"e1", "e2"})
        assert set(preds) == {"e1", "e2"}

    def test_missing_horizons_rejected(self):
        out = json.dumps({"episode_id": "e1", "answer": 42})
        assert _parse_output(out, {"e1"}) == {}


class TestCallableMode:
    EPS = [{"episode_id": "e1"}, {"episode_id": "e2"}, {"episode_id": "e3"}]

    def test_predictions_collected(self):
        r = run_basket_callable(lambda e: {"7": {"x": 1}}, self.EPS)
        assert r.ok and r.predictions_out == 3

    def test_crashing_model_abstains_not_fatal(self):
        def flaky(e):
            if e["episode_id"] == "e2":
                raise RuntimeError("boom")
            return {"7": {}}
        r = run_basket_callable(flaky, self.EPS)
        assert r.ok and r.predictions_out == 2
        assert "e2" not in r.predictions

    def test_abstention_via_none(self):
        r = run_basket_callable(lambda e: None, self.EPS)
        assert r.ok and r.predictions_out == 0
