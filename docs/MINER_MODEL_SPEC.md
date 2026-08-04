# SN21 Miner Model Specification — v1 (M0 contract freeze)

**Status:** working draft frozen for build; ratification with the launch amendment.
**Decisions embedded (2026-07-25):** full containers at launch · admission = beat the published naive baseline · budget 1 GB RAM / 15 min per daily basket · ≥7 shadow scored days before weight cutover.

## 1. What you submit
A container image (OCI/Docker). You commit its **digest** on-chain (replaces prediction commitments — same extrinsic, new meaning). Updating your model = new digest = re-enters the backtest gate.

## 2. Execution contract
- Entrypoint reads **one episode payload JSON per line on stdin** (schema: episode payload v2.0 — id, metadata, account_state, pre-window series, action bundle with magnitudes) and writes **one prediction JSON per line on stdout**: `{"episode_id": ..., "horizons": {"7": {...}, "14": {...}, "28": {...}}}` — per horizon: p10/p50/p90 for cost_delta_pct, conversions_delta_pct, efficiency_delta_pct (monotone), plus goal_miss_probability and instability_risk in [0,1].
- **No network.** The sandbox runs `--network=none`. Everything you need ships in the image.
- **Budget: 1 GB RAM, 15 CPU-minutes per daily basket** (~250 episodes). Exceeding either aborts the day's run: no scores that day (the episode-weighted average makes missed days self-penalising; no additional punishment).
- Deterministic output for identical input is strongly recommended (audits replay your container).

## 3. Admission — the backtest gate
On submission, your container runs against a **held-out historical corpus** (episodes with settled outcomes). Admission requires **beating the published naive baseline** (persistence: zero-change medians with corpus-calibrated spreads) on the published gate metric (quantile pinball score blended with direction accuracy, 70/30). Every model update re-runs the gate. Gate results are published.

## 4. Daily cycle (once admitted)
Day 0 changes → Day 1 the subnet executes your container against the day's basket inside the sandbox; outputs are locked as your predictions. Outcomes at 7/14/28 days (+7-day settle) score exactly once; scores fold into your episode-age-weighted standing; weights follow the published curve; the champion changes only under the promotion rule.

## 5. Liveness
Crash/timeout/budget-breach = no scores that day. Chronic failure policy (strikes/eviction) published with the amendment.
