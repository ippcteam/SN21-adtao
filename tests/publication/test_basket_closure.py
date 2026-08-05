"""Receipt ↔ basket-manifest closure.

The check defends one property: the episodes we SCORED are the episodes the
subnet REVEALED. The tests below pin the three ways that could go wrong
quietly — the wrong basket compared, a healthy day reported as a fault, and
an unreadable manifest counted as a pass.
"""

from datetime import date

from hope.publication.basket_closure import (
    basket_day_for,
    check_receipt,
    episodes_by_basket_day,
)

SETTLE = date(2026, 9, 8)          # a day carrying all three horizons


def _entry(episode, horizon, finalized_on=SETTLE):
    return {"episode_id": episode, "horizon_days": horizon,
            "miner": "hkA", "score": 0.5, "finalized_on": str(finalized_on)}


def _receipt(*entries):
    return {"feed": "daily_receipt", "entries": list(entries)}


# ---- the clock --------------------------------------------------------------

def test_each_horizon_derives_a_different_basket_day():
    """A receipt for one settle day covers THREE baskets. Comparing all of
    its entries against one day's manifest would be the wrong check."""
    assert basket_day_for(SETTLE, 7) == date(2026, 8, 24)     # D-15
    assert basket_day_for(SETTLE, 14) == date(2026, 8, 17)    # D-22
    assert basket_day_for(SETTLE, 28) == date(2026, 8, 3)     # D-36


def test_the_first_basket_settles_on_the_published_dates():
    """The published schedule says BD-2026-08-03 settles 18 Aug / 25 Aug /
    8 Sep. The derivation must agree with the dates miners were given."""
    first = date(2026, 8, 3)
    assert basket_day_for(date(2026, 8, 18), 7) == first
    assert basket_day_for(date(2026, 8, 25), 14) == first
    assert basket_day_for(date(2026, 9, 8), 28) == first


def test_entries_group_by_their_own_basket_day():
    grouped = episodes_by_basket_day([
        _entry("e1", 7), _entry("e2", 7), _entry("e3", 14),
    ])
    assert grouped == {
        date(2026, 8, 24): {"e1", "e2"},
        date(2026, 8, 17): {"e3"},
    }


def test_an_entry_missing_its_clock_fields_is_skipped_not_guessed():
    grouped = episodes_by_basket_day([
        {"episode_id": "e1", "horizon_days": 7},              # no settle date
        {"episode_id": "e2", "finalized_on": str(SETTLE)},    # no horizon
        {"horizon_days": 7, "finalized_on": str(SETTLE)},     # no episode
    ])
    assert grouped == {}


# ---- the property under defence ---------------------------------------------

def test_a_scored_episode_absent_from_its_basket_is_a_violation():
    receipt = _receipt(_entry("e1", 7), _entry("rogue", 7))
    out = check_receipt(receipt, lambda _day: {"e1"})

    assert out["ok"] is False
    assert out["violations"] == [
        {"basket_day": "2026-08-24", "episode_ids": ["rogue"]}]
    assert out["baskets"][0]["status"] == "scored_episodes_not_in_basket"


def test_everything_scored_being_in_its_basket_passes():
    receipt = _receipt(_entry("e1", 7), _entry("e2", 14))
    out = check_receipt(receipt, lambda _day: {"e1", "e2", "e3"})

    assert out["ok"] is True and out["checked"] is True
    assert out["violations"] == []
    assert out["scored_episodes"] == 2 and out["verified_episodes"] == 2


def test_the_right_basket_is_compared_for_each_horizon():
    """The check must not pass by comparing everything against a union of
    every manifest — each horizon is held to its OWN basket."""
    manifests = {
        date(2026, 8, 24): {"seven"},      # 7-day basket
        date(2026, 8, 17): {"fourteen"},   # 14-day basket
    }
    good = _receipt(_entry("seven", 7), _entry("fourteen", 14))
    assert check_receipt(good, manifests.get)["ok"] is True

    # same two ids, horizons swapped: each is now in the OTHER day's basket
    swapped = _receipt(_entry("fourteen", 7), _entry("seven", 14))
    out = check_receipt(swapped, manifests.get)
    assert out["ok"] is False
    assert len(out["violations"]) == 2


# ---- what must NOT be called a fault ----------------------------------------

def test_an_episode_in_the_basket_but_unscored_is_normal():
    """Censored horizons are dropped by design and a miner who submitted
    nothing produces no entry. Flagging that would fault every healthy day."""
    receipt = _receipt(_entry("e1", 7))
    out = check_receipt(receipt, lambda _day: {"e1", "censored", "no_prediction"})

    assert out["ok"] is True
    assert out["baskets"][0]["unscored_in_basket"] == 2
    assert out["violations"] == []


# ---- unavailable is not the same as fine ------------------------------------

def test_an_unreadable_manifest_is_unchecked_not_passed():
    receipt = _receipt(_entry("e1", 7))
    out = check_receipt(receipt, lambda _day: None)

    assert out["ok"] is False              # not a pass
    assert out["checked"] is False
    assert out["violations"] == []         # and not a violation either
    assert out["unchecked_days"] == ["2026-08-24"]
    assert out["verified_episodes"] == 0
    assert out["baskets"][0]["status"] == "unchecked_manifest_unavailable"


def test_one_missing_manifest_does_not_hide_a_violation_elsewhere():
    receipt = _receipt(_entry("rogue", 7), _entry("e2", 14))

    def manifest_for(day):
        return None if day == date(2026, 8, 17) else {"e1"}

    out = check_receipt(receipt, manifest_for)
    assert out["ok"] is False
    assert out["violations"] == [
        {"basket_day": "2026-08-24", "episode_ids": ["rogue"]}]
    assert out["unchecked_days"] == ["2026-08-17"]


def test_an_empty_receipt_is_vacuously_ok_and_says_so():
    out = check_receipt(_receipt(), lambda _day: {"e1"})
    assert out["ok"] is True and out["scored_episodes"] == 0
    assert out["baskets"] == []
