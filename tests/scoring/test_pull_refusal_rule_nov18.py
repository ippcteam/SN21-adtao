"""A registry that refuses an anonymous pull is the miner's fault, from a start date (Sir Rob, 17 Nov 2026, mlrJIz2A).

1. The puller marks a refusal (401, 403, 404 on the token or the manifest, asked without our login) and nothing else.
2. The day run records it as `pull_refused: <code>:`, apart from `pull_failed:`.
3. Before SN21_PULL_REFUSAL_FROM, and with it unset, a refused pull stays ours: no charge, no strike.
4. On and after it: charged, a strike with reason image_refused, and the morning alert no longer counts it as ours.
5. The admission gate passes no day, so a refused new model stays pending as before.
"""
import io
import json
import urllib.error
from datetime import date

import pytest

from hope.backtest import local_executor as LE
from hope.backtest import oci_pull as O
from hope.scoring import chronic_failure as cf
from hope.scoring.absence_penalty import PUBLISHED_RUN_BUDGET_S, operator_fault, pull_refusal_from

START = date(2026, 12, 1)
BEFORE, ON_DAY = date(2026, 11, 30), date(2026, 12, 1)
DIGEST = "sha256:" + "a" * 64
BEARER = {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'}
REFUSED_TOKEN = "pull_refused: 401: token request failed: 401"   # UID 202's model, 15 Nov, as it would now read


def _http(monkeypatch, manifest_code=None, token_code=None, blob_code=None):
    """A registry that answers the manifest, the token or a blob with the given status."""
    def fake(url, headers, timeout=120):
        if "/token" in url:
            if token_code:
                raise urllib.error.HTTPError(url, token_code, "x", {}, io.BytesIO())
            return io.BytesIO(json.dumps({"token": "t"}).encode())
        if "Authorization" not in headers:
            raise urllib.error.HTTPError(url, 401, "auth", BEARER, io.BytesIO())
        code = manifest_code if "/manifests/" in url else blob_code
        raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO())
    monkeypatch.setattr(O, "_http_get", fake)
    monkeypatch.delenv(O.PULL_AUTH_ENV, raising=False)
    monkeypatch.delenv("SN21_MODEL_KEEP", raising=False)


# ---- 1. what the puller marks ---------------------------------------------------------------

@pytest.mark.parametrize("code", [401, 403, 404])
def test_an_anonymous_manifest_refusal_is_marked_with_its_status(monkeypatch, code):
    _http(monkeypatch, manifest_code=code)
    with pytest.raises(O.PullError) as e:
        O._Registry("ghcr.io", "miner/m").manifest_bytes(DIGEST)
    assert e.value.refused == code


def test_a_token_service_refusal_is_marked(monkeypatch):
    _http(monkeypatch, token_code=401)
    with pytest.raises(O.PullError) as e:
        O._Registry("ghcr.io", "miner/m").manifest_bytes(DIGEST)
    assert e.value.refused == 401 and str(e.value) == "token request failed: 401"


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_rate_limits_and_server_errors_are_not_refusals(monkeypatch, code):
    _http(monkeypatch, manifest_code=code)
    with pytest.raises(O.PullError) as e:
        O._Registry("ghcr.io", "miner/m").manifest_bytes(DIGEST)
    assert e.value.refused is None


def test_a_blob_the_registry_will_not_serve_is_not_a_refusal(monkeypatch, tmp_path):
    _http(monkeypatch, blob_code=404)
    with pytest.raises(O.PullError) as e:
        O._Registry("ghcr.io", "miner/m").blob(DIGEST, str(tmp_path / "b"))
    assert e.value.refused is None


def test_a_refusal_to_our_own_login_is_never_the_miners(monkeypatch):
    _http(monkeypatch, manifest_code=404)
    monkeypatch.setenv(O.PULL_AUTH_ENV, json.dumps({"ghcr.io": {"username": "u", "password": "p"}}))
    with pytest.raises(O.PullError) as e:
        O._Registry("ghcr.io", "miner/m").manifest_bytes(DIGEST)
    assert e.value.refused is None


# ---- 2. what the day run records -------------------------------------------------------------

