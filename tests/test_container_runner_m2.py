"""M2 runner — contract flags, output parsing, callable mode, failure paths."""
import json
from typing import ClassVar

from hope.backtest.container_runner import (
    _parse_output,
    delivered,
    docker_command,
    required_horizons,
    run_basket_callable,
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
            json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}}}),
            "{broken json",
            json.dumps({"episode_id": "unknown", "horizons": {"7": {"p50": 0.1}}}),
            json.dumps({"episode_id": "e2", "horizons": {"14": {"p50": 0.2}}}),
        ])
        preds = _parse_output(out, {"e1", "e2"})
        assert set(preds) == {"e1", "e2"}

    def test_missing_horizons_rejected(self):
        out = json.dumps({"episode_id": "e1", "answer": 42})
        assert _parse_output(out, {"e1"}) == {}


class TestDeliveredPrediction:
    """A prediction is what settle can score: one non-empty block per horizon
    the episode asks for. An empty or partial `horizons` is an abstention."""

    EPS: ClassVar = [
        {"episode_id": "e1", "episode_metadata": {"outcome_horizons_days": [7, 14]}},
        {"episode_id": "e2", "episode_metadata": {"outcome_horizons_days": [7, 14, 28]}},
        {"episode_id": "e3"},   # no metadata -> schema default (7, 14)
    ]

    def test_required_horizons_from_episode_metadata(self):
        assert required_horizons(self.EPS) == {"e1": ["7", "14"], "e2": ["7", "14", "28"], "e3": ["7", "14"]}

    def test_empty_horizons_is_an_abstention(self):
        out = json.dumps({"episode_id": "e1", "horizons": {}})
        assert _parse_output(out, {"e1"}, required_horizons(self.EPS)) == {}
        assert _parse_output(out, {"e1"}) == {}          # even without a map

    def test_partial_horizons_is_an_abstention(self):
        out = json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}}})
        assert _parse_output(out, {"e1"}, required_horizons(self.EPS)) == {}

    def test_null_block_is_an_abstention(self):
        out = json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}, "14": None}})
        assert _parse_output(out, {"e1"}, required_horizons(self.EPS)) == {}
        out = json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}, "14": {}}})
        assert _parse_output(out, {"e1"}, required_horizons(self.EPS)) == {}

    def test_full_prediction_counts(self):
        out = json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}, "14": {"p50": 0.2}}})
        preds = _parse_output(out, {"e1"}, required_horizons(self.EPS))
        assert set(preds) == {"e1"}

    def test_extra_horizons_do_not_hurt(self):
        out = json.dumps({"episode_id": "e1", "horizons": {"7": {"p50": 0.1}, "14": {"p50": 0.2}, "28": {"p50": 0.3}}})
        assert set(_parse_output(out, {"e1"}, required_horizons(self.EPS))) == {"e1"}

    def test_delivered_helper(self):
        assert delivered({"7": {"a": 1}, "14": {"a": 1}}, ["7", "14"])
        assert not delivered({"7": {"a": 1}}, ["7", "14"])
        assert not delivered({}, ["7"])
        assert not delivered(None, ["7"])
        assert delivered({"7": {"a": 1}}, None) and not delivered({"7": {}}, None)

    def test_callable_mode_counts_only_full_predictions(self):
        eps = [{"episode_id": "e1", "episode_metadata": {"outcome_horizons_days": [7, 14]}},
               {"episode_id": "e2", "episode_metadata": {"outcome_horizons_days": [7, 14]}}]
        r = run_basket_callable(lambda e: {"7": {"x": 1}, "14": {"x": 1}} if e["episode_id"] == "e1" else {"7": {"x": 1}}, eps)
        assert r.predictions_out == 1 and set(r.predictions) == {"e1"}


class TestCallableMode:
    EPS: ClassVar = [{"episode_id": "e1"}, {"episode_id": "e2"}, {"episode_id": "e3"}]

    def test_predictions_collected(self):
        r = run_basket_callable(lambda e: {"7": {"x": 1}, "14": {"x": 1}}, self.EPS)
        assert r.ok and r.predictions_out == 3

    def test_crashing_model_abstains_not_fatal(self):
        def flaky(e):
            if e["episode_id"] == "e2":
                raise RuntimeError("boom")
            return {"7": {"x": 1}, "14": {"x": 1}}
        r = run_basket_callable(flaky, self.EPS)
        assert r.ok and r.predictions_out == 2
        assert "e2" not in r.predictions

    def test_abstention_via_none(self):
        r = run_basket_callable(lambda e: None, self.EPS)
        assert r.ok and r.predictions_out == 0
