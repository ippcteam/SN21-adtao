#!/usr/bin/env python3
"""Launch rehearsal — drive the whole chain with every launch flag ON.

WHY THIS EXISTS. Every piece of the launch path is unit-tested, but the units
are tested with their OWN flag on and the others off. Launch flips them all at
once, and the failure mode that costs a launch day is the one where each flag
is individually fine and the combination is not. This drives the real
run_daily_loop entrypoint on a temp ledger with the full flag set enabled and
asserts the chain produces a sane, non-empty weight vector.

It also deliberately proves the DANGEROUS case rather than only the happy one:
with the chronic-failure policy enabled, evicting the only placement-eligible
model empties the entire weight vector. reference-v1 is currently the only
model that has ever run in shadow, so on today's subnet that policy can zero
everything. That is a governance decision for Rob, not a defect — and a
rehearsal that only demonstrated the happy path would let it stay invisible.

Self-contained: temp directories, injected providers, no database, no chain,
no network. Exit 0 = rehearsed clean.

    python3 scripts/launch_rehearsal.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from hope.scoring.settle_day_flow import (  # noqa: E402
    SettledHorizon, score_entry, score_entry_v2, settle_scoring_v2_enabled,
)
from hope.validator.daily_loop import run_daily_loop  # noqa: E402

METRICS = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")

# Everything launch turns on, in one place. This IS the cutover flag set.
LAUNCH_FLAGS = {
    "SN21_SETTLE_SCORING_V2": "true",     # spec formula + own-goal basis
    "SN21_SCORING_V2": "true",            # chain-side 4-component scorer
    "SN21_DAILY_STREAM_WEIGHTS": "on",    # daily stream drives weights
    "SN21_TIERED_WEIGHTS": "on",          # tiered allocation
    "SN21_CHRONIC_FAILURE_POLICY": "on",  # liveness enforcement
    "SN21_IM_LAUNCH_DATE": "2026-08-10",  # Rob's date, once he names it
}

EPISODE_COUNT = 300              # > PLACEMENT_FLOOR_PREDICTIONS (250)
SETTLE_DAY = date(2026, 8, 25)   # a settle day inside week 2 of the ladder
SHADOW_DAY = "2026-08-24"
KEY = Ed25519PrivateKey.generate()
FAILS = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def _pred(p50=0.4, spread=0.3):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread} for m in METRICS}


def _write_shadow(root, day, hotkey, eps, ok=True, error=None):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    rec = {"day": day, "hotkey": hotkey,
           "predictions": {e: {"7": _pred(), "14": _pred(), "28": _pred()}
                           for e in eps}, "ok": ok}
    if error:
        rec["error"] = error
    with open(os.path.join(d, f"{hotkey}.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


# A miner needs PLACEMENT_FLOOR_PREDICTIONS (250) SCORED predictions inside the
# 35-day window before it can be placed at all (episode_average.py:111). Seeding
# a couple of episodes produces an empty weight vector that looks like a bug and
# is not, so the rehearsal seeds past the floor deliberately.
EPISODES = tuple(f"ep{i}" for i in range(EPISODE_COUNT))


def _outcomes(day):
    # ALL THREE horizons. Evidence mass is horizon-WEIGHTED, not counted
    # (episode_average.scored_prediction_count sums entry weights, and the
    # blend is 0.20/0.35/0.45). A miner predicting only the 7d horizon
    # accumulates evidence five times slower and needs ~1,250 episodes to
    # reach the 250 floor instead of 250. Worth stating in the miner notice.
    return [SettledHorizon(e, h, 0.4 if i % 2 else -0.3, 0.2, -0.1, day)
            for i, e in enumerate(EPISODES) for h in (7, 14, 28)]


def _load_weights(path):
    """The intended weight vector as actually written to disk."""
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        doc = json.load(f)
    return doc.get("weights") or {}


def rehearse(miners, failing=(), flags=None):
    root = tempfile.mkdtemp(prefix="sn21_rehearsal_")
    try:
        for hk in miners:
            _write_shadow(root, SHADOW_DAY, hk, EPISODES,
                          ok=hk not in failing,
                          error="exit=137: killed" if hk in failing else None)
        return run_daily_loop(
            root, root, SETTLE_DAY,
            outcomes_provider=_outcomes,
            earnings_provider=lambda d: {hk: 100.0 for hk in miners},
            key_loader=lambda: KEY,
            day_volume_provider=lambda d: 400,
            environ=dict(flags if flags is not None else LAUNCH_FLAGS),
        ), root
    finally:
        pass  # caller cleans up


def main():
    print("SN21 LAUNCH REHEARSAL — every flag ON\n")
    print("flag set:")
    for k, v in sorted(LAUNCH_FLAGS.items()):
        print(f"    {k} = {v}")

    # ---- 1. the v2 scorer must actually be ACTIVE, not silently falling back
    print("\n1. the scoring flag genuinely switches the formula")
    check("v2 enabled under the launch flag set",
          settle_scoring_v2_enabled(LAUNCH_FLAGS))
    a = score_entry(_pred(), {m: 0.4 for m in METRICS})
    b = score_entry_v2(_pred(), {m: 0.4 for m in METRICS})
    check("v1 and v2 give different scores (so the flag is observable)",
          a != b, f"v1={a} v2={b}")

    # ---- 2. healthy multi-miner subnet: the chain runs end to end
    print("\n2. healthy subnet, all flags on, full chain")
    summary, root = rehearse(["alpha", "beta", "gamma"])
    try:
        for step in ("settle", "liveness", "capture", "advisory", "publish", "weights"):
            check(f"step '{step}' reported without error",
                  step in summary and "error" not in (summary.get(step) or {}),
                  str(summary.get(step, {}))[:110])
        # The summary reports earning_set_size and a PATH; the vector itself is
        # written to intended_weights_<day>.json. Reading a "weights" key off the
        # summary silently yields {} and reads as a broken subnet — assert on
        # what the loop actually produces, and on the file it actually writes.
        wsum = summary.get("weights") or {}
        w = _load_weights(wsum.get("path"))
        check("earning set is NON-EMPTY", wsum.get("earning_set_size", 0) > 0,
              f"earning_set_size={wsum.get('earning_set_size')}")
        check("intended-weights file holds a real vector", bool(w),
              f"{len(w)} miners, sum={round(sum(w.values()), 6) if w else 0}")
        # Rob's [D7] curve is 50/25/10; normalised over three miners that is
        # 0.588 / 0.294 / 0.118. Assert the SHAPE, not just that a vector
        # exists — a uniform split would also be non-empty and would mean the
        # curve never applied.
        ranked = sorted(w.values(), reverse=True)
        check("weights follow Rob's 50/25/10 curve, normalised",
              len(ranked) == 3
              and abs(ranked[0] - 0.5882) < 0.001
              and abs(ranked[1] - 0.2941) < 0.001
              and abs(ranked[2] - 0.1176) < 0.001,
              " > ".join(str(round(v, 4)) for v in ranked))
        check("no miner was struck on a clean day",
              not (summary.get("liveness") or {}).get("struck"),
              str((summary.get("liveness") or {}).get("struck")))

        # ---- Rob's four verifiability properties (2026-08-05), end to end:
        # the receipt published, the accuracy doc anchored it, and verify_day
        # REPRODUCES every score from the receipt alone. This is a miner's
        # rerun executed inside the launch gate — if reproduction breaks,
        # launch fails here rather than in a miner's Discord complaint.
        rec = summary.get("receipt") or {}
        check("scoring receipt published", bool(rec.get("published")),
              str(rec))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from verify_day import verify_day as _vd
        # expect_anchor is the FEED ROOT — what actually goes on chain. One
        # commitment slot per hotkey means anchoring per-day hashes would erase
        # yesterday's; the rolling root keeps every day verifiable from head.
        v = _vd(root, str(SETTLE_DAY),
                expect_anchor=(summary.get("publish") or {}).get("feed_root"))
        check("verify_day: attestation valid",
              v["checks"]["attestation"]["verdict"] == "PASS")
        check("verify_day: anchor names the receipt",
              v["checks"]["anchor_linkage"]["verdict"] == "PASS",
              str(v["checks"]["anchor_linkage"]))
        check("verify_day: EVERY score reproduced from the receipt",
              v["checks"]["score_reproduction"]["verdict"] == "PASS",
              str(v["checks"]["score_reproduction"]))
        # The top link: this day must be a leaf of the rolling root the
        # validator anchors, and the root the loop computed must be the one
        # the verifier reconstructs. One commitment slot per hotkey means the
        # root is what goes on chain, not the day's own hash.
        check("verify_day: day is inside the anchored feed root",
              v["checks"]["feed_root"]["verdict"] == "PASS",
              str(v["checks"]["feed_root"]))
        check("the loop computed a feed root to anchor",
              bool((summary.get("publish") or {}).get("feed_root")),
              str((summary.get("publish") or {}).get("feed_root")))
        # Rob's dated schedule (2026-08-03) replaced the weekly ramp, so the
        # floor is whatever his sheet says for the settle day — not a week
        # index. Derived from the schedule rather than hardcoded, so the
        # rehearsal cannot drift from the published numbers.
        from hope.scoring.collateral_floor import floor_for_day
        from datetime import date as _date
        floor = summary.get("collateral_floor_alpha")
        # LAUNCH_FLAGS configures SN21_IM_LAUNCH_DATE, which now means the
        # first-live-bundle date and SHIFTS the whole schedule. Compare against
        # the shifted schedule, not the published one — asserting the literal
        # sheet here would fail precisely BECAUSE the shift is working.
        _launch = LAUNCH_FLAGS.get("SN21_IM_LAUNCH_DATE")
        expected = floor_for_day(_date.fromisoformat(summary["day"]),
                                 _date.fromisoformat(_launch) if _launch else None)
        check("alpha floor matches Rob's dated schedule for the settle day",
              floor == expected,
              f"{summary['day']} (launch {_launch}) -> {expected}, got {floor}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ---- 3. THE DANGER CASE, proven not asserted
    print("\n3. single-model subnet + eviction  (Rob's open governance gate)")
    flags = dict(LAUNCH_FLAGS)
    flags["SN21_CHRONIC_STRIKES"] = "1"      # evict on the first failed day
    summary2, root2 = rehearse(["solo"], failing=("solo",), flags=flags)
    try:
        live = summary2.get("liveness") or {}
        w2 = _load_weights((summary2.get("weights") or {}).get("path"))
        check("the sole model was struck for a failed execution day",
              bool(live.get("struck")), str(live.get("struck")))
        # WAS: "EVICTING THE ONLY MODEL EMPTIES THE WEIGHT VECTOR" — asserted
        # the hazard, because at the time it was real and Rob had not ruled.
        # He ruled on 2026-08-03: "We have to not evict all miners - we need at
        # least 1 house model". The floor is now built, so the assertion
        # INVERTS: the sole model must survive its own eviction and keep the
        # vector alive. If this ever goes back to {} the subnet pays nobody and
        # five copying validators propagate it within the hour.
        check("ROB'S FLOOR HOLDS: the only model is not evicted",
              w2 != {} and "solo" in w2,
              f"weights={w2} — the sole model must survive")
        check("the withheld eviction is recorded, not silently skipped",
              bool(live.get("withheld")),
              f"withheld={live.get('withheld')}")
    finally:
        shutil.rmtree(root2, ignore_errors=True)

    # ---- 4. flags OFF must still be a working subnet (rollback path)
    print("\n4. rollback: every launch flag off, chain still runs")
    summary3, root3 = rehearse(["alpha", "beta"], flags={})
    try:
        check("settle still ran with all flags off",
              "error" not in (summary3.get("settle") or {}),
              str(summary3.get("settle"))[:90])
        # No launch date configured no longer means "hold at rung zero" — Rob's
        # sheet carries real calendar dates, so the published schedule runs as
        # miners were shown it.
        from hope.scoring.collateral_floor import floor_for_day as _ffd
        from datetime import date as _d2
        expect3 = _ffd(_d2.fromisoformat(summary3["day"]))
        check("with no launch date set, the published sheet governs",
              summary3.get("collateral_floor_alpha") == expect3,
              f"{summary3['day']} -> sheet says {expect3}, "
              f"got {summary3.get('collateral_floor_alpha')}")
    finally:
        shutil.rmtree(root3, ignore_errors=True)

    print("\n" + "=" * 66)
    if FAILS:
        print(f"REHEARSAL FAILED — {len(FAILS)} check(s): {FAILS}")
        return 1
    print("REHEARSAL CLEAN — the launch flag set works together, the rollback\n"
          "path works, Rob's dated alpha schedule governs, and his floor\n"
          "holds: the last model survives eviction and the withhold is recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
