"""D7 — emission weight curve: steep but continuous, no winner-take-all cliff.

Design v0.5 §7 [D7]: weight follows the moving-average standing through a
published curve — the top model takes roughly 50-60%, second place ~20%, a
decaying tail after that, and zero below a published score threshold. A
crossover shifts weight gradually as the gap widens; there is nothing
discontinuous to flip. Expected earning set 5-15 models, hard ceiling 20
per basket.

All numeric parameters are REVIEW-SET ([D7] "curve numbers at first
review") — the defaults here are the v0.5 prose values, wired so the
published config can restate them without code change.

Pure module: consumes {miner: standing} (the D13 average), returns
normalized weights. Champion PROMOTION is deliberately not here — that is
the [D8] rule; the curve pays, the promotion rule decides who runs live.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CurveParams:
    """Published curve parameters (restated at four-weekly reviews).

    Rob 2026-07-28 ("I want people to stay in the game"): fixed shares
    50/25/10 for the top three ranks, then a decreasing geometric tail;
    hard cap 20. Replaces the working default 55/20/geometric-from-2nd.
    """
    score_threshold: float = 0.0   # zero weight below this standing ([D7]); review-set
    top_share: float = 0.50        # champion (Rob 2026-07-28)
    second_share: float = 0.25     # second place
    third_share: float = 0.10      # third place — fixed, tail starts AFTER it
    tail_decay: float = 0.5        # each further rank gets this × previous share
    max_earners: int = 20          # hard ceiling per basket ([D7])


DEFAULT_PARAMS = CurveParams()


def curve_weights(standings: dict[str, float],
                  params: CurveParams = DEFAULT_PARAMS) -> dict[str, float]:
    """Rank-based curve over standings → normalized weight vector.

    - miners at/below the threshold (or with no standing) get exactly 0
    - ranks beyond max_earners get exactly 0 (safety ceiling)
    - remaining shares: top_share, second_share, then geometric tail
    - re-normalized over the actual earning set so weights sum to 1.0
      (fewer earners than curve slots → shares scale up proportionally,
      keeping the RATIOS of the published curve, which is the published
      object; the absolute top share grows only when the field is thin)
    - deterministic tie-break on (standing desc, miner id asc) so equal
      standings cannot produce nondeterministic weight vectors
    """
    eligible = sorted(
        ((m, s) for m, s in standings.items()
         if s is not None and s > params.score_threshold),
        key=lambda kv: (-kv[1], kv[0]),
    )[: params.max_earners]

    if not eligible:
        return {m: 0.0 for m in standings}

    raw_shares = []
    for rank in range(len(eligible)):
        if rank == 0:
            raw_shares.append(params.top_share)
        elif rank == 1:
            raw_shares.append(params.second_share)
        elif rank == 2:
            raw_shares.append(params.third_share)
        else:
            raw_shares.append(raw_shares[-1] * params.tail_decay)

    total = sum(raw_shares)
    weights = {m: 0.0 for m in standings}
    for (miner, _), share in zip(eligible, raw_shares):
        weights[miner] = share / total
    return weights


def earning_set(standings: dict[str, float],
                params: CurveParams = DEFAULT_PARAMS) -> list[str]:
    """Miners with non-zero weight under the curve, best first."""
    w = curve_weights(standings, params)
    return [m for m, v in sorted(w.items(), key=lambda kv: -kv[1]) if v > 0]
