"""Copy detection, and precedence over a copy.

Reported by a miner on 2026-08-06, and every step confirmed against the code
before any of this was written: images are public and anonymously pullable,
the admission gate does not look at duplication, an identical container earns
an identical standing, and the curve's tie-break was (standing desc, hotkey
ASC) — so the copy holding the lexicographically smaller hotkey took the
slot. Copying was not merely possible, it was strictly better than building,
for the price of grinding a hotkey.

The tie-break is the part that paid, and it is the part these tests pin.
"""

import pytest

from hope.scoring.duplication import (
    Submission,
    digest_collisions,
    find_duplicates,
    fingerprints_from_receipt,
    first_seen_fingerprints,
    precedence_map,
    prediction_collisions,
    prediction_fingerprint,
)
from hope.scoring.weight_curve import CurveParams, curve_weights

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


# ---- the exploit, and that it is closed ------------------------------------

def test_a_copy_no_longer_wins_a_tie_by_owning_a_smaller_hotkey():
    """THE REPORTED ATTACK. 'aaa' copies 'zzz', ties exactly, and used to take
    the slot purely on lexical order."""
    standings = {"zzz_original": 0.72, "aaa_copy": 0.72}
    precedence = {"zzz_original": 100, "aaa_copy": 5000}   # copy committed later

    weights = curve_weights(standings, precedence=precedence)

    assert weights["zzz_original"] > weights["aaa_copy"], (
        "the earlier model must outrank an identical later one")


def test_without_precedence_the_old_lexical_behaviour_is_unchanged():
    """Existing callers that pass no precedence keep their exact prior
    ordering — the fix is additive, not a silent re-ranking of live weights."""
    standings = {"zzz": 0.72, "aaa": 0.72}
    weights = curve_weights(standings)
    assert weights["aaa"] > weights["zzz"]


def test_an_unknown_commit_time_never_outranks_a_known_earlier_one():
    """A copier must not gain by making their precedence unreadable."""
    standings = {"known_early": 0.6, "unknown": 0.6}
    weights = curve_weights(standings, precedence={"known_early": 42})
    assert weights["known_early"] > weights["unknown"]


def test_precedence_never_beats_a_genuinely_better_model():
    """Being first is a tie-break, not a moat. A newcomer that actually scores
    higher must still win, or the curve stops rewarding accuracy."""
    standings = {"incumbent": 0.60, "newcomer": 0.75}
    weights = curve_weights(standings, precedence={"incumbent": 1,
                                                   "newcomer": 9999})
    assert weights["newcomer"] > weights["incumbent"]


def test_ordering_stays_deterministic_when_precedence_also_ties():
    same = {"b_miner": 0.5, "a_miner": 0.5}
    prec = {"b_miner": 77, "a_miner": 77}
    first = curve_weights(same, precedence=prec)
    second = curve_weights(dict(reversed(same.items())), precedence=prec)
    assert first == second


# ---- detecting the same bytes ----------------------------------------------

def test_two_hotkeys_on_one_digest_are_a_copy_group():
    subs = [
        Submission("original", DIGEST_A, first_seen_block=100),
        Submission("copycat", DIGEST_A, first_seen_block=900),
    ]
    (group,) = digest_collisions(subs)

    assert group.kind == "same_digest"
    assert group.original == "original"
    assert group.copies == ("copycat",)
    assert "block 100" in group.evidence


def test_a_unique_digest_is_not_a_copy():
    subs = [Submission("a", DIGEST_A, 1), Submission("b", DIGEST_B, 2)]
    assert digest_collisions(subs) == []


def test_precedence_within_a_block_is_deterministic():
    subs = [
        Submission("zeta", DIGEST_A, first_seen_block=500),
        Submission("alpha", DIGEST_A, first_seen_block=500),
    ]
    (group,) = digest_collisions(subs)
    assert group.original == "alpha"


def test_precedence_follows_the_model_a_hotkey_is_running():
    """SEMANTICS CHANGED after a miner's report (2026-08-06): precedence used
    to be the hotkey's earliest commit of ANYTHING, which crowned an old
    hotkey with a junk history as the "original" of a model it copied last
    week. Hotkey seniority is not authorship — precedence follows the model
    the hotkey is running now."""
    subs = [
        Submission("miner", DIGEST_A, first_seen_block=100),
        Submission("miner", DIGEST_B, first_seen_block=800),   # switched model
    ]
    assert precedence_map(subs)["miner"] == 800