def _run_with(monkeypatch, tmp_path, exc):
    monkeypatch.setattr(LE, "_wait_for_disk", lambda dest: 1 << 40)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(LE, "pull_and_unpack", boom)
    return LE.run_basket_local("ghcr.io/miner/m", DIGEST, [{"episode_id": "e1"}], workdir_root=str(tmp_path))


def test_a_refused_pull_is_recorded_apart_from_other_pull_failures(monkeypatch, tmp_path):
    r = _run_with(monkeypatch, tmp_path, O.PullError("token request failed: 401", refused=401))
    assert r.ok is False and r.error == REFUSED_TOKEN
    r = _run_with(monkeypatch, tmp_path, O.PullError("GET u -> 503"))
    assert r.error == "pull_failed: GET u -> 503"


# ---- 3 + 4. whose fault, by basket day ---------------------------------------------------------

def test_before_the_start_date_or_unset_a_refusal_stays_ours():
    assert operator_fault(REFUSED_TOKEN, PUBLISHED_RUN_BUDGET_S, BEFORE, START)
    assert operator_fault(REFUSED_TOKEN, PUBLISHED_RUN_BUDGET_S, ON_DAY, None)
    assert cf.classify_failure(False, REFUSED_TOKEN, BEFORE, START) == cf.FAULT_SUBNET
    assert cf.classify_failure(False, REFUSED_TOKEN, ON_DAY, None) == cf.FAULT_SUBNET


def test_from_the_start_date_a_refusal_is_the_miners():
    assert not operator_fault(REFUSED_TOKEN, PUBLISHED_RUN_BUDGET_S, ON_DAY, START)
    assert cf.classify_failure(False, REFUSED_TOKEN, ON_DAY, START) == cf.FAULT_MINER
    assert cf.failure_reason(False, REFUSED_TOKEN, ON_DAY, START) == cf.REASON_IMAGE_REFUSED


def test_other_pull_failures_stay_ours_after_the_start_date():
    for err in ("pull_failed: GET u -> 503", "pull_failed: token request failed: 429",
                "pull_failed: blob digest mismatch: asked a, got b", "pull timeout>600s", "disk_low: 1 MB free"):
        assert operator_fault(err, PUBLISHED_RUN_BUDGET_S, ON_DAY, START), err


def test_the_strike_fold_strikes_a_refusal_only_from_the_start_date():
    params = cf.ChronicFailureParams(pull_refusal_from=START)
    before = cf.observe_day({}, [], BEFORE, {"hk": (False, REFUSED_TOKEN)}, params)
    after = cf.observe_day({}, [], ON_DAY, {"hk": (False, REFUSED_TOKEN)}, params)
    assert before.struck == () and before.excluded == ("hk",)
    assert after.struck == ("hk",)
    assert [e.reason for e in after.events if e.kind == cf.KIND_STRIKE] == [cf.REASON_IMAGE_REFUSED]


def test_the_start_date_comes_from_one_setting_for_both_rules():
    env = {"SN21_PULL_REFUSAL_FROM": "2026-12-01"}
    assert pull_refusal_from(env) == START
    assert cf.params_from_env(env).pull_refusal_from == START
    assert pull_refusal_from({}) is None and cf.params_from_env({}).pull_refusal_from is None
    assert pull_refusal_from({"SN21_PULL_REFUSAL_FROM": "soon"}) is None


def test_the_morning_alert_stops_counting_a_refusal_as_ours_from_the_start_date(monkeypatch):
    from scripts import run_daily_pipeline as P
    results = {"hk": {"ok": False, "error": REFUSED_TOKEN}}
    monkeypatch.setenv("SN21_PULL_REFUSAL_FROM", "2026-12-01")
    assert P.shadow_run_health(results, {}, "2026-11-30")["operator_faults"] == 1
    assert P.shadow_run_health(results, {}, "2026-12-01")["operator_faults"] == 0


# ---- 5. the admission gate ---------------------------------------------------------------------

def test_the_admission_gate_keeps_a_refused_new_model_pending():
    # intake_runner asks with no day: a refused image is retried and admitted once it is public.
    assert operator_fault(REFUSED_TOKEN)
