"""Reference validator burn composition.

A tracking validator must commit the SAME on-chain vector the operator commits —
miners scaled to (1 - burn), the burn UID carrying burn. Dropping the burn UID
diverges from consensus by the burn share and caps the tracker's vtrust at
~1 - burn (the reported 0.85 at a 0.15 burn). These pin that the reference loop
reproduces the operator's split.
"""

import pytest

from scripts.run_partner_validator import compose_onchain_weights


def test_burn_included_matches_operator_split():
    uids, weights = compose_onchain_weights([(7, 0.6), (9, 0.4)], 135, 0.15)
    got = dict(zip(uids, weights))
    assert got[7] == pytest.approx(0.51)    # 0.6 * (1 - 0.15)
    assert got[9] == pytest.approx(0.34)    # 0.4 * (1 - 0.15)
    assert got[135] == pytest.approx(0.15)  # burn share on the burn UID
    assert sum(weights) == pytest.approx(1.0)
    # miner mass is exactly 1 - burn — the share a burn-dropping tracker loses,
    # which is what caps its vtrust.
    assert sum(w for u, w in zip(uids, weights) if u != 135) == pytest.approx(0.85)


def test_no_burn_uid_commits_miner_vector_as_is():
    uids, weights = compose_onchain_weights([(7, 0.6), (9, 0.4)], None, 0.0)
    got = dict(zip(uids, weights))
    assert got[7] == pytest.approx(0.6)
    assert got[9] == pytest.approx(0.4)
    assert 135 not in got
    assert sum(weights) == pytest.approx(1.0)


def test_unnormalised_input_is_renormalised_then_burned():
    # Published miner weights need not sum to 1; scale by their own total first.
    uids, weights = compose_onchain_weights([(7, 3.0), (9, 1.0)], 135, 0.2)
    got = dict(zip(uids, weights))
    assert got[7] == pytest.approx(0.75 * 0.8)   # 0.6
    assert got[9] == pytest.approx(0.25 * 0.8)   # 0.2
    assert got[135] == pytest.approx(0.2)
    assert sum(weights) == pytest.approx(1.0)


def test_zero_or_full_burn_fraction_is_ignored():
    # A degenerate fraction (0 or >=1) is not applied — commit the miner vector.
    for frac in (0.0, 1.0, 1.5, -0.1):
        uids, weights = compose_onchain_weights([(7, 0.6), (9, 0.4)], 135, frac)
        assert 135 not in uids
        assert sum(weights) == pytest.approx(1.0)
