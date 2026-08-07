"""Detecting copied models, and deciding who has precedence over a copy.

THE ATTACK, as reported by a miner on 2026-08-06 and confirmed against the
code before anything here was written:

    Images must be public and anonymously pullable, so anyone watching the
    chain can pull a newly committed model and re-commit it under their own
    hotkey within minutes. The admission gate scores a model against the
    naive baseline and does not look at duplication, so a copy is admitted
    on the original's merits. An identical container produces identical
    predictions, therefore an identical standing — and the curve's tie-break
    was (standing desc, hotkey ASC), so the copy with the lexicographically
    smaller hotkey took the slot.

That last step is the part that made copying strictly profitable rather than
merely possible, and it is the part fixed here.

TWO KINDS OF COPY, TWO DETECTORS
    Same bytes     two hotkeys commit the SAME digest. Objective, provable
                   from chain alone, no inference.
    Same behaviour a rebuilt image has a different digest but produces
                   identical predictions. Two independently built models do
                   not agree to full float precision across a whole basket;
                   agreement at that resolution is the same model.

PRECEDENCE, NOT PUNISHMENT
    This module answers "who was first", which is a fact, and deliberately
    does NOT decide what happens to the copy — whether it is excluded,
    zero-weighted or left to compete is a governance call with real economic
    consequences. Ranking by precedence already removes the free win; the
    policy layer can go further when it is ruled on.

    Precedence is the block height at which a digest was first committed.
    Chain order is the one ordering a copier cannot manipulate after the
    fact: they can grind a favourable hotkey, but they cannot commit before
    the model they are copying existed.

Pure module: no chain calls, no I/O. Everything arrives as data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Submission:
    """One miner's model as the chain records it."""
    hotkey: str
    digest: str
    first_seen_block: int


@dataclass(frozen=True)
class CopyGroup:
    """A set of miners running the same model, and who got there first."""
    kind: str                    # "same_digest" | "same_predictions"
    original: str                # hotkey with precedence
    copies: tuple[str, ...]      # every other member, earliest-first
    evidence: str
    digest: str | None = None    # the shared digest, for same_digest groups

    @property
    def members(self) -> tuple[str, ...]:
        return (self.original,) + self.copies


def digest_collisions(submissions: Iterable[Submission]) -> list[CopyGroup]:
    """Hotkeys committing byte-identical images.

    The strongest signal available: no inference, no threshold, just the same
    sha256 committed twice. Precedence goes to the lowest block, ties on block
    broken by hotkey so the result is deterministic within a block.
    """
    by_digest: dict[str, list[Submission]] = {}
    for sub in submissions:
        by_digest.setdefault(sub.digest, []).append(sub)

    groups = []
    for digest, subs in sorted(by_digest.items()):
        if len(subs) < 2:
            continue
        ordered = sorted(subs, key=lambda s: (s.first_seen_block, s.hotkey))
        groups.append(CopyGroup(
            kind="same_digest",
            original=ordered[0].hotkey,
            copies=tuple(s.hotkey for s in ordered[1:]),
            evidence=(f"{len(subs)} hotkeys committed {digest}; earliest at "
                      f"block {ordered[0].first_seen_block}"),
            digest=digest,
        ))
    return groups