def test_recommitting_the_same_digest_keeps_the_earliest_block():
    """Re-pushing your own model must not reset your place in the queue."""
    subs = [
        Submission("miner", DIGEST_A, first_seen_block=100),
        Submission("miner", DIGEST_A, first_seen_block=800),
    ]
    assert precedence_map(subs)["miner"] == 100


# ---- detecting the same behaviour from different bytes ---------------------

def test_a_rebuilt_image_is_caught_by_identical_predictions():
    """The digest differs, so byte comparison cannot see it. Two honestly
    different models do not agree to full float precision across a basket."""
    predictions = {"ep1": {"7": {"cost_delta_pct": {"p50": -0.0512345}}}}
    shared = prediction_fingerprint(predictions)

    groups = prediction_collisions(
        {"original": shared, "rebuilt": shared, "genuine": "different"},
        precedence={"original": 10, "rebuilt": 9000},
    )

    assert len(groups) == 1
    assert groups[0].original == "original"
    assert groups[0].copies == ("rebuilt",)
    assert "genuine" not in groups[0].members


def test_the_fingerprint_ignores_key_order_but_not_the_numbers():
    a = {"ep1": {"7": {"x": 1}, "14": {"y": 2}}}
    b = {"ep1": {"14": {"y": 2}, "7": {"x": 1}}}
    different = {"ep1": {"7": {"x": 1.0000001}, "14": {"y": 2}}}

    assert prediction_fingerprint(a) == prediction_fingerprint(b)
    assert prediction_fingerprint(a) != prediction_fingerprint(different)


def test_a_miner_with_no_predictions_is_not_grouped_with_another_silent_one():
    """Two miners who produced nothing are not the same model."""
    assert prediction_collisions({"a": "", "b": ""}) == []


# ---- the report is evidence, not an accusation -----------------------------

def test_the_report_names_the_copies_and_shows_its_working():
    subs = [
        Submission("original", DIGEST_A, 100),
        Submission("copycat", DIGEST_A, 900),
        Submission("honest", DIGEST_B, 200),
    ]
    report = find_duplicates(subs)

    assert report.copied_hotkeys == {"copycat"}
    payload = report.as_dict()
    assert payload["total_groups"] == 1
    # A miner accused of copying can check the claim rather than take it.
    assert DIGEST_A in payload["groups"][0]["evidence"]
    assert "honest" not in payload["copied_hotkeys"]


def test_detection_reports_precedence_and_does_not_decide_punishment():
    """Excluding or zero-weighting a copy is a governance call with real
    economic consequences. This layer establishes the fact of who was first;
    ranking already removes the free win."""
    report = find_duplicates([
        Submission("original", DIGEST_A, 1),
        Submission("copycat", DIGEST_A, 2),
    ])
    group = report.groups[0]
    assert group.original == "original"
    assert not hasattr(group, "penalty")


@pytest.mark.parametrize("blocks,expected", [
    ((1, 2, 3), "first"),
    ((3, 2, 1), "third"),
])
def test_the_earliest_commit_is_the_original_whatever_the_hotkey(blocks, expected):
    names = ["first", "second", "third"]
    subs = [Submission(n, DIGEST_A, b) for n, b in zip(names, blocks)]
    assert digest_collisions(subs)[0].original == expected


def test_max_earners_still_caps_the_field_with_precedence_applied():
    """A copy bot with many UIDs must not be able to widen the earning set."""
    standings = {f"hk{i:02d}": 0.5 for i in range(40)}
    precedence = {f"hk{i:02d}": i for i in range(40)}
    weights = curve_weights(standings, CurveParams(), precedence=precedence)
    assert sum(1 for w in weights.values() if w > 0) == 20


# ---- the second report (2026-08-06): seniority games and rebuilds ----------

