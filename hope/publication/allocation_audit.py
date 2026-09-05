"""The day's allocation audit, written as a public document.

WHY THIS EXISTS
    The rules promise that every detected group is published with its
    working, and that a miner can recompute any grouping from the day's
    receipt. Both halves were true of the computation and neither was true
    of anything a miner could fetch: the audit lived in the operator's own
    store, and the leaderboard showed each miner only the line about
    themselves.

    That is enough to be told you were excluded and not enough to check it.
    A miner could not see the group they were placed in, who kept the seat,
    or the numbers behind it — and could not look at a group they were not
    in at all, which is exactly what someone verifying the rule wants to do.

WHAT IS IN IT
    Whole groups, named. Publishing the payee alongside the excluded is the
    point: a suppression whose beneficiary is hidden reads as arbitrary, and
    the precedence claim ("the earliest submission earns") is only checkable
    if you can see who that was.

    Everything here is derived from the published receipt, so nothing in it
    is a fact a miner has to take on trust.
"""
from __future__ import annotations

import json
import os
from datetime import date

FEED_DIR = "allocation_audit"

HOW_TO_VERIFY = (
    "Every grouping here is computed from the predictions in the same day's "
    "receipt at /v1/daily/{day}/receipt. Recompute it: take each hotkey's "
    "predictions for the day, compare them, and check the members listed. "
    "Point-estimate groups are an exact match on the point estimates and "
    "need no parameters. Lineage groups use the four-signal test at the "
    "parameter version named in this document. If a grouping does not "
    "reproduce, say so with the day and the group and it will be answered."
)


def audit_path(root: str, day: date | str) -> str:
    return os.path.join(root, FEED_DIR, f"{day}.json")


def _count(value) -> int | None:
    """How many, whether the control recorded a number or the names.

    `coldkey_cap.dropped` is a count in the per-control status block and the
    list of hotkeys in the audit body. Reading one shape and publishing the
    other put ninety ss58 addresses where a summary count belonged.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return None


def _names(value) -> list:
    return sorted(value) if isinstance(value, (list, tuple, set)) else []


def build_document(day: date | str, collapse_audit: dict | None) -> dict:
    """The public shape of one day's audit.

    Reads defensively: the audit is assembled by several controls and a key
    a control did not write must publish as "this control recorded nothing",
    never as an exception that takes the whole document out.
    """
    audit = collapse_audit or {}
    policies = audit.get("policies") or {}
    lineage = audit.get("lineage") or {}
    coldkey = audit.get("coldkey_cap") or {}
    tenure = audit.get("tenure_gated") or {}

    # Both detectors' groups, in one list. They answer the same question — who
    # else runs this model, and which of them earns — and publishing only the
    # behavioural one left the exact-match exclusions naming nobody.
    groups = []
    for group in (list(lineage.get("groups") or [])
                  + list(audit.get("one_payer_groups") or [])):
        if not isinstance(group, dict):
            continue
        eliminated = list(group.get("eliminated") or [])
        groups.append({
            "kind": group.get("kind"),
            "seat_held_by": group.get("payee"),
            "excluded": eliminated,
            "size": len(eliminated) + 1,
            "evidence": group.get("evidence"),
        })
    # Largest first: the big clusters are what anyone checking the rule
    # looks at, and ordering by size keeps that stable day to day.
    groups.sort(key=lambda g: -g["size"])

    return {
        "day": str(day),
        "generated_from": f"/v1/daily/{day}/receipt",
        "parameters_version": (policies.get("lineage") or {}).get(
            "params_version"),
        "reference_exemption_configured": (policies.get("lineage") or {}).get(
            "exemption_configured"),
        "summary": {
            "groups": len(groups),
            "hotkeys_excluded_as_copies": len(audit.get("suppressed") or []),
            "hotkeys_excluded_by_coldkey_cap": _count(coldkey.get("dropped")),
            "hotkeys_below_tenure": len(tenure.get("hotkeys") or []),
        },
        "groups": groups,
        "one_coldkey_one_seat": {
            "applied": coldkey.get("applied"),
            "excluded_count": _count(coldkey.get("dropped")),
            "excluded": _names(coldkey.get("dropped")),
        },
        "tenure": {
            "minimum_scored_days": (policies.get("tenure") or {}).get(
                "min_days"),
            "excluded": sorted(tenure.get("hotkeys") or []),
            # How many days each of them actually has. Counting receipts a
            # hotkey appears in gives a different, higher number — one receipt
            # can carry entries settled on more than one date — so publishing
            # the figure the gate used is what makes the verdict checkable.
            "scored_days": dict(tenure.get("scored_days") or {}),
            "stood_down": bool(tenure.get("stood_down")),
        },
        "controls": policies,
        # Per-hotkey standings (rule amendment 2026-09-05): the relative
        # standing that ranks and pays, the absolute accuracy over the same
        # window (the board's headline number) and the rank. Written by the
        # allocation; absent on days before the amendment.
        "standings": dict(audit.get("standings") or {}),
        "how_to_verify": HOW_TO_VERIFY,
    }


def write_allocation_audit(root: str, day: date | str,
                           collapse_audit: dict | None) -> dict:
    """Write the day's audit and report what it contains.

    Rewritten on a re-run rather than refused: unlike the receipt, this is a
    derived view of the day's allocation, and a corrected allocation must not
    leave a stale audit standing beside it claiming otherwise.
    """
    document = build_document(day, collapse_audit)
    path = audit_path(root, day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return {"path": path, **document["summary"]}