def prediction_fingerprint(predictions: Mapping) -> str:
    """A stable hash of one miner's predictions for a basket.

    Canonical JSON with sorted keys, so the fingerprint depends on the
    numbers and not on dict ordering or whitespace.

    THIS TEST IS EXACT, AND EXACTNESS WAS THE HOLE. The docstring used to
    argue that two honestly different models "would have to agree exactly" —
    true, and the converse is what mattered: two IDENTICAL models need only
    disagree in the last decimal to be counted as separate payees. Reported
    2026-08-07. Kept because byte-identical output is still the cleanest
    evidence there is, but it is no longer the whole test — see
    `distance_collisions`, which subsumes it.
    """
    canonical = json.dumps(predictions, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def fingerprints_from_receipt(entries: Iterable[Mapping]) -> dict[str, str]:
    """{miner: fingerprint} from one day's receipt entries.

    The receipt already records every miner's predictions verbatim, so the
    published record IS the behaviour history — no new storage, and anyone
    can recompute these fingerprints from the same documents verify_day
    reads. A miner with no usable predictions gets NO fingerprint rather
    than a fingerprint of emptiness: two silent miners are not the same
    model.
    """
    per_miner: dict[str, dict] = {}
    for entry in entries:
        miner = entry.get("miner")
        prediction = entry.get("prediction")
        if not miner or prediction is None:
            continue
        per_miner.setdefault(miner, {}).setdefault(
            str(entry.get("episode_id")), {})[str(entry.get("horizon_days"))] = prediction
    return {miner: prediction_fingerprint(preds)
            for miner, preds in per_miner.items() if preds}


def first_seen_fingerprints(
    days: Iterable[tuple],
) -> dict[tuple[str, str], object]:
    """{(fingerprint, hotkey): earliest marker} across published history.

    `days` is an iterable of (marker, {hotkey: fingerprint}) — the marker is
    whatever the caller orders time by (ISO day strings from receipts, block
    numbers…), and markers must be mutually comparable within one call.
    """
    first: dict[tuple[str, str], object] = {}
    for marker, fingerprints in days:
        for hotkey, fingerprint in fingerprints.items():
            if not fingerprint:
                continue
            key = (fingerprint, hotkey)
            held = first.get(key)
            if held is None or marker < held:
                first[key] = marker
    return first


def prediction_collisions(
    fingerprints: Mapping[str, str],
    precedence: Mapping[str, int] | None = None,
    history: Mapping[tuple[str, str], object] | None = None,
) -> list[CopyGroup]:
    """Miners whose predictions are identical — the rebuilt-image case.

    `fingerprints` is {hotkey: fingerprint} for the CURRENT basket;
    `precedence` is {hotkey: first block of the model it is running};
    `history` is {(fingerprint, hotkey): earliest marker this hotkey was
    RECORDED producing this behaviour} — built from receipts via
    `first_seen_fingerprints`.

    WHY HISTORY OUTRANKS COMMIT ORDER (audit 2026-08-06, scenario D). An
    author who rebuilds their own image gets a new digest, and a new digest
    means a fresh commit block — so on commit order alone, the copier who
    copied the OLD build suddenly precedes the author, and the author is
    flagged as a copy of their own model. What survives a rebuild is the
    behaviour, and the receipts prove who produced it first. So members of
    a behaviour group are ordered by: recorded history of this fingerprint,
    then commit precedence, then hotkey. A hotkey missing from either
    source sorts after every hotkey present in it — an unknown time never
    outranks a known earlier one.
    """
    precedence = precedence or {}
    history = history or {}
    by_print: dict[str, list[str]] = {}
    for hotkey, fingerprint in fingerprints.items():
        if not fingerprint:
            continue
        by_print.setdefault(fingerprint, []).append(hotkey)

    def member_key(fingerprint: str, hotkey: str):
        seen = ((0, history[(fingerprint, hotkey)])
                if (fingerprint, hotkey) in history else (1,))
        committed = ((0, precedence[hotkey])
                     if hotkey in precedence else (1,))
        return (seen, committed, hotkey)

    groups = []
    for fingerprint, hotkeys in sorted(by_print.items()):
        if len(hotkeys) < 2:
            continue
        ordered = sorted(hotkeys, key=lambda hk: member_key(fingerprint, hk))
        groups.append(CopyGroup(
            kind="same_predictions",
            original=ordered[0],
            copies=tuple(ordered[1:]),
            evidence=(f"{len(hotkeys)} miners produced byte-identical "
                      f"predictions (fingerprint {fingerprint[:16]}…)"),
        ))
    return groups


def active_submission(submissions: Iterable[Submission]) -> dict[str, Submission]:
    """{hotkey: the submission it is currently running}.

    The registry resolves a hotkey to its LATEST valid commitment, so that is
    what "currently running" means here too.
    """
    latest: dict[str, Submission] = {}
    for sub in submissions:
        held = latest.get(sub.hotkey)
        if held is None or sub.first_seen_block > held.first_seen_block:
            latest[sub.hotkey] = sub
    return latest


def precedence_map(submissions: Iterable[Submission]) -> dict[str, int]:
    """{hotkey: when this hotkey first committed THE MODEL IT IS RUNNING}.

    PRECEDENCE IS PER MODEL, NOT PER HOTKEY. An earlier version of this took
    each hotkey's earliest commit across everything it had ever submitted,
    which a miner reported as backwards on 2026-08-06, correctly:

        an attacker registered months ago with some junk model outranks the
        author who shipped the good model last week — so the copier is
        crowned the original and the author is labelled the copy.

    Hotkey seniority is not authorship. What earns precedence over a model is
    having committed THAT model first, so the block is looked up against the
    hotkey's active digest and nothing else. Re-committing the same digest
    does not reset it: the earliest time this hotkey published these bytes
    counts, so a miner is not punished for re-pushing their own model.
    """
    active = active_submission(submissions)
    first_for_pair: dict[tuple[str, str], int] = {}
    for sub in submissions:
        key = (sub.hotkey, sub.digest)
        held = first_for_pair.get(key)
        if held is None or sub.first_seen_block < held:
            first_for_pair[key] = sub.first_seen_block
    return {
        hotkey: first_for_pair[(hotkey, sub.digest)]
        for hotkey, sub in active.items()
    }


@dataclass
class DuplicationReport:
    """What a sweep found. Serialisable, so it can be published as evidence
    rather than asserted — a miner accused of copying can check the claim."""
    groups: list[CopyGroup] = field(default_factory=list)

    @property
    def copied_hotkeys(self) -> set[str]:
        return {hk for g in self.groups for hk in g.copies}

    def as_dict(self) -> dict:
        return {
            "groups": [
                {"kind": g.kind, "original": g.original,
                 "copies": list(g.copies), "evidence": g.evidence,
                 "digest": g.digest}
                for g in self.groups
            ],
            "copied_hotkeys": sorted(self.copied_hotkeys),
            "total_groups": len(self.groups),
        }


def find_duplicates(
    submissions: Iterable[Submission],
    fingerprints: Mapping[str, str] | None = None,
    history: Mapping[tuple[str, str], object] | None = None,
) -> DuplicationReport:
    """Both detectors over one population. `history` is the receipts-derived
    behaviour record (`first_seen_fingerprints`); pass it whenever receipts
    exist, because commit order alone mislabels an author who rebuilt."""
    subs = list(submissions)
    groups = digest_collisions(subs)
    if fingerprints:
        groups.extend(prediction_collisions(
            fingerprints, precedence_map(subs), history))
    return DuplicationReport(groups=groups)


# ---------------------------------------------------------------------------
# THE POLICY — ruled 2026-08-06: one payer per model.
#
# "Once per model, using the first submissions. We want a strong but fair
# signal: submit a copy, you won't get paid."
#
# When several hotkeys run the same model, only the one with precedence
# earns; the rest are excluded from the EARNING SET for the day. Nothing
# else changes: standings are untouched (scores are facts), containers keep
# being executed, and the exclusion lapses the moment the miner runs a model
# that is theirs — the next basket under their own model earns normally.
# Applies from switch-on, never retroactively: the flag gates the filter,
# not history.

ONE_PAYER_FLAG_ENV = "SN21_ONE_PAYER_PER_MODEL"
EXEMPT_DIGESTS_ENV = "SN21_COPY_EXEMPT_DIGESTS"


def one_payer_enabled(environ) -> bool:
    return (environ.get(ONE_PAYER_FLAG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def exempt_digests_from(environ) -> frozenset[str]:
    """Digests exempt from the one-payer rule — the reference model.

    Everyone starting from the published reference runs byte-identical code,
    and that is participation, not plagiarism. Without this exemption, day
    one of enforcement would zero every newcomer except whichever of them
    registered first.
    """
    raw = (environ.get(EXEMPT_DIGESTS_ENV) or "").strip()
    return frozenset(d.strip() for d in raw.split(",") if d.strip())


def suppressed_copies(
    report: DuplicationReport,
    exempt_digests: frozenset[str] = frozenset(),
    active_digests: Mapping[str, str] | None = None,
    exempt_hotkeys: frozenset[str] = frozenset(),
) -> set[str]:
    """Hotkeys that do not earn today under one-payer-per-model.

    Per group, everyone but the original. A group is exempt when:

    - the shared digest is exempt (same_digest), or
    - any member is running an exempt digest (same_predictions — a rebuilt
      reference model behaves identically to the reference and must not
      condemn the people running the original), or
    - any member is an exempt HOTKEY. This is how the reference model is
      exempted before its image has ever been pushed digest-pinned: the
      house model runs the reference, so a group containing the house
      hotkey IS the reference behaviour that day. Behaviour-derived, so it
      needs no registry publication to work.
    """
    active_digests = active_digests or {}
    suppressed: set[str] = set()
    for group in report.groups:
        if group.digest and group.digest in exempt_digests:
            continue
        if exempt_hotkeys and any(hk in exempt_hotkeys for hk in group.members):
            continue
        if group.kind == "same_predictions" and any(
            active_digests.get(hk) in exempt_digests for hk in group.members
        ):
            continue
        suppressed.update(group.copies)
    return suppressed


# ---------------------------------------------------------------------------
# BEHAVIOURAL DISTANCE — because byte-equality was trivially evaded.
#
# Reported by a miner on 2026-08-07, and correct. `prediction_fingerprint`
# groups on sha256 of the canonical JSON, and the docstring stated the
# weakness as if it were a strength: two honestly different models "would have
# to agree exactly". The converse is the hole — two IDENTICAL models need only
# disagree in the last decimal to be counted as separate payees. Perturbing a
# clone is a one-line change; the exact-match test cannot see it.
#
# So grouping moves from "are these bytes equal" to "do these models BEHAVE
# the same", measured the way scoring already measures error:
#
#     d(A,B) = mean over shared (episode, horizon, metric) of
#                  |p50_A - p50_B| / max(|actual|, 1.0)
#
# The scale is `max(|actual|, 1.0)`, matching
# hope/scoring/components/quantile_accuracy.py, so a distance is denominated
# in the same units as the error the subnet pays on. Everything it needs —
# predictions verbatim, and the settled actuals — is already in the published
# daily receipt, so any miner can recompute a grouping and contest it. That
# is the point: an accusation nobody can check is not evidence.
#
# TAU IS A PUBLISHED PARAMETER, NOT A CONSTANT OF NATURE. It decides who gets
# paid, so it belongs with the curve numbers under the four-weekly review,
# and the default here is deliberately conservative.

DEFAULT_TAU = 0.02

# Two miners who overlap on a handful of rows can look identical by accident.
# Below this many shared (episode, horizon, metric) rows the answer is "cannot
# tell", and cannot-tell must never cost somebody their earnings.
MIN_OVERLAP_ROWS = 30

P50_KEY = "p50"


def _p50_rows(predictions: Mapping) -> dict:
    """{(episode, horizon, metric): p50} from one miner's predictions."""
    rows = {}
    for episode_id, horizons in (predictions or {}).items():
        for horizon, metrics in (horizons or {}).items():
            for metric, quantiles in (metrics or {}).items():
                if not isinstance(quantiles, Mapping):
                    continue
                value = quantiles.get(P50_KEY)
                if isinstance(value, (int, float)):
                    rows[(str(episode_id), str(horizon), str(metric))] = float(value)
    return rows


def behavioural_distance(pred_a: Mapping, pred_b: Mapping,
                         actuals: Mapping,
                         min_overlap: int = MIN_OVERLAP_ROWS):
    """Mean scaled |p50| gap over shared rows, or None if too little overlap.

    None means "cannot tell", and is deliberately distinct from 0.0 ("these
    are the same model"). A caller must not read the absence of evidence as
    evidence.
    """
    rows_a, rows_b = _p50_rows(pred_a), _p50_rows(pred_b)
    shared = rows_a.keys() & rows_b.keys()
    if len(shared) < min_overlap:
        return None

    total = 0.0
    for key in shared:
        actual = actuals.get(key)
        scale = max(abs(actual), 1.0) if isinstance(actual, (int, float)) else 1.0
        total += abs(rows_a[key] - rows_b[key]) / scale
    return total / len(shared)


def _single_linkage(members: list, close) -> list:
    """Groups where a chain of near-clones collapses together.

    Single linkage on purpose: a clone of a clone is still a clone, and
    requiring every pair in a group to be close would let a ladder of small
    perturbations walk out of any threshold. The cost is chaining — with a
    low tau and honest lineages an order of magnitude further apart, the
    margin absorbs it, but that is exactly why tau is published and reviewed
    rather than tuned quietly.
    """
    parent = {m: m for m in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if close(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

    clusters: dict = {}
    for m in members:
        clusters.setdefault(find(m), []).append(m)
    return [sorted(c) for c in clusters.values() if len(c) > 1]


def distance_collisions(
    predictions_by_miner: Mapping[str, Mapping],
    actuals: Mapping,
    tau: float = DEFAULT_TAU,
    precedence: Mapping[str, int] | None = None,
    history: Mapping[tuple, object] | None = None,
    min_overlap: int = MIN_OVERLAP_ROWS,
) -> list[CopyGroup]:
    """Miners whose predictions are the same model wearing noise.

    Generalises the exact-match detector: byte-identical predictions have
    distance 0 and are caught here too. Precedence inside a group is the
    existing rule — recorded behaviour history first, then commit order.
    """
    miners = sorted(predictions_by_miner)
    if len(miners) < 2:
        return []

    pairwise: dict = {}

    def close(a, b):
        key = (a, b) if a < b else (b, a)
        if key not in pairwise:
            pairwise[key] = behavioural_distance(
                predictions_by_miner[a], predictions_by_miner[b],
                actuals, min_overlap)
        d = pairwise[key]
        return d is not None and d < tau

    groups = []
    for cluster in _single_linkage(miners, close):
        ordered = sorted(cluster, key=lambda hk: (
            (0, history[(hk,)]) if history and (hk,) in history else (1,),
            (0, precedence[hk]) if precedence and hk in precedence else (1,),
            hk,
        ))
        gaps = [d for (a, b), d in pairwise.items()
                if a in cluster and b in cluster and d is not None]
        widest = max(gaps) if gaps else 0.0
        groups.append(CopyGroup(
            kind="same_behaviour",
            original=ordered[0],
            copies=tuple(ordered[1:]),
            evidence=(f"{len(cluster)} miners within tau={tau} behavioural "
                      f"distance (widest pair in group {widest:.4f}); "
                      f"recomputable from the day's receipt"),
        ))
    return groups
