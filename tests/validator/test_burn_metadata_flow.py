"""Burn metadata, end to end: publish -> fetch -> compose == operator commit.

The whole point of the published burn metadata is that a tracking validator
following the reference loop commits the SAME on-chain vector the operator's
committer produces. These tests prove each link, and then the equivalence
itself, branch for branch: partial burn, zero burn, full single-UID override.
The operator's committer code path is exercised unmodified (run_daily_weights_only
through compose_and_commit_weights) — nothing here stubs its burn logic.
"""

import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.run_daily_pipeline import stage_publish_weights
from scripts.run_partner_validator import compose_onchain_weights, fetch_vector
from hope.scoring.collateral_floor import resolve_burn_fraction
from hope.validator.onchain_runner import run_daily_weights_only


# ---------------------------------------------------------------- publish ----

def _intended(tmp_path, day, weights):
    (tmp_path / f"intended_weights_{day}.json").write_text(
        json.dumps({"weights": weights, "gated": False}))


def _capture_post(monkeypatch):
    import scripts.run_daily_pipeline as rdp
    captured = {}

    def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"success": True}

    monkeypatch.setattr(rdp, "_api_post", fake_post)
    return captured


def test_publish_meta_carries_burn_and_leaves_weights_bare(tmp_path, monkeypatch):
    _intended(tmp_path, "2026-09-04", {"hkA": 0.6, "hkB": 0.4})
    captured = _capture_post(monkeypatch)
    monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "135")
    monkeypatch.delenv("SN21_BURN_FRACTION", raising=False)

    res = stage_publish_weights(str(tmp_path), date(2026, 9, 4))

    assert res["published"]
    meta = captured["body"]["meta"]
    assert meta["burn_uid"] == 135
    # the published fraction is exactly what resolve_burn_fraction yields for
    # the day under the same env precedence the committer uses
    import os
    assert meta["burn_fraction"] == pytest.approx(
        resolve_burn_fraction(os.environ, date(2026, 9, 4))[0])
    # and the vector itself stays the BARE miner distribution — burn is
    # metadata for trackers, never applied at publish
    assert captured["body"]["weights"] == {"hkA": 0.6, "hkB": 0.4}


def test_publish_meta_env_override_wins_like_the_committer(tmp_path, monkeypatch):
    _intended(tmp_path, "2026-09-04", {"hkA": 1.0})
    captured = _capture_post(monkeypatch)
    monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "135")
    monkeypatch.setenv("SN21_BURN_FRACTION", "0.3")

    stage_publish_weights(str(tmp_path), date(2026, 9, 4))

    assert captured["body"]["meta"]["burn_fraction"] == pytest.approx(0.3)


def test_publish_meta_without_override_uid_publishes_no_burn(tmp_path, monkeypatch):
    _intended(tmp_path, "2026-09-04", {"hkA": 1.0})
    captured = _capture_post(monkeypatch)
    monkeypatch.delenv("SN21_OVERRIDE_WEIGHT_UID", raising=False)
    monkeypatch.setenv("SN21_BURN_FRACTION", "0.3")   # irrelevant without a UID

    stage_publish_weights(str(tmp_path), date(2026, 9, 4))

    meta = captured["body"]["meta"]
    assert meta["burn_uid"] is None
    assert meta["burn_fraction"] == 0.0


# ------------------------------------------------------------------ fetch ----

def _fake_response(payload):
    m = mock.MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    m.__exit__.return_value = False
    return m


def test_fetch_vector_extracts_burn_meta():
    payload = {"weights": {"hkA": 0.6, "hkB": 0.4},
               "meta": {"burn_uid": 135, "burn_fraction": 0.15}}
    with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        vec, uid, frac = fetch_vector("http://x", "k")
    assert vec == {"hkA": 0.6, "hkB": 0.4}
    assert uid == 135 and frac == pytest.approx(0.15)


def test_fetch_vector_without_meta_falls_back_to_no_burn():
    payload = {"weights": {"hkA": 1.0}}
    with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        vec, uid, frac = fetch_vector("http://x", "k")
    assert vec == {"hkA": 1.0} and uid is None and frac == 0.0


def test_fetch_vector_malformed_meta_is_safe():
    payload = {"weights": {"hkA": 1.0},
               "meta": {"burn_uid": "nonsense", "burn_fraction": "junk"}}
    with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        vec, uid, frac = fetch_vector("http://x", "k")
    assert vec == {"hkA": 1.0} and uid is None and frac == 0.0


# ------------------------------------------------- operator == tracker ----

class _Metagraph:
    def __init__(self, pairs):          # pairs: [(hotkey, uid), ...]
        self.hotkeys = [hk for hk, _ in pairs]
        self.uids = [u for _, u in pairs]


