"""run_daily_weights_only — the post-weekly commit path (2026-08-24).

The weekly stream wound down, --release auto stopped resolving, and the
daily on-chain weight commit that lived inside the epoch run silently
stopped: the chain showed heartbeat re-assertions of a stale vector while
the dashboard moved (miner-reported, ~2 days). These tests pin the two
behaviours that make the standalone path safe to run every tick:

  1. a healthy daily allocation commits through the SAME composition the
     epoch path used — allowlist, alpha gate, burn/override — so the burn
     split on chain is identical to what the epoch run would have produced;
  2. any failure to obtain a usable allocation leaves the vector empty and
     SKIPS the commit, so the prior on-chain weights stand (never a wipe).
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from hope.validator.onchain_runner import run_daily_weights_only


class _FakeMetagraph:
    def __init__(self):
        self.hotkeys = ["hkA", "hkB", "hkburn"]
        self.uids = [7, 9, 0]


class _FakeSubtensor:
    def metagraph(self, netuid):
        return _FakeMetagraph()


def _alloc(weights):
    return SimpleNamespace(weights=weights, gated=False, standings={},
                           promotion=None, earning_set_size=len(weights),
                           collapse_audit={})


@pytest.fixture
def daily_env(monkeypatch):
    monkeypatch.setenv("SN21_DAILY_STREAM_WEIGHTS", "1")
    monkeypatch.setenv("SN21_DAILY_WEIGHTS_API", "1")
    monkeypatch.setenv("HOPE_API_URL", "https://fake")
    monkeypatch.setenv("HOPE_API_KEY", "k")
    monkeypatch.delenv("SN21_WEIGHT_ALLOWLIST_UIDS", raising=False)
    monkeypatch.delenv("SN21_COLLATERAL_ENFORCE", raising=False)
    monkeypatch.delenv("SN21_ALPHA_GATE_DRYRUN", raising=False)


def test_healthy_allocation_commits_with_burn_composition(daily_env, monkeypatch):
    # Burn 30% to uid 0, exactly the launch configuration: the standalone
    # path must produce the same split the epoch path did.
    monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "0")
    monkeypatch.setenv("SN21_BURN_FRACTION", "0.3")
    captured = {}

    def fake_commit(*, subtensor, validator_wallet, netuid, uids, weights):
        captured["uids"] = uids
        captured["weights"] = weights
        return SimpleNamespace(success=True, message="ok", block_number=123,
                               block_hash="0xabc", extrinsic_hash="0xdef")

    with mock.patch(
        "hope.validator.daily_stream_weights.allocation_from_api",
        return_value=_alloc({"hkA": 0.6, "hkB": 0.4}),
    ), mock.patch(
        "hope.validator.onchain_runner.commit_weights_layer_9c3",
        side_effect=fake_commit,
    ):
        res = run_daily_weights_only(
            subtensor=_FakeSubtensor(), validator_wallet=object(), netuid=21)

    assert res.success
    got = dict(zip(captured["uids"], captured["weights"]))
    assert got[0] == pytest.approx(0.3)          # burn share to uid 0
    assert got[7] == pytest.approx(0.42)         # 0.6 * (1 - 0.3)
    assert got[9] == pytest.approx(0.28)         # 0.4 * (1 - 0.3)
    assert sum(captured["weights"]) == pytest.approx(1.0)


def test_failed_allocation_skips_commit_and_keeps_prior_vector(daily_env, monkeypatch):
    # API down (or any error): the vector stays empty and NOTHING is
    # committed — the prior on-chain weights remain standing. This is the
    # fail-safe that makes it safe to run this path on every daemon tick.
    monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "0")
    monkeypatch.setenv("SN21_BURN_FRACTION", "0.3")
    commit = mock.Mock()
    with mock.patch(
        "hope.validator.daily_stream_weights.allocation_from_api",
        side_effect=RuntimeError("api down"),
    ), mock.patch(
        "hope.validator.onchain_runner.commit_weights_layer_9c3", commit,
    ):
        res = run_daily_weights_only(
            subtensor=_FakeSubtensor(), validator_wallet=object(), netuid=21)

    assert not res.success
    assert "skipping weights commit" in res.message
    commit.assert_not_called()


def test_none_subtensor_skips_commit_without_crashing(daily_env):
    # A brief RPC outage leaves the runner's subtensor None (init_bittensor
    # swallows the connection error and sets no_chain). The daily commit path
    # must skip cleanly — prior on-chain vector stands — rather than raise
    # AttributeError on subtensor.metagraph.
    commit = mock.Mock()
    with mock.patch(
        "hope.validator.onchain_runner.commit_weights_layer_9c3", commit,
    ):
        res = run_daily_weights_only(
            subtensor=None, validator_wallet=object(), netuid=21)

    assert not res.success
    assert "chain unreachable" in res.message
    commit.assert_not_called()


def test_no_placement_eligible_standings_skips_commit(daily_env, monkeypatch):
    # An empty (but successful) allocation is the "nobody placement-eligible"
    # day — also a skip, never an empty-vector wipe.
    monkeypatch.delenv("SN21_OVERRIDE_WEIGHT_UID", raising=False)
    commit = mock.Mock()
    with mock.patch(
        "hope.validator.daily_stream_weights.allocation_from_api",
        return_value=_alloc({}),
    ), mock.patch(
        "hope.validator.onchain_runner.commit_weights_layer_9c3", commit,
    ):
        res = run_daily_weights_only(
            subtensor=_FakeSubtensor(), validator_wallet=object(), netuid=21)

    assert not res.success
    commit.assert_not_called()


# ---------------------------------------------- one-payer subprocess ----

def test_one_payer_subprocess_returns_reader_result(tmp_path):
    """The child re-runs the real reader over the same ledger; an empty
    receipts dir means an empty suppression set, delivered via the boundary."""
    from datetime import date
    from hope.validator.daily_stream_weights import (
        one_payer_suppression_subprocess,
    )
    (tmp_path / "receipts").mkdir()
    out = one_payer_suppression_subprocess(
        str(tmp_path), date(2026, 8, 26), {"SN21_ONE_PAYER_PER_MODEL": "1"})
    assert out == frozenset()


def test_one_payer_subprocess_child_death_suppresses_nobody(tmp_path):
    """A dead child (here: killed by timeout) must fail EMPTY, not loud."""
    from datetime import date
    from hope.validator.daily_stream_weights import (
        one_payer_suppression_subprocess,
    )
    out = one_payer_suppression_subprocess(
        str(tmp_path / "missing"), date(2026, 8, 26), {}, timeout_s=1)
    assert out == frozenset()


# ------------------------------------------- fingerprint index ----

def _mini_receipt(tmp_path, day, entries):
    import json as _json
    rdir = tmp_path / "receipts"
    rdir.mkdir(exist_ok=True)
    (rdir / f"{day}.json").write_text(_json.dumps(
        {"document": {"metrics": {"entries": entries}}}))


def _entry(miner, episode, score=0.5, h=7, p50=1.0):
    return {"miner": miner, "episode_id": episode, "horizon_days": h,
            "score": score,
            "prediction": {"cost_delta_pct": {"p10": 0.0, "p50": p50, "p90": 2.0}}}


def test_index_is_built_once_and_reused(tmp_path):
    import json as _json
    from hope.validator.daily_stream_weights import (
        ensure_fingerprint_indexes, _load_day_fingerprints,
    )
    _mini_receipt(tmp_path, "2026-08-20", [_entry("A", "e1"), _entry("B", "e1", p50=9.9)])
    assert ensure_fingerprint_indexes(str(tmp_path)) == 1
    assert ensure_fingerprint_indexes(str(tmp_path)) == 0   # warm: nothing to build
    idx = tmp_path / "fingerprints" / "2026-08-20.json"
    assert idx.exists()
    prints = _load_day_fingerprints(str(tmp_path), "2026-08-20")
    assert set(prints) == {"A", "B"} and prints["A"] != prints["B"]
    # a corrupted index heals itself from the receipt
    idx.write_text("{corrupt")
    prints2 = _load_day_fingerprints(str(tmp_path), "2026-08-20")
    assert prints2 == prints
    assert _json.loads(idx.read_text()) == prints


def test_suppression_via_index_matches_direct_receipts(tmp_path):
    from datetime import date
    from hope.validator.daily_stream_weights import (
        ensure_fingerprint_indexes, one_payer_suppression_from_receipts,
    )
    # day1: A alone produces the behaviour; day2: B copies A exactly
    _mini_receipt(tmp_path, "2026-08-20", [_entry("A", "e1")])
    _mini_receipt(tmp_path, "2026-08-21", [_entry("A", "e2"), _entry("B", "e2")])
    direct = one_payer_suppression_from_receipts(
        str(tmp_path), date(2026, 8, 21), {})
    ensure_fingerprint_indexes(str(tmp_path))
    via_index = one_payer_suppression_from_receipts(
        str(tmp_path), date(2026, 8, 21), {})
    assert direct == via_index == frozenset({"B"})   # the copy, not the author
