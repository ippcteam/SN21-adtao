"""M3 shadow mode — ledger, finalize+attest, cutover gate."""
import json
from datetime import date, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.backtest.container_runner import run_basket_callable
from hope.backtest.shadow import (
    ShadowModel,
    cutover_ready,
    finalize_day,
    run_shadow_day,
)
from hope.publication.rail import AttestedDocument, verify
from hope.scoring.episode_average import ScoredEpisode

M = ShadowModel(hotkey="hk1", image_digest="sha256:abc", admitted_at="2026-08-01")
D = date(2026, 8, 10)


def _runner(model, episodes):
    return run_basket_callable(lambda e: {"7": {"x": 1}}, episodes)


class TestLedger:
    def test_run_and_record(self, tmp_path):
        eps = [{"episode_id": "e1"}, {"episode_id": "e2"}]
        s = run_shadow_day("2026-08-10", eps, [M], _runner, str(tmp_path))
        assert s["models_run"] == 1 and s["results"]["hk1"]["predictions"] == 2
        lines = (tmp_path / "shadow" / "2026-08-10" / "hk1.jsonl").read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["image_digest"] == "sha256:abc" and rec["predictions_out"] == 2

    def test_finalize_attested_and_verifiable(self, tmp_path):
        run_shadow_day("2026-08-10", [{"episode_id": "e1"}], [M], _runner, str(tmp_path))
        key = Ed25519PrivateKey.generate()
        out = finalize_day(str(tmp_path), "2026-08-10", "2026-08-10T23:59:00+00:00",
                           private_key=key)
        assert out["document"]["metrics"]["model_count"] == 1
        att = AttestedDocument(document=out["document"], sha256=out["sha256"],
                               signature_hex=out["signature_hex"],
                               public_key_hex=out["public_key_hex"])
        assert verify(att) is True


class TestCutoverGate:
    def _eps(self, n_days, per_day=5):
        return [ScoredEpisode(0.6, D - timedelta(days=i), 1.0)
                for i in range(n_days) for _ in range(per_day)]

    def test_seven_scored_days_ready(self):
        v = cutover_ready({"hk1": self._eps(7)}, D)
        assert v["models"]["hk1"]["ready"] is True and v["cutover_possible"]

    def test_six_days_not_ready(self):
        v = cutover_ready({"hk1": self._eps(6)}, D)
        assert v["models"]["hk1"]["ready"] is False and not v["cutover_possible"]

    def test_days_counted_distinct_not_volume(self):
        # 1000 episodes all on ONE day != 7 days of evidence
        eps = [ScoredEpisode(0.9, D, 1.0)] * 1000
        v = cutover_ready({"hk1": eps}, D)
        assert v["models"]["hk1"]["ready"] is False


# ---- models run a few at a time; the ledger does not depend on it -----------

def test_shadow_workers_env():
    from hope.backtest.shadow import shadow_workers
    assert shadow_workers({}) == 2
    assert shadow_workers({"SN21_SHADOW_WORKERS": "1"}) == 1
    assert shadow_workers({"SN21_SHADOW_WORKERS": "9"}) == 4
    assert shadow_workers({"SN21_SHADOW_WORKERS": "x"}) == 2


def test_two_models_run_concurrently_and_both_are_ledgered(tmp_path):
    import time
    from hope.backtest.container_runner import RunResult
    from hope.backtest.shadow import ShadowModel, run_shadow_day

    def slow_runner(model, eps):
        time.sleep(0.4)
        return RunResult(ok=True, predictions={"e1": {"7": {}}}, episodes_in=len(eps), predictions_out=1)

    models = [ShadowModel(hotkey=f"hk{i}", image_digest=f"sha256:{'0' * 63}{i}", admitted_at="2026-09-01") for i in range(2)]
    t0 = time.monotonic()
    s = run_shadow_day("2026-09-13", [{"episode_id": "e1"}], models, slow_runner, str(tmp_path), workers=2)
    took = time.monotonic() - t0
    assert set(s["results"]) == {"hk0", "hk1"} and s["models_run"] == 2
    assert took < 0.7, f"two 0.4 s models took {took:.2f} s — they did not overlap"
    day_dir = tmp_path / "shadow" / "2026-09-13"
    assert sorted(p.name for p in day_dir.iterdir() if p.suffix == ".jsonl") == ["hk0.jsonl", "hk1.jsonl"]


def test_single_worker_is_the_old_sequential_run(tmp_path):
    from hope.backtest.container_runner import RunResult
    from hope.backtest.shadow import ShadowModel, run_shadow_day
    order = []

    def runner(model, eps):
        order.append(model.hotkey)
        return RunResult(ok=True, predictions={}, episodes_in=1, predictions_out=0)

    models = [ShadowModel(hotkey=f"hk{i}", image_digest=f"sha256:{'0' * 63}{i}", admitted_at="2026-09-01") for i in range(3)]
    run_shadow_day("2026-09-13", [{"episode_id": "e1"}], models, runner, str(tmp_path), workers=1)
    assert order == ["hk0", "hk1", "hk2"]
