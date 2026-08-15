"""The alpha-hold gate decides who gets paid, so its failure modes matter more
than its happy path: it must never confiscate on our own outage, and never
empty the vector."""

from hope.scoring.collateral_gate import (
    ENFORCE_ENV,
    apply_hold,
    effective_hold,
    metagraph_alpha_reader,
)

ON = {ENFORCE_ENV: "1"}
OFF: dict = {}


def _reader(table):
    return lambda key: table.get(key)


def test_off_by_default_returns_the_vector_untouched():
    w = {1: 0.5, 2: 0.5}
    out = apply_hold(w, 150.0, _reader({1: 0.0, 2: 0.0}), environ=OFF)
    assert out.applied is False
    assert out.weights == w


def test_below_floor_is_dropped_and_survivors_renormalise():
    w = {1: 0.4, 2: 0.4, 3: 0.2}
    out = apply_hold(w, 150.0, _reader({1: 200.0, 2: 200.0, 3: 2.13}),
                     environ=ON)
    assert out.applied is True
    assert 3 in out.excluded
    assert round(sum(out.weights.values()), 9) == 1.0
    # ratios preserved between survivors
    assert round(out.weights[1], 9) == round(out.weights[2], 9) == 0.5


def test_burn_share_is_untouched_and_freed_weight_goes_to_miners():
    """The burn destination rides in the same vector. Re-normalising over the
    whole thing would raise burn above the published rate — the freed weight
    belongs to the miners who qualified."""
    w = {1: 0.3, 2: 0.2, 135: 0.5}   # 135 = burn
    out = apply_hold(w, 150.0, _reader({1: 500.0, 2: 1.0, 135: 10_000.0}),
                     environ=ON, protected={135})
    assert round(sum(out.weights.values()), 9) == 1.0
    assert 2 in out.excluded
    assert round(out.weights[135], 9) == 0.5      # burn share unchanged
    assert round(out.weights[1], 9) == 0.5        # took the excluded miner's share


def test_protected_destination_is_never_itself_excluded():
    """The burn UID holds no alpha; without protection the gate would drop
    the burn destination itself."""
    w = {1: 0.5, 135: 0.5}
    out = apply_hold(w, 150.0, _reader({1: 500.0, 135: 0.0}),
                     environ=ON, protected={135})
    assert 135 not in out.excluded
    assert round(out.weights[135], 9) == 0.5


def test_capture_locked_alpha_also_satisfies_the_hold():
    """SN21_STAKING allows the floor to be met by earnings captured into the
    lock, not only by alpha held outright."""
    out = apply_hold({1: 1.0}, 150.0, _reader({1: 0.0}),
                     locked={1: 300.0}, environ=ON)
    assert out.applied is True and not out.excluded


def test_unreadable_hotkey_is_kept_not_confiscated():
    """A chain we could not read is our outage, never their failure."""
    out = apply_hold({1: 0.5, 2: 0.5}, 150.0, _reader({1: 500.0}), environ=ON)
    assert 2 not in out.excluded
    assert 2 in out.unreadable


def test_reader_that_raises_is_treated_as_unreadable():
    def boom(_):
        raise RuntimeError("rpc down")
    out = apply_hold({1: 1.0}, 150.0, boom, environ=ON)
    assert out.excluded == {} and out.unreadable == [1]


def test_refuses_to_empty_the_vector():
    """If everyone looks under-staked — which one bad metagraph read would
    produce — the gate must refuse rather than pay nobody."""
    out = apply_hold({1: 0.5, 2: 0.5}, 150.0, _reader({1: 0.0, 2: 0.0}),
                     environ=ON)
    assert out.applied is False
    assert "refusing to empty" in out.refused_reason
    assert out.weights == {1: 0.5, 2: 0.5}


def test_zero_floor_is_a_no_op():
    out = apply_hold({1: 1.0}, 0.0, _reader({1: 0.0}), environ=ON)
    assert out.applied is False


def test_force_runs_the_gate_for_dry_runs_without_the_flag():
    out = apply_hold({1: 0.5, 2: 0.5}, 150.0, _reader({1: 500.0, 2: 1.0}),
                     environ=OFF, force=True)
    assert out.applied is True and 2 in out.excluded


def test_effective_hold_takes_the_greater_of_the_two():
    assert effective_hold(1, _reader({1: 10.0}), {1: 400.0}) == 400.0
    assert effective_hold(1, _reader({1: 900.0}), {1: 400.0}) == 900.0
    assert effective_hold(1, _reader({}), None) is None


class _Meta:
    alpha_stake = [10.0, 250.0]
    hotkeys = ["hk0", "hk1"]


def test_metagraph_reader_by_uid_and_out_of_range():
    read = metagraph_alpha_reader(_Meta())
    assert read(1) == 250.0
    assert read(99) is None          # out of range -> unknown, not zero
    assert read("nope") is None


# ---- gate_uid_weight_vector: the on-chain weight-path convenience -----------
# The shared weight path calls this with parallel uid/weight lists. It must
# drop miners below the floor, keep the ones we cannot read a stake for
# (fail-open), no-op when the flag is off and not forced, refuse to empty, and
# preserve survivor order — the same guarantees as apply_hold, list-shaped.

from hope.scoring.collateral_gate import gate_uid_weight_vector


def _reader(alpha):
    return lambda uid: alpha.get(int(uid))


def test_gate_vector_drops_below_floor_keeps_unreadable():
    alpha = {10: 200.0, 11: 50.0, 12: 300.0}          # 13 absent -> unreadable
    uids, weights, res = gate_uid_weight_vector(
        [10, 11, 12, 13], [0.4, 0.3, 0.2, 0.1], _reader(alpha), 150.0,
        environ={}, force=True)
    assert set(uids) == {10, 12, 13}                  # 11 dropped; 13 kept
    assert 13 in res.unreadable and 11 in res.excluded
    assert abs(sum(weights) - 1.0) < 1e-9             # renormalised


def test_gate_vector_noop_when_flag_off_and_not_forced():
    uids, weights, res = gate_uid_weight_vector(
        [10, 11], [0.5, 0.5], _reader({10: 200.0, 11: 50.0}), 150.0,
        environ={}, force=False)
    assert not res.applied and uids == [10, 11] and weights == [0.5, 0.5]


def test_gate_vector_refuses_to_empty():
    # all readable and all below floor -> zero survivors -> keep original
    uids, weights, res = gate_uid_weight_vector(
        [10, 11], [0.5, 0.5], _reader({10: 10.0, 11: 10.0}), 150.0,
        environ={}, force=True)
    assert not res.applied and uids == [10, 11] and "refusing to empty" in res.refused_reason


def test_gate_vector_preserves_survivor_order():
    uids, _, _ = gate_uid_weight_vector(
        [12, 10], [0.5, 0.5], _reader({10: 200.0, 12: 300.0}), 150.0,
        environ={}, force=True)
    assert uids == [12, 10]