def test_an_old_hotkey_with_a_junk_history_does_not_own_a_model_it_copied():
    """THE SECOND REPORTED ATTACK. The copier registered long ago with junk,
    so under hotkey-level precedence their ancient block outranked the author
    — the copier was crowned original and the AUTHOR was flagged as the copy.
    Precedence must follow the model, not the hotkey's seniority."""
    junk = "sha256:" + "9" * 64
    rebuilt = "sha256:" + "8" * 64
    fingerprint = "identical-behaviour"

    subs = [
        Submission("old_attacker", junk, first_seen_block=100),
        Submission("author", DIGEST_A, first_seen_block=9000),
        Submission("old_attacker", rebuilt, first_seen_block=9100),
    ]
    (group,) = prediction_collisions(
        {"author": fingerprint, "old_attacker": fingerprint},
        precedence_map(subs),
    )
    assert group.original == "author"
    assert group.copies == ("old_attacker",)


def test_an_author_who_rebuilds_keeps_precedence_via_recorded_history():
    """Scenario D from the audit. A rebuild changes the digest, so on commit
    order alone the copier of the OLD build suddenly precedes the author —
    the author reads as a copy of their own model. What survives a rebuild is
    the behaviour, and the receipts prove who produced it first."""
    rebuilt_copy = "sha256:" + "8" * 64
    authors_new_build = "sha256:" + "7" * 64
    fingerprint = "the-behaviour"

    subs = [
        Submission("author", DIGEST_A, first_seen_block=9000),
        Submission("copier", rebuilt_copy, first_seen_block=9100),
        Submission("author", authors_new_build, first_seen_block=9200),
    ]
    # Without history the commit clock betrays the author…
    (naive,) = prediction_collisions(
        {"author": fingerprint, "copier": fingerprint}, precedence_map(subs))
    assert naive.original == "copier"

    # …and the receipts put it right: the author was RECORDED producing this
    # behaviour before the copier existed.
    history = first_seen_fingerprints([
        ("2026-08-18", {"author": fingerprint}),
        ("2026-08-20", {"author": fingerprint, "copier": fingerprint}),
    ])
    (informed,) = prediction_collisions(
        {"author": fingerprint, "copier": fingerprint},
        precedence_map(subs), history)
    assert informed.original == "author"
    assert informed.copies == ("copier",)


def test_history_is_built_from_the_receipt_itself():
    """No new storage: the receipt already carries every miner's predictions
    verbatim, so anyone can recompute these fingerprints from the published
    record."""
    entries = [
        {"miner": "hkA", "episode_id": "e1", "horizon_days": 7,
         "prediction": {"cost_delta_pct": {"p50": -0.05}}},
        {"miner": "hkB", "episode_id": "e1", "horizon_days": 7,
         "prediction": {"cost_delta_pct": {"p50": -0.05}}},
        {"miner": "hkSilent", "episode_id": "e1", "horizon_days": 7,
         "prediction": None},
    ]
    prints = fingerprints_from_receipt(entries)
    assert prints["hkA"] == prints["hkB"]        # identical behaviour is visible
    assert "hkSilent" not in prints              # silence is not a behaviour


def test_history_keeps_the_earliest_sighting():
    history = first_seen_fingerprints([
        ("2026-08-20", {"hk": "fp"}),
        ("2026-08-18", {"hk": "fp"}),
    ])
    assert history[("fp", "hk")] == "2026-08-18"


def test_front_running_is_settled_by_commit_order_which_is_the_documented_defence():
    """Scenario E. A registry watcher who commits the author's digest FIRST
    wins on chain order — the chain cannot tell builder from watcher. The
    defence is procedural and documented: commit the digest while the image
    is still private, then make it public. This test pins the behaviour so
    the docs and the code cannot drift apart silently."""
    subs = [
        Submission("watcher", DIGEST_A, first_seen_block=9020),
        Submission("author", DIGEST_A, first_seen_block=9050),
    ]
    (group,) = digest_collisions(subs)
    assert group.original == "watcher"           # chain order, stated plainly

    committed_first = [
        Submission("author", DIGEST_A, first_seen_block=9010),
        Submission("watcher", DIGEST_A, first_seen_block=9020),
    ]
    (defended,) = digest_collisions(committed_first)
    assert defended.original == "author"
