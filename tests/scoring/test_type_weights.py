"""Type-weighted scoring: the measurement, the guards, and the gate.

WHY THIS EXISTS
    The per-prediction score is identical arithmetic for every change type,
    so nothing rewards being better at hard changes — 118 models inside a
    0.14 band, a leader that performance cannot displace. The fix weights
    entries by change-type family, with the weights DERIVED from measurement
    (headroom: how far the best models pull away from the median per family).

    What is pinned here is mostly the guards, because each one blocks a
    specific way this could go wrong:

      * headroom, not difficulty — a family nobody can predict must get NO
        extra weight, or the score becomes a lottery on noise;
      * minimum evidence — a rare family stays neutral at exactly 1.0;
      * floor/cap — one family can never dominate the standings;
      * frequency normalisation — the overall standing scale is preserved;
      * the ratification gate — a draft table can be built and modelled but
        must be REFUSED by the scoring loader: publishing the amendment is
        what turns it on, not editing an env var;
      * default-off — with no fn, day_flow output is byte-identical to the
        pre-type-weight pipeline.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hope.scoring.daily_score_flow import HorizonResult, day_flow
from hope.scoring.type_weights import (
    STATUS_DRAFT,
    STATUS_RATIFIED,
    TypeWeightTable,
    compute_table,
    load_table_for_scoring,
)


def _entries(fam, n_miners, per_miner, base=0.5, spread=0.0):
    """n_miners miners, per_miner entries each on family fam; miner i's
    scores sit at base + i*spread — spread>0 makes the family separable."""
    out = []
    for i in range(n_miners):
        for _ in range(per_miner):
            out.append((f"miner{i}", f"{fam}:x", base + i * spread))
    return out


GATES = dict(min_entries=100, min_miners=5, miner_min_n=10)


class TestHeadroomIsTheSignal:
    def test_a_separable_family_outweighs_a_noise_family(self):
        # SEP: miners clearly differ. NOISE: everyone identical.
        entries = (_entries("SEP", 10, 20, base=0.4, spread=0.02)
                   + _entries("NOISE", 10, 20, base=0.5, spread=0.0))
        t = compute_table(entries, **GATES)
        assert t.families["SEP"].weight > t.families["NOISE"].weight

    def test_pure_noise_gets_no_boost(self):
        """Everyone equally bad = zero headroom = at/below neutral after
        normalisation — never rewarded for being unpredictable."""
        entries = (_entries("SEP", 10, 20, base=0.4, spread=0.02)
                   + _entries("NOISE", 10, 20, base=0.2, spread=0.0))
        t = compute_table(entries, **GATES)
        assert t.families["NOISE"].headroom == 0.0
        assert t.families["NOISE"].weight <= 1.0

    def test_low_field_accuracy_alone_earns_nothing(self):
        """The trap from the design discussion: hard-but-noise must lose to
        easier-but-separable."""
        entries = (_entries("HARDNOISE", 10, 20, base=0.1, spread=0.0)
                   + _entries("EASYSKILL", 10, 20, base=0.7, spread=0.015))
        t = compute_table(entries, **GATES)
        assert (t.families["EASYSKILL"].weight
                > t.families["HARDNOISE"].weight)


class TestEvidenceGates:
    def test_a_rare_family_is_exactly_neutral(self):
        entries = (_entries("BIG", 10, 20, spread=0.02)
                   + _entries("RARE", 2, 3, spread=0.5))
        t = compute_table(entries, **GATES)
        assert t.families["RARE"].headroom is None
        assert t.families["RARE"].weight == 1.0

    def test_too_few_qualified_miners_is_neutral_even_with_volume(self):
        entries = (_entries("BIG", 10, 20, spread=0.02)
                   + _entries("ONEMINER", 1, 500, spread=0.0))
        t = compute_table(entries, **GATES)
        assert t.families["ONEMINER"].weight == 1.0

    def test_miners_below_their_own_min_n_do_not_shape_headroom(self):
        # 5 solid miners identical + 1 lucky one-entry miner far above:
        # the outlier must not manufacture headroom.
        entries = _entries("F", 6, 15, base=0.5, spread=0.0)
        entries.append(("lucky", "F:x", 0.99))
        t = compute_table(entries, min_entries=50, min_miners=5,
                          miner_min_n=10)
        assert t.families["F"].headroom == 0.0


class TestBounds:
    def test_cap_holds_under_extreme_headroom(self):
        entries = (_entries("EXTREME", 10, 20, base=0.1, spread=0.09)
                   + _entries("MILD", 10, 20, base=0.5, spread=0.001))
        t = compute_table(entries, **GATES)
        ratio = t.families["EXTREME"].weight / t.families["MILD"].weight
        assert ratio <= t.cap / t.floor + 1e-9

    def test_frequency_normalisation_is_exact_when_no_clamp_binds(self):
        """Bounds are the published guarantee and are applied LAST, so scale
        preservation is exact only when no weight hits the floor or cap —
        the amendment states it as 'approximately unchanged' for that
        reason. This pins the exact case…"""
        entries = (_entries("A", 10, 30, base=0.4, spread=0.02)
                   + _entries("B", 10, 10, base=0.5, spread=0.012))
        t = compute_table(entries, **GATES)
        assert all(t.floor < s.weight < t.cap
                   for s in t.families.values()), "clamp bound — bad fixture"
        mass = sum(s.n_entries * s.weight for s in t.families.values())
        n = sum(s.n_entries for s in t.families.values())
        assert mass / n == pytest.approx(1.0, abs=1e-9)

    def test_scale_drift_is_bounded_when_a_clamp_binds(self):
        """…and this bounds the approximate case: even with a binding clamp
        the frequency-weighted mean multiplier stays near 1."""
        entries = (_entries("A", 10, 30, base=0.4, spread=0.02)
                   + _entries("B", 10, 10, base=0.5, spread=0.005))
        t = compute_table(entries, **GATES)
        mass = sum(s.n_entries * s.weight for s in t.families.values())
        n = sum(s.n_entries for s in t.families.values())
        assert 0.8 <= mass / n <= 1.25

    def test_deterministic_for_identical_input(self):
        entries = _entries("A", 8, 15, spread=0.01) + _entries("B", 8, 15)
        a = compute_table(entries, **GATES).to_json()
        b = compute_table(list(entries), **GATES).to_json()
        assert a == b


class TestApplication:
    def test_unknown_and_unlabelled_are_neutral(self):
        t = compute_table(_entries("A", 10, 20, spread=0.02), **GATES)
        assert t.weight_for(None) == 1.0
        assert t.weight_for("NEVER_SEEN:x") == 1.0

    def test_lookup_is_by_family_not_full_key(self):
        t = compute_table(_entries("BUDGET", 10, 20, spread=0.02), **GATES)
        assert t.weight_for("BUDGET:up_large") == t.weight_for("BUDGET:down_small")

    def test_round_trips_through_json(self):
        t = compute_table(_entries("A", 10, 20, spread=0.02), **GATES)
        t2 = TypeWeightTable.from_json(t.to_json())
        assert t2.weight_for("A:x") == pytest.approx(t.weight_for("A:x"))


class TestTheRatificationGate:
    def _write(self, tmp_path, status):
        t = compute_table(_entries("A", 10, 20, spread=0.02), **GATES)
        t.status = status
        p = tmp_path / "table.json"
        t.save(str(p))
        return str(p)

    def test_a_draft_table_is_refused_for_scoring(self, tmp_path):
        p = self._write(tmp_path, STATUS_DRAFT)
        with pytest.raises(ValueError, match="amendment"):
            load_table_for_scoring(p)

    def test_a_ratified_table_loads(self, tmp_path):
        p = self._write(tmp_path, STATUS_RATIFIED)
        t = load_table_for_scoring(p)
        assert t.status == STATUS_RATIFIED

    def test_a_wrong_params_version_is_refused(self, tmp_path):
        p = self._write(tmp_path, STATUS_RATIFIED)
        d = json.loads(open(p).read())
        d["params_version"] = "type-weights-v0"
        open(p, "w").write(json.dumps(d))
        with pytest.raises(ValueError, match="params_version"):
            load_table_for_scoring(p)


class TestDayFlowIntegration:
    D = date(2026, 9, 1)

    def _results(self):
        return [HorizonResult(miner="m", episode_id=f"e{i}", horizon_days=7,
                              score=0.5, finalized_on=self.D,
                              resolution="high") for i in range(3)]

    def test_default_off_is_byte_identical(self):
        a = day_flow(self._results(), self.D)
        b = day_flow(self._results(), self.D, type_weight_fn=None)
        assert [(e.miner, e.weight, e.score) for e in a] == \
               [(e.miner, e.weight, e.score) for e in b]

    def test_the_multiplier_applies_to_weight_not_score(self):
        """Weighting evidence must never rewrite what the evidence SAYS."""
        out = day_flow(self._results(), self.D, type_weight_fn=lambda e: 2.0)
        base = day_flow(self._results(), self.D)
        for got, ref in zip(out, base):
            assert got.weight == pytest.approx(ref.weight * 2.0)
            assert got.score == ref.score

    def test_a_zero_or_negative_multiplier_is_ignored(self):
        """Weight 0 would erase evidence — a censorship primitive, not a
        weight. A broken fn degrades to neutral."""
        out = day_flow(self._results(), self.D, type_weight_fn=lambda e: 0.0)
        base = day_flow(self._results(), self.D)
        assert [e.weight for e in out] == [e.weight for e in base]

    def test_per_episode_weights_differ_per_entry(self):
        fn = lambda eid: 2.0 if eid == "e1" else 1.0   # noqa: E731
        out = {e.episode_id: e.weight
               for e in day_flow(self._results(), self.D, type_weight_fn=fn)}
        assert out["e1"] == pytest.approx(2 * out["e0"])


class TestBoundsAreTheFinalOperation:
    def test_no_weight_ever_exceeds_the_cap_after_normalisation(self):
        """The published guarantee is the bounds. Clamping before the
        frequency scale let the scale push a weight past the cap — the clamp
        must be the last thing that touches a weight."""
        # A dominant low-headroom family drives the scale above 1, which
        # would lift an at-cap family beyond it if clamped first.
        entries = (_entries("HUGE_FLAT", 12, 60, base=0.5, spread=0.0005)
                   + _entries("SPIKY", 12, 15, base=0.2, spread=0.08))
        t = compute_table(entries, **GATES)
        for fam, s in t.families.items():
            assert t.floor - 1e-9 <= s.weight <= t.cap + 1e-9, (fam, s.weight)


class TestPipelineGate:
    def test_no_env_means_off(self, monkeypatch):
        monkeypatch.delenv("SN21_TYPE_WEIGHTS_FILE", raising=False)
        from scripts.run_daily_pipeline import _type_weight_fn
        assert _type_weight_fn("/nonexistent") is None

    def test_a_draft_table_in_env_stays_off(self, tmp_path, monkeypatch):
        """Setting the env var must not be able to bypass ratification."""
        t = compute_table(_entries("A", 10, 20, spread=0.02), **GATES)
        p = tmp_path / "draft.json"
        t.save(str(p))
        monkeypatch.setenv("SN21_TYPE_WEIGHTS_FILE", str(p))
        from scripts.run_daily_pipeline import _type_weight_fn
        assert _type_weight_fn(str(tmp_path)) is None

    def test_a_ratified_table_activates_and_labels_resolve(self, tmp_path,
                                                           monkeypatch):
        from scripts.run_daily_pipeline import (
            _type_weight_fn,
            write_transition_key_map,
        )
        t = compute_table(_entries("BUDGET", 10, 20, spread=0.02), **GATES)
        t.status = STATUS_RATIFIED
        p = tmp_path / "ratified.json"
        t.save(str(p))
        write_transition_key_map(str(tmp_path), "BD-x", [{
            "episode_id": "ep1",
            "action_bundle": {"bundle_summary":
                              {"transition_key": "BUDGET:up_large"}},
        }])
        monkeypatch.setenv("SN21_TYPE_WEIGHTS_FILE", str(p))
        fn = _type_weight_fn(str(tmp_path))
        assert fn is not None
        assert fn("ep1") == pytest.approx(t.weight_for("BUDGET:up_large"))
        assert fn("unlabelled-ep") == 1.0


class TestUnknownIsNeverAFamily:
    def test_a_dominant_unlabelled_bucket_stays_exactly_neutral(self):
        """Caught by REAL data on 2026-09-01: 69.9% of entries were
        unlabelled, the UNKNOWN bucket measured the highest headroom, and
        compute_table handed it weight 1.28 — a BOOST for having no label.
        UNKNOWN is the absence of a type, not a type; whatever it measures,
        its weight is exactly 1.0."""
        entries = (
            [(f"m{i}", None, 0.4 + i * 0.02) for i in range(12)
             for _ in range(30)]                       # unlabelled, separable
            + _entries("BUDGET", 12, 20, base=0.55, spread=0.002)
            + _entries("TARGET", 12, 20, base=0.5, spread=0.02))
        t = compute_table(entries, **GATES)
        assert t.families["UNKNOWN"].weight == 1.0
        assert t.weight_for(None) == 1.0
        # the real families still weight relative to each other
        assert t.families["TARGET"].weight > t.families["BUDGET"].weight
