"""One payer per model means exactly one — the uid72 / uid95 case of 7-8 Oct 2026.

Two different hotkeys produced byte-identical h7/h14 predictions. The lineage
test seated the hotkey seen first in the receipts (uid72); the identical-
predictions test seated the other one (uid95), because both first produced
that day's fingerprint on the same day and the tie fell to the hotkey string.
The two exclusions added up and the model earned nothing. SN21_REWARDS.md:
"Identical models pay once ... only the earliest submission earns."
"""
import json
import os
from datetime import date

from hope.scoring.duplication import prediction_collisions
from hope.validator.daily_stream_weights import (
    one_payer_suppression_from_receipts,
    orphaned_payees,
)

UID72 = "5HErWbYgik799qxV83ms11tHgKETPtDYBWNZvAkpLVKYVafn"   # first in receipts 2026-09-19
UID95 = "5ELkgFdJeoC19YZM8Bzg2G9g2JwYiKD8TkqsfJ6fEDRxdgWt"   # first in receipts 2026-09-30
OTHER = "5Fother000000000000000000000000000000000000000000"
assert UID95 < UID72        # why the old tie-break picked uid95


def _ledger(tmp_path, days: dict) -> str:
    """A ledger root with one receipt stub and one fingerprint index per day."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "receipts"))
    os.makedirs(os.path.join(root, "fingerprints"))
    for day_name, prints in days.items():
        with open(os.path.join(root, "receipts", f"{day_name}.json"), "w") as f:
            json.dump({"document": {"metrics": {"entries": []}}}, f)
        with open(os.path.join(root, "fingerprints", f"{day_name}.json"), "w") as f:
            json.dump(prints, f)
    return root


def test_same_day_tie_goes_to_the_senior_hotkey_not_the_alphabet():
    groups = prediction_collisions(
        {UID72: "fpA", UID95: "fpA"},
        precedence={UID72: "2026-09-19", UID95: "2026-09-30"},
        history={("fpA", UID72): "2026-10-07", ("fpA", UID95): "2026-10-07"})
    assert groups[0].original == UID72 and groups[0].copies == (UID95,)


def test_receipt_path_seats_uid72_on_the_7_oct_shape(tmp_path):
    root = _ledger(tmp_path, {
        "2026-09-19": {UID72: "fp-sep19"},
        "2026-09-30": {UID72: "fp-sep30-a", UID95: "fp-sep30-b"},
        "2026-10-07": {UID72: "fp-oct7", UID95: "fp-oct7", OTHER: "fp-x"},
    })
    stats = {}
    suppressed = one_payer_suppression_from_receipts(
        root, date(2026, 10, 7), {"SN21_ONE_PAYER_PER_MODEL": "1"}, stats)
    assert suppressed == frozenset({UID95})
    assert stats["group_detail"][0]["payee"] == UID72
    assert stats["seniority"] == {UID72: "2026-09-19", UID95: "2026-09-30"}


def test_conflicting_tests_never_leave_a_model_without_an_earner():
    # The published 7 Oct audit: lineage seats uid72, prediction groups seat uid95.
    groups = [(UID72, [UID95]), (UID95, [UID72]), (UID95, [UID72])]
    restored = orphaned_payees(groups, {UID72, UID95},
                               seniority={UID72: "2026-09-19", UID95: "2026-09-30"},
                               preferred=[UID72])
    assert restored == {UID72}


def test_lineage_payee_is_preferred_when_seniority_is_unknown():
    restored = orphaned_payees([(UID72, [UID95]), (UID95, [UID72])], {UID72, UID95},
                               seniority={}, preferred=[UID72])
    assert restored == {UID72}


def test_nothing_is_restored_when_a_member_still_earns():
    assert orphaned_payees([(UID72, [UID95])], {UID95}) == set()


def test_groups_that_share_a_hotkey_are_one_set():
    a, b, c = "5Aaa", "5Bbb", "5Ccc"
    restored = orphaned_payees([(a, [b]), (b, [c]), (c, [a])], {a, b, c},
                               seniority={a: "2026-09-01", b: "2026-09-02", c: "2026-09-03"})
    assert restored == {a}
