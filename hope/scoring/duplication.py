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


# A point estimate is the number a model is actually asserting; the interval
# around it is the model's stated uncertainty. Both are scored — but only the
# point estimate has to be identical for two models to be the same model, and
# an interval bound is the cheapest thing in the payload to jitter.
INTERVAL_KEYS = ("p10", "p90")
POINT_KEY = "p50"

# A miner whose point estimates never vary is not evidence of copying: two
# models that both answer "no change" everywhere agree because the question
# was easy, not because one was pulled from the other. Below this many
# DISTINCT point values a miner gets no point-fingerprint at all — the same
# reasoning as refusing to fingerprint a miner with no predictions.
MIN_DISTINCT_POINTS = 8

# The point-estimate test is OFF until switched on, exactly like every other
# control here that decides who is paid. It is not off because it is doubtful
# — it is exact, needs no calibration, and against production receipts it
# reproduces what the calibrated behavioural layer finds — but because
# widening a published rule changes who earns, and that is a ruling, not a
# deploy. Landing it dark means the code can ship, be reviewed and be tested
# on real data before anyone's income moves.
POINT_FINGERPRINT_FLAG_ENV = "SN21_ONE_PAYER_POINT_ESTIMATES"


def point_fingerprints_enabled(environ) -> bool:
    return (environ.get(POINT_FINGERPRINT_FLAG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def point_estimates(prediction) -> object:
    """One prediction reduced to what it ASSERTS, dropping the interval.

    Recurses so the shape does not have to be known in advance: a metric
    carrying {p10, p50, p90} keeps only p50, a bare scalar (goal_miss_
    probability, instability_risk) is kept as-is, and anything unrecognised
    is passed through rather than silently dropped — an unknown field we
    cannot classify is safer compared than ignored.
    """
    if isinstance(prediction, Mapping):
        if POINT_KEY in prediction:
            return prediction[POINT_KEY]
        return {k: point_estimates(v) for k, v in prediction.items()
                if k not in INTERVAL_KEYS}
    if isinstance(prediction, (list, tuple)):
        return [point_estimates(v) for v in prediction]
    return prediction


def _distinct_point_values(reduced: Mapping) -> int:
    """How many different numbers a miner actually asserted across the day."""
    seen = set()

    def walk(node):
        if isinstance(node, Mapping):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        else:
            seen.add(node)

    walk(reduced)
    return len(seen)


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
    return _fingerprints(entries, reduce=False)


def point_fingerprints_from_receipt(entries: Iterable[Mapping]) -> dict[str, str]:
    """{miner: fingerprint} over POINT ESTIMATES only.

    WHY A SECOND FINGERPRINT
        The byte-identical test hashes the whole prediction object, so two
        runs of one model are counted as separate payees the moment they
        differ in any field — including the interval bounds, which are the
        cheapest thing in the payload to perturb. That hole was reported on
        2026-08-07 and left to the behavioural layer, which needs calibrated
        thresholds and is therefore off until an operator sets them.

        This closes it without thresholds. It is still an EXACT test — no
        tolerance, no parameters, nothing to tune wrong — but it compares the
        numbers a model asserts rather than the whole envelope around them.
        Two models agreeing on hundreds of point estimates to full float
        precision are one model; that is not a judgement call, and it needs
        no calibration to say so.

        Deliberately NOT a distance test: this must stay the control that
        works with nothing configured. The behavioural layer still subsumes
        it and remains the real answer for near-identical models.
    """
    return _fingerprints(entries, reduce=True)


def _fingerprints(entries: Iterable[Mapping], reduce: bool) -> dict[str, str]:
    per_miner: dict[str, dict] = {}
    for entry in entries:
        miner = entry.get("miner")
        prediction = entry.get("prediction")
        if not miner or prediction is None:
            continue
        per_miner.setdefault(miner, {}).setdefault(
            str(entry.get("episode_id")), {})[str(entry.get("horizon_days"))] = (
                point_estimates(prediction) if reduce else prediction)

    out: dict[str, str] = {}
    for miner, preds in per_miner.items():
        if not preds:
            continue
        # A flat answer is agreement about an easy question, not evidence of
        # a shared model, so it earns no point-fingerprint. The whole-object
        # fingerprint keeps no such guard because matching every field of a
        # flat prediction byte-for-byte is already conclusive.
        if reduce and _distinct_point_values(preds) < MIN_DISTINCT_POINTS:
            continue
        out[miner] = prediction_fingerprint(preds)
    return out


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
EXEMPT_HOTKEYS_ENV = "SN21_COPY_EXEMPT_HOTKEYS"


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


def exempt_hotkeys_from(environ) -> frozenset[str]:
    """Hotkeys whose behaviour never condemns the group it appears in.

    The digest list above cannot be used everywhere. The weights path groups
    miners from the published receipts, and a receipt entry carries a hotkey
    and a prediction — no digest — so on that path a digest exemption can
    never fire. Naming the hotkey is what makes the published exemption
    ("running the reference unchanged is participation, not plagiarism")
    enforceable where the suppression is actually decided.
    """
    raw = (environ.get(EXEMPT_HOTKEYS_ENV) or "").strip()
    return frozenset(h.strip() for h in raw.split(",") if h.strip())


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
# SCALED DISTANCE — the shared measurement, not a control on its own.
#
# Kept because the multi-metric lineage test below uses it as ONE of its four
# signals. It was briefly wired as a standalone collapse and that was removed:
# a single distance has a single boundary, and a boundary is a target. Any
# caller wanting to group models wants `lineage_collisions`.
#
# Original context, still worth keeping —
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

# NO DEFAULT THRESHOLDS anywhere in this module. Ruled 2026-08-07: "do not
# accept miner-proposed tau/K." The only numbers anyone proposed came from the
# miner who reported the weakness, and their band sits just outside their own
# fleet's spread. Values come from the operator's own red-team calibration —
# see LineageParams, which stays unconfigured, and therefore inert, until all
# of them are set.

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


# ---------------------------------------------------------------------------
# MULTI-METRIC LINEAGE TEST — a single distance was farmable.
#
# Ruled 2026-08-07, and it supersedes the scalar test above as the control.
# Two attacks defeat one number:
#
#   A3  sit just above a published threshold. One scalar has one boundary,
#       and a boundary is a target.
#   A4  chain of near-clones. Single linkage on a scalar merges or splits
#       clusters on purpose, depending on which is convenient.
#
# So a pair counts as one lineage only if EVERY signal agrees. The signals are
# chosen so that noise which defeats one tends to break another, or to wreck
# the score the copier is trying to keep:
#
#   correlation        stays ~1 under output noise; drops hard for an
#                      independent model
#   sign agreement     direction vs zero — cheap to keep, but keeping it
#                      while breaking correlation is hard
#   scaled L1          the distance already implemented; noise breaks it
#                      first and cheapest, which is why it cannot be alone
#   disagreement rate  share of rows moved more than delta — catches "average
#                      looks fine because a few rows moved a lot"
#
# The last one is the trap for the obvious evasion: perturbing a handful of
# rows enough to lift the mean while leaving the model identical everywhere
# else. Mean distance forgives that; a rate does not.

DEFAULT_DISAGREE_DELTA = 0.05


@dataclass(frozen=True)
class LineageParams:
    """Published, versioned, and calibrated by the OPERATOR's red team.

    Every value is None until set, and `configured()` is False until all of
    them are. There is no default set of numbers on purpose — see TAU_UNSET.
    """
    corr_min: float | None = None
    sign_agree_min: float | None = None
    distance_max: float | None = None
    disagree_max: float | None = None
    disagree_delta: float = DEFAULT_DISAGREE_DELTA
    version: str = "unset"

    def configured(self) -> bool:
        return None not in (self.corr_min, self.sign_agree_min,
                            self.distance_max, self.disagree_max)


@dataclass(frozen=True)
class PairSignals:
    """Every number behind one pair verdict, so a receipt can publish it and
    a miner can recompute it."""
    overlap: int
    correlation: float
    sign_agreement: float
    distance: float
    disagreement: float

    def as_dict(self) -> dict:
        return {
            "overlap": self.overlap,
            "correlation": round(self.correlation, 6),
            "sign_agreement": round(self.sign_agreement, 6),
            "distance": round(self.distance, 6),
            "disagreement": round(self.disagreement, 6),
        }


def _pearson(xs, ys) -> float:
    """1.0 for a pair with no variance to disagree about.

    Two flat, identical prediction vectors are maximally similar, but the
    formula is 0/0 there. Returning 0.0 would call two constant clones
    'independent' — the opposite of the truth.
    """
    n = len(xs)
    if n < 2:
        return 1.0
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    num = sum(a * b for a, b in zip(dx, dy))
    denx = sum(a * a for a in dx) ** 0.5
    deny = sum(b * b for b in dy) ** 0.5
    if denx == 0 and deny == 0:
        return 1.0
    if denx == 0 or deny == 0:
        # One side is flat and the other is not: no linear relationship, and
        # crucially not evidence of sameness.
        return 0.0
    return num / (denx * deny)


def pair_signals(pred_a: Mapping, pred_b: Mapping, actuals: Mapping,
                 delta: float = DEFAULT_DISAGREE_DELTA,
                 min_overlap: int = MIN_OVERLAP_ROWS) -> PairSignals | None:
    """All four signals for one pair, or None when the overlap is too thin."""
    rows_a, rows_b = _p50_rows(pred_a), _p50_rows(pred_b)
    shared = sorted(rows_a.keys() & rows_b.keys())
    if len(shared) < min_overlap:
        return None

    xs = [rows_a[k] for k in shared]
    ys = [rows_b[k] for k in shared]

    scaled = []
    for key, x, y in zip(shared, xs, ys):
        actual = actuals.get(key)
        scale = max(abs(actual), 1.0) if isinstance(actual, (int, float)) else 1.0
        scaled.append(abs(x - y) / scale)

    agree = sum(1 for x, y in zip(xs, ys)
                if (x > 0) == (y > 0) or (x == 0 and y == 0))

    return PairSignals(
        overlap=len(shared),
        correlation=_pearson(xs, ys),
        sign_agreement=agree / len(shared),
        distance=sum(scaled) / len(scaled),
        disagreement=sum(1 for d in scaled if d > delta) / len(scaled),
    )


def same_lineage(signals: PairSignals | None, params: LineageParams) -> bool:
    """AND of every signal. Any one of them clearing the pair is enough to
    call it independent, which is the direction that protects miners."""
    if signals is None or not params.configured():
        return False
    return (signals.correlation >= params.corr_min
            and signals.sign_agreement >= params.sign_agree_min
            and signals.distance <= params.distance_max
            and signals.disagreement <= params.disagree_max)


def _components(members: list, linked) -> list:
    parent = {m: m for m in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if linked(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    out: dict = {}
    for m in members:
        out.setdefault(find(m), []).append(m)
    return [sorted(c) for c in out.values() if len(c) > 1]


def lineage_collisions(
    predictions_by_miner: Mapping[str, Mapping],
    actuals: Mapping,
    params: LineageParams,
    precedence: Mapping[str, int] | None = None,
    require_medoid_edge: bool = True,
    min_overlap: int = MIN_OVERLAP_ROWS,
) -> tuple[list[CopyGroup], dict]:
    """Components of one lineage, plus the pairwise evidence behind them.

    `require_medoid_edge` cuts the chain attack (A4): after components form,
    each member must also be same-lineage with the component's MEDOID — the
    member most similar to the rest — not merely with one neighbour. A ladder
    of small steps therefore cannot drag distant models into one group, and
    anyone dropped by that second test is split back out.
    """
    if not params.configured():
        return [], {}

    miners = sorted(predictions_by_miner)
    if len(miners) < 2:
        return [], {}

    cache: dict = {}

    def signals(a, b):
        key = (a, b) if a < b else (b, a)
        if key not in cache:
            cache[key] = pair_signals(predictions_by_miner[a],
                                      predictions_by_miner[b], actuals,
                                      params.disagree_delta, min_overlap)
        return cache[key]

    def linked(a, b):
        return same_lineage(signals(a, b), params)

    groups = []
    for component in _components(miners, linked):
        members = component
        if require_medoid_edge and len(members) > 2:
            # The medoid is the member with the highest mean correlation to
            # the rest — the centre of the cluster rather than an endpoint.
            # `pool` is bound explicitly: `members` is rebound just below, and
            # a closure over it would read the NEW value the moment this is
            # ever called lazily.
            def affinity(m, pool=tuple(members)):
                vals = [signals(m, o).correlation for o in pool
                        if o != m and signals(m, o) is not None]
                return sum(vals) / len(vals) if vals else -1.0

            medoid = max(members, key=affinity)
            members = [medoid] + [m for m in members
                                  if m != medoid and linked(m, medoid)]
            if len(members) < 2:
                continue

        ordered = sorted(members, key=lambda hk: (
            (0, precedence[hk]) if precedence and hk in precedence else (1,),
            hk,
        ))
        worst = max((signals(a, b).distance for a in members for b in members
                     if a < b and signals(a, b) is not None), default=0.0)
        groups.append(CopyGroup(
            kind="same_lineage",
            original=ordered[0],
            copies=tuple(ordered[1:]),
            evidence=(f"{len(members)} hotkeys of one lineage under "
                      f"params@{params.version} (widest scaled distance "
                      f"{worst:.4f}); every pair signal recomputable from the "
                      f"day's receipt"),
        ))

    audit = {f"{a}|{b}": s.as_dict() for (a, b), s in cache.items()
             if s is not None}
    return groups, audit
