"""Type-weighted scoring — weight table model, computation, and the gate.

WHY (2026-09-01). The per-prediction score is identical arithmetic for every
change type, so a model that is genuinely better on hard changes earns no
more than one that is good at easy ones. The observable result: 118 models
inside a 0.14 band, the top 12 inside 0.01, and a leader that cannot be
displaced by performance. Weighting entries by change type is the fix — and
the weights are DERIVED FROM MEASUREMENT, not chosen:

    weight(family) ∝ headroom(family)

where headroom is how far the best models pull away from the median model on
that family (p90 − p50 of per-miner mean scores). This deliberately measures
SEPARABILITY, not raw difficulty: a family where nobody can predict the
outcome (pure noise) shows low headroom and gets no extra weight — weighting
raw difficulty would turn the score into a lottery on unpredictable types.

GUARDS, each doing one job:
  * min_entries / min_miners — a rare family stays at weight exactly 1.0
    until there is enough evidence; three lucky episodes must never outrank
    five hundred consistent ones.
  * floor / cap — no family is worth less than half or more than three times
    another, so the standings cannot be captured by a single family.
  * frequency normalisation — weights are scaled so the frequency-weighted
    mean multiplier is 1.0, keeping the overall standing scale comparable
    before/after.
  * trailing window, frozen table — weights come from a fixed window and are
    published as a versioned table; they change at reviews, not daily, so
    miners have a stable, checkable target.

GOVERNANCE GATE. This changes who is paid, so it is a rule change and ships
through a published amendment (the copy-detection route). The code enforces
the process: `load_table_for_scoring` refuses any table whose status is not
"ratified" — a draft table can be built, modelled, and reviewed, but cannot
score. Application is prospective by construction: entry weights are written
at entry time into an append-only ledger, so enabling the table never
re-prices an already-entered result (v0.5 §4: nothing is ever rescored).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Mapping

from hope.reporting.type_accuracy import type_family

PARAMS_VERSION = "type-weights-v1"

STATUS_DRAFT = "draft"
STATUS_RATIFIED = "ratified"

DEFAULT_MIN_ENTRIES = 500      # entries a family needs before it can weigh
DEFAULT_MIN_MINERS = 12        # distinct qualified miners it needs
DEFAULT_MINER_MIN_N = 20       # entries a miner needs to qualify on a family
DEFAULT_FLOOR = 0.5
DEFAULT_CAP = 3.0


@dataclass(frozen=True)
class FamilyStat:
    """The measured row behind one family's weight — published beside it so
    the weight is checkable, never asserted."""
    n_entries: int
    freq_share: float
    field_mean: float
    miners_qualified: int
    headroom: float | None      # None when below the evidence gates
    weight: float


@dataclass
class TypeWeightTable:
    params_version: str = PARAMS_VERSION
    status: str = STATUS_DRAFT
    window_start: str = ""
    window_end: str = ""
    min_entries: int = DEFAULT_MIN_ENTRIES
    min_miners: int = DEFAULT_MIN_MINERS
    miner_min_n: int = DEFAULT_MINER_MIN_N
    floor: float = DEFAULT_FLOOR
    cap: float = DEFAULT_CAP
    families: dict = field(default_factory=dict)   # family -> FamilyStat

    # -- application ------------------------------------------------------
    def weight_for(self, transition_key: str | None) -> float:
        """Multiplier for one entry. UNKNOWN and unlisted families are
        neutral — an unlabelled entry must never be advantaged or punished
        for the label being missing."""
        fam = type_family(transition_key)
        stat = self.families.get(fam)
        return stat.weight if stat is not None else 1.0

    # -- (de)serialisation ------------------------------------------------
    def to_json(self) -> dict:
        return {
            "params_version": self.params_version,
            "status": self.status,
            "window": {"start": self.window_start, "end": self.window_end},
            "gates": {"min_entries": self.min_entries,
                      "min_miners": self.min_miners,
                      "miner_min_n": self.miner_min_n},
            "bounds": {"floor": self.floor, "cap": self.cap},
            "families": {
                f: {"n_entries": s.n_entries,
                    "freq_share": round(s.freq_share, 6),
                    "field_mean": round(s.field_mean, 6),
                    "miners_qualified": s.miners_qualified,
                    "headroom": (round(s.headroom, 6)
                                 if s.headroom is not None else None),
                    "weight": round(s.weight, 6)}
                for f, s in sorted(self.families.items())
            },
        }

    @classmethod
    def from_json(cls, d: Mapping) -> "TypeWeightTable":
        t = cls(
            params_version=str(d.get("params_version") or ""),
            status=str(d.get("status") or STATUS_DRAFT),
            window_start=str((d.get("window") or {}).get("start") or ""),
            window_end=str((d.get("window") or {}).get("end") or ""),
        )
        gates = d.get("gates") or {}
        bounds = d.get("bounds") or {}
        t.min_entries = int(gates.get("min_entries", DEFAULT_MIN_ENTRIES))
        t.min_miners = int(gates.get("min_miners", DEFAULT_MIN_MINERS))
        t.miner_min_n = int(gates.get("miner_min_n", DEFAULT_MINER_MIN_N))
        t.floor = float(bounds.get("floor", DEFAULT_FLOOR))
        t.cap = float(bounds.get("cap", DEFAULT_CAP))
        for fam, s in (d.get("families") or {}).items():
            t.families[str(fam)] = FamilyStat(
                n_entries=int(s.get("n_entries", 0)),
                freq_share=float(s.get("freq_share", 0.0)),
                field_mean=float(s.get("field_mean", 0.0)),
                miners_qualified=int(s.get("miners_qualified", 0)),
                headroom=(float(s["headroom"])
                          if s.get("headroom") is not None else None),
                weight=float(s.get("weight", 1.0)),
            )
        return t

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_json(), fh, sort_keys=True, indent=1)
        os.replace(tmp, path)


def compute_table(
    entries: Iterable[tuple[str, str | None, float]],
    *,
    window_start: str = "",
    window_end: str = "",
    min_entries: int = DEFAULT_MIN_ENTRIES,
    min_miners: int = DEFAULT_MIN_MINERS,
    miner_min_n: int = DEFAULT_MINER_MIN_N,
    floor: float = DEFAULT_FLOOR,
    cap: float = DEFAULT_CAP,
) -> TypeWeightTable:
    """Measure (miner, transition_key, score) entries into a DRAFT table.

    Deterministic for a given input set: same entries -> byte-identical
    table. The output is always status=draft — ratification is a human act
    recorded by editing the status, never something computation grants.
    """
    per_family: dict[str, dict[str, list[float]]] = {}
    for miner, tkey, score in entries:
        fam = type_family(tkey)
        per_family.setdefault(fam, {}).setdefault(str(miner), []).append(
            float(score))

    total_n = sum(len(v) for fams in per_family.values()
                  for v in fams.values())

    # measure
    measured: dict[str, dict] = {}
    for fam, miners in per_family.items():
        scores_all = [s for v in miners.values() for s in v]
        qual_means = sorted(
            sum(v) / len(v) for v in miners.values() if len(v) >= miner_min_n)
        head = None
        if (len(scores_all) >= min_entries
                and len(qual_means) >= min_miners):
            # p90 − p50 of qualified miner means: how far the best pull away
            # from the middle. Index arithmetic, no interpolation — stable
            # under small-n changes.
            p90 = qual_means[min(len(qual_means) - 1,
                                 int(round(0.90 * (len(qual_means) - 1))))]
            p50 = qual_means[int(round(0.50 * (len(qual_means) - 1)))]
            head = max(0.0, p90 - p50)
        measured[fam] = {
            "n": len(scores_all),
            "mean": (sum(scores_all) / len(scores_all)) if scores_all else 0.0,
            "miners": len(qual_means),
            "headroom": head,
        }

    eligible = {f: m for f, m in measured.items() if m["headroom"] is not None}
    heads = [m["headroom"] for m in eligible.values()]
    med = median(heads) if heads else 0.0

    raw: dict[str, float] = {}
    for fam, m in measured.items():
        if fam in eligible and med > 0:
            raw[fam] = min(cap, max(floor, m["headroom"] / med))
        else:
            raw[fam] = 1.0   # neutral below the gates — never zero

    # frequency normalisation over ALL entries so the standing scale holds
    mass = sum(measured[f]["n"] * raw[f] for f in measured)
    scale = (total_n / mass) if mass > 0 else 1.0

    table = TypeWeightTable(
        status=STATUS_DRAFT,
        window_start=window_start, window_end=window_end,
        min_entries=min_entries, min_miners=min_miners,
        miner_min_n=miner_min_n, floor=floor, cap=cap,
    )
    for fam, m in measured.items():
        w = raw[fam] * scale if fam in eligible else 1.0
        table.families[fam] = FamilyStat(
            n_entries=m["n"],
            freq_share=(m["n"] / total_n) if total_n else 0.0,
            field_mean=m["mean"],
            miners_qualified=m["miners"],
            headroom=m["headroom"],
            weight=w,
        )
    return table


def load_table_for_scoring(path: str) -> TypeWeightTable:
    """Load a table for LIVE scoring. Refuses anything but a ratified table
    of the current params version — the process gate, enforced in code.

    A draft can be built, modelled and reviewed; putting it on the scoring
    path without ratification is exactly the shortcut this raises on.
    """
    with open(path) as fh:
        table = TypeWeightTable.from_json(json.load(fh))
    if table.status != STATUS_RATIFIED:
        raise ValueError(
            f"type-weight table at {path} has status={table.status!r}; only a "
            f"ratified table may score (publish the amendment first)")
    if table.params_version != PARAMS_VERSION:
        raise ValueError(
            f"type-weight table params_version={table.params_version!r} does "
            f"not match this code ({PARAMS_VERSION}); refusing to guess")
    return table
