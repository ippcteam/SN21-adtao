"""Tests for the epoch consensus reporter + rolling persistence."""

from types import SimpleNamespace

from hope.consensus.epoch_consensus import (
    build_epoch_consensus,
    compute_and_persist_consensus,
    read_rolling_state,
    write_rolling_state,
    artifact_path_for,
)


def _episode(eid, transition_key, spend_bucket="mid"):
    return SimpleNamespace(
        episode_metadata=SimpleNamespace(episode_id=eid),
        account_state=SimpleNamespace(spend_bucket=spend_bucket),
        action_bundle=SimpleNamespace(
            bundle_summary=SimpleNamespace(transition_key=transition_key)
        ),
    )


def _outcome(eid, cost):
    ho = SimpleNamespace(cost_delta_pct=cost, conversions_delta_pct=5.0, efficiency_delta_pct=-3.0)
    return SimpleNamespace(episode_id=eid, t7=ho, t14=ho)


def _quant(p10, p50, p90):
    return SimpleNamespace(p10=p10, p50=p50, p90=p90)


def _prediction(eid, p50_cost):
    h = SimpleNamespace(
        cost_delta_pct=_quant(p50_cost - 5, p50_cost, p50_cost + 5),
        conversions_delta_pct=_quant(0, 5, 10),
        efficiency_delta_pct=_quant(-6, -3, 0),
    )
    return SimpleNamespace(episode_id=eid, horizons={"7": h, "14": h})


def _corpus(transition_key, n, shape="mid", cost=50.0):
    eps = [_episode(f"{transition_key}:{i}", transition_key, shape) for i in range(n)]
    outs = [_outcome(f"{transition_key}:{i}", cost + i) for i in range(n)]
    return eps, outs


def test_build_artifact_outcome_track():
    eps, outs = _corpus("BUDGET:up_large", 22)
    artifact, state = build_epoch_consensus("E1", eps, outs, {}, {})
    assert artifact["schema_version"] == "consensus-v1"
    assert artifact["epoch_id"] == "E1"
    assert artifact["outcomes_ingested"] == 22
    assert artifact["elite_miner_count"] == 0
    # the cell publishes at transition_x_shape level
    pub = [c for c in artifact["cells"]["outcome"]
           if c["level"] == "transition_x_shape" and c["status"] == "publish"]
    assert pub and pub[0]["n"] == 22
    assert artifact["cells"]["prediction"] == []
    assert state  # rolling state returned


def test_only_elite_miners_feed_prediction_track():
    eps, outs = _corpus("BUDGET:up_large", 12)
    eids = [e.episode_metadata.episode_id for e in eps]
    predictions = {
        "good": [_prediction(eid, 60.0) for eid in eids],
        "bad": [_prediction(eid, 5.0) for eid in eids],
    }
    # 'good' beat baseline (skill 0.4), 'bad' did not (skill 0.0) -> only 'good'.
    miner_scores = {
        "good": SimpleNamespace(skill_score=0.4),
        "bad": SimpleNamespace(skill_score=0.0),
    }
    artifact, _ = build_epoch_consensus("E1", eps, outs, predictions, miner_scores)
    assert artifact["elite_miner_count"] == 1
    assert len(artifact["cells"]["prediction"]) > 0


def test_rolling_state_accumulates_across_calls():
    # epoch 1: 6 samples -> nothing publishes/provisional at cell level
    eps1, outs1 = _corpus("BUDGET:down", 6, shape="large")
    a1, s1 = build_epoch_consensus("E1", eps1, outs1, {}, {})
    cell1 = [c for c in a1["cells"]["outcome"]
             if c["level"] == "transition_x_shape"]
    assert all(c["status"] == "fallback" for c in cell1)  # n=6 < 10

    # epoch 2: another 6, seeded with epoch-1 state -> 12, now provisional
    eps2, outs2 = _corpus("BUDGET:down", 6, shape="large", cost=80.0)
    a2, _ = build_epoch_consensus("E2", eps2, outs2, {}, {}, prior_state=s1)
    cell2 = [c for c in a2["cells"]["outcome"]
             if c["level"] == "transition_x_shape"]
    assert any(c["n"] == 12 and c["status"] == "provisional" for c in cell2)


def test_compute_and_persist_writes_artifact_and_state(tmp_path):
    eps, outs = _corpus("BUDGET:up_large", 22)
    path, artifact = compute_and_persist_consensus(
        "E1", eps, outs, {}, {}, base_dir=tmp_path,
    )
    assert path.exists()
    assert path == artifact_path_for("E1", base_dir=tmp_path)
    # rolling state persisted + reloadable
    state = read_rolling_state(base_dir=tmp_path)
    assert state is not None
    assert "outcome" in state


def test_persisted_state_feeds_next_epoch(tmp_path):
    # two epochs persisted to the same dir accumulate via the rolling state file
    eps1, outs1 = _corpus("BUDGET:up", 6, shape="large")
    compute_and_persist_consensus("E1", eps1, outs1, {}, {}, base_dir=tmp_path)

    eps2, outs2 = _corpus("BUDGET:up", 6, shape="large", cost=90.0)
    _, artifact2 = compute_and_persist_consensus(
        "E2", eps2, outs2, {}, {}, base_dir=tmp_path,
    )
    cell = [c for c in artifact2["cells"]["outcome"]
            if c["level"] == "transition_x_shape"]
    assert any(c["n"] == 12 for c in cell)  # epoch-1 state carried in