class _Subtensor:
    def __init__(self, pairs):
        self._mg = _Metagraph(pairs)

    def metagraph(self, netuid):
        return self._mg


def _alloc(weights):
    return SimpleNamespace(weights=weights, gated=False, standings={},
                           promotion=None, earning_set_size=len(weights),
                           collapse_audit={})


def _operator_commit(monkeypatch, *, miner_weights, mg_pairs, burn_uid, burn_env):
    """Run the operator's REAL committer path and capture its vector."""
    monkeypatch.setenv("SN21_DAILY_STREAM_WEIGHTS", "1")
    monkeypatch.setenv("SN21_DAILY_WEIGHTS_API", "1")
    monkeypatch.setenv("HOPE_API_URL", "https://fake")
    monkeypatch.setenv("HOPE_API_KEY", "k")
    monkeypatch.delenv("SN21_WEIGHT_ALLOWLIST_UIDS", raising=False)
    monkeypatch.delenv("SN21_COLLATERAL_ENFORCE", raising=False)
    monkeypatch.delenv("SN21_ALPHA_GATE_DRYRUN", raising=False)
    monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", str(burn_uid))
    if burn_env is None:
        monkeypatch.delenv("SN21_BURN_FRACTION", raising=False)
    else:
        monkeypatch.setenv("SN21_BURN_FRACTION", str(burn_env))

    captured = {}

    def fake_commit(*, subtensor, validator_wallet, netuid, uids, weights):
        captured["uids"] = uids
        captured["weights"] = weights
        return SimpleNamespace(success=True, message="ok", block_number=1,
                               block_hash="0x", extrinsic_hash="0x")

    with mock.patch(
        "hope.validator.daily_stream_weights.allocation_from_api",
        return_value=_alloc(miner_weights),
    ), mock.patch(
        "hope.validator.onchain_runner.commit_weights_layer_9c3",
        side_effect=fake_commit,
    ):
        res = run_daily_weights_only(
            subtensor=_Subtensor(mg_pairs), validator_wallet=object(), netuid=21)

    assert res.success
    return dict(zip(captured["uids"], captured["weights"]))


@pytest.mark.parametrize("burn_uid,burn_env", [
    (0, "0.3"),      # the launch configuration pinned by the existing suite
    (135, "0.15"),   # the current production shape (UID 135, scheduled 15%)
])
def test_tracker_composition_equals_operator_commit(monkeypatch, burn_uid, burn_env):
    miner_weights = {"hkA": 0.6, "hkB": 0.4}
    mg_pairs = [("hkA", 7), ("hkB", 9), ("hkburn", burn_uid)]
    operator = _operator_commit(
        monkeypatch, miner_weights=miner_weights, mg_pairs=mg_pairs,
        burn_uid=burn_uid, burn_env=burn_env)

    # the tracker sees the SAME miner vector plus the published meta
    uid_of = dict((hk, u) for hk, u in mg_pairs)
    pairs = [(uid_of[hk], w) for hk, w in miner_weights.items()]
    uids, weights = compose_onchain_weights(pairs, burn_uid, float(burn_env))
    tracker = dict(zip(uids, weights))

    assert set(tracker) == set(operator)
    for u in operator:
        assert tracker[u] == pytest.approx(operator[u]), f"uid {u} diverges"
    assert sum(tracker.values()) == pytest.approx(1.0)


def test_tracker_matches_operator_zero_burn(monkeypatch):
    miner_weights = {"hkA": 0.6, "hkB": 0.4}
    mg_pairs = [("hkA", 7), ("hkB", 9), ("hkburn", 135)]
    operator = _operator_commit(
        monkeypatch, miner_weights=miner_weights, mg_pairs=mg_pairs,
        burn_uid=135, burn_env="0")

    uids, weights = compose_onchain_weights([(7, 0.6), (9, 0.4)], 135, 0.0)
    tracker = dict(zip(uids, weights))
    assert tracker == {u: pytest.approx(w) for u, w in operator.items()}
    assert 135 not in tracker


def test_tracker_matches_operator_full_override(monkeypatch):
    miner_weights = {"hkA": 0.6, "hkB": 0.4}
    mg_pairs = [("hkA", 7), ("hkB", 9), ("hkburn", 135)]
    operator = _operator_commit(
        monkeypatch, miner_weights=miner_weights, mg_pairs=mg_pairs,
        burn_uid=135, burn_env="1.0")

    uids, weights = compose_onchain_weights([(7, 0.6), (9, 0.4)], 135, 1.0)
    assert dict(zip(uids, weights)) == {135: pytest.approx(1.0)}
    assert operator == {135: pytest.approx(1.0)}
