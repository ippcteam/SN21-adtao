"""Tests for the per-cell consensus engine."""

from types import SimpleNamespace

import pytest

from hope.consensus import CellConsensusBuilder, shape_stratum
from hope.consensus.cell_consensus import (
    LEVEL_CELL,
    LEVEL_TRANSITION,
    LEVEL_FAMILY,
)


def _episode(eid, transition_key, spend_bucket):
    return SimpleNamespace(
        episode_metadata=SimpleNamespace(episode_id=eid),
        account_state=SimpleNamespace(spend_bucket=spend_bucket),
        action_bundle=SimpleNamespace(
            bundle_summary=SimpleNamespace(transition_key=transition_key)
        ),
    )


def _horizon_outcome(cost, conv, eff):
    return SimpleNamespace(
        cost_delta_pct=cost, conversions_delta_pct=conv, efficiency_delta_pct=eff
    )


def _outcome(eid, cost, conv, eff):
    ho = _horizon_outcome(cost, conv, eff)
    return SimpleNamespace(episode_id=eid, t7=ho, t14=ho)


def _quant(p10, p50, p90):
    return SimpleNamespace(p10=p10, p50=p50, p90=p90)


def _prediction(eid, p50_cost):
    h = SimpleNamespace(
        cost_delta_pct=_quant(p50_cost - 10, p50_cost, p50_cost + 10),
        conversions_delta_pct=_quant(0, 5, 10),
        efficiency_delta_pct=_quant(-10, -5, 0),
    )
    return SimpleNamespace(episode_id=eid, horizons={"7": h, "14": h})


def _corpus(transition_key, shape, n, cost=50.0):
    eps, outs = [], []
    for i in range(n):
        eid = f"{transition_key}:{shape}:{i}"
        eps.append(_episode(eid, transition_key, shape))
        outs.append(_outcome(eid, cost + i, 5.0, -3.0))
    return eps, outs


def test_shape_stratum_folds_micro_and_low_into_small():
    assert shape_stratum("micro") == "small"
    assert shape_stratum("low") == "small"
    assert shape_stratum("mid") == "mid"
    assert shape_stratum("high") == "large"
    assert shape_stratum(None) == "mid"  # safe default


def test_publish_when_cell_clears_bar():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("BUDGET:up_large", "mid", 22)
    assert b.ingest_outcomes(eps, outs) == 22

    c = b.resolve("BUDGET:up_large", "mid", "14")
    assert c is not None
    assert c.level == LEVEL_CELL
    assert c.status == "publish"
    assert c.n == 22
    assert c.label == "BUDGET:up_large × mid"
    # cost p50 ~ median of 50..71
    assert 58 <= c.quantiles["cost"][1] <= 63
    assert 0.0 < c.confidence <= 1.0


def test_provisional_band():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("BUDGET:down_large", "mid", 14)
    b.ingest_outcomes(eps, outs)
    c = b.resolve("BUDGET:down_large", "mid", "14")
    assert c.status == "provisional"
    assert c.n == 14


def test_cascade_falls_back_to_transition_then_family():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    # 6 in (down × mid), 6 in (down × large): neither cell clears 10,
    # but the transition level (12) does.
    for shape in ("mid", "large"):
        eps, outs = _corpus("BUDGET:down", shape, 6)
        b.ingest_outcomes(eps, outs)

    c = b.resolve("BUDGET:down", "mid", "14")
    assert c is not None
    assert c.level == LEVEL_TRANSITION   # fell back one level
    assert c.n == 12
    assert c.shape is None

    # A brand-new shape with no cell + thin transition still reaches family.
    b2 = CellConsensusBuilder(publish_n=20, provisional_n=10)
    for tk in ("BUDGET:up", "BUDGET:down", "BUDGET:flat"):
        eps, outs = _corpus(tk, "large", 4)   # 4 each: no cell, no transition ≥10
        b2.ingest_outcomes(eps, outs)
    c2 = b2.resolve("BUDGET:up", "large", "7")
    assert c2.level == LEVEL_FAMILY          # only BUDGET family (12) clears
    assert c2.transition_key is None
    assert c2.n == 12


def test_returns_none_when_even_family_too_thin():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("CAMPAIGN_PAUSE", "mid", 3)
    b.ingest_outcomes(eps, outs)
    assert b.resolve("CAMPAIGN_PAUSE", "mid", "14") is None


def test_rolling_accumulation_via_state_merge():
    # Two epochs of 6 each in the same cell -> 12 total clears provisional.
    b1 = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("BUDGET:up_large", "large", 6)
    b1.ingest_outcomes(eps, outs)
    assert b1.resolve("BUDGET:up_large", "large", "14") is None  # only 6

    b2 = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps2, outs2 = _corpus("BUDGET:up_large", "large", 6, cost=80.0)
    b2.ingest_outcomes(eps2, outs2)

    # Merge epoch-1 state into epoch-2 builder -> 12 samples, now resolvable.
    b2.load_state(b1.to_state())
    c = b2.resolve("BUDGET:up_large", "large", "14")
    assert c is not None
    assert c.n == 12
    assert c.level == LEVEL_CELL


def test_prediction_source_and_skill_weighting():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, _ = _corpus("BUDGET:up_large", "mid", 12)
    eids = [e.episode_metadata.episode_id for e in eps]

    # two miners predict every episode; weighting biases the pooled p50.
    low_miner = [_prediction(eid, p50_cost=10.0) for eid in eids]
    high_miner = [_prediction(eid, p50_cost=90.0) for eid in eids]
    b.ingest_predictions(
        eps,
        {"low": low_miner, "high": high_miner},
        miner_weights={"low": 0.1, "high": 0.9},
    )
    c = b.resolve("BUDGET:up_large", "mid", "14", source="prediction")
    assert c is not None
    assert c.source == "prediction"
    # high-skill miner (predicting ~90) dominates the weighted pool.
    assert c.quantiles["cost"][1] > 50


def test_outcome_and_prediction_stores_are_independent():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("BUDGET:up", "mid", 12)
    b.ingest_outcomes(eps, outs)
    assert b.resolve("BUDGET:up", "mid", "14", source="outcome") is not None
    # no predictions ingested -> prediction source resolves to None
    assert b.resolve("BUDGET:up", "mid", "14", source="prediction") is None


def test_all_cells_report():
    b = CellConsensusBuilder(publish_n=20, provisional_n=10)
    eps, outs = _corpus("BUDGET:up_large", "mid", 22)
    b.ingest_outcomes(eps, outs)
    cells = b.all_cells(source="outcome", min_n=10)
    # cell + transition + family, each at 2 horizons = 6 readings ≥10
    labels = {(c.level, c.horizon) for c in cells}
    assert (LEVEL_CELL, "14") in labels
    assert (LEVEL_TRANSITION, "7") in labels
    assert (LEVEL_FAMILY, "14") in labels
