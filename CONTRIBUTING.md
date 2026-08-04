# Contributing to SN21

## Getting Started

1. Fork the repo and clone it locally.
2. Follow the install instructions in [README.md](README.md).
3. Create a feature branch from `main`.

## Branch Naming

```
feat/short-desc    # New features
fix/short-desc     # Bug fixes
docs/short-desc    # Documentation only
```

## Commit Messages

This repo is public and watched by miners, validators, and prospective
contributors. Commit messages are a permanent disclosure surface, so the
standard is stricter than the usual "imperative tense" rule:

**Format:**

```
<type>(<scope>): <subject>

<body — what changed in behavioural terms, and the why>

Refs: <spec doc> §<section>            # for scoring / protocol changes
Effective epoch: <epoch key>           # for scoring / protocol changes
```

- `<type>` is one of `feat`, `fix`, `refactor`, `docs`, `test`, `chore`,
  `security`. Use `security` for any fix touching keys, signatures,
  commit-reveal, or the bonding curve.
- `<scope>` is the top-level area: `miner`, `validator`, `scoring`,
  `protocol`, `commitment`, `archive`, `docs`, `data`, `ci`.
- `<subject>` is imperative, under 72 characters, no trailing period.

**Disclosure rules — the standard for this repo:**

- **Describe behaviour, never values.** Say "tighten the null-detector
  near-zero band" or "switch outcome deltas to fractional units", not
  "lower threshold from 2.0 to 0.02" or "outcomes now range [-1, 1]".
  Specific parameter values, before/after deltas, and threshold numbers
  belong in the spec doc, not in the commit log.
- **No score-gaming hints.** Don't describe how a change benefits a
  particular strategy, how it interacts with another constant, or what
  the optimal response would be.
- **Reference, don't restate.** For any change to scoring or on-chain
  protocol, point to the spec section that governs it and the epoch the
  change becomes effective. The reader should consult the spec for the
  authoritative numbers.
- **No "before / after" math in the body.** "Increases miner reward by
  N%" or "reduces false-positive rate from X to Y" leaks calibration
  data — keep it out.

Examples that pass review:

```
fix(scoring): align null-detector bands with outcome unit convention

The detector thresholds and outcome deltas now use the same units,
so predictions in the documented fractional form no longer trip the
near-zero penalty unexpectedly. Documentation updated.

Refs: docs/SN21_REWARD_MECHANISM.md §4.3
Effective epoch: WR-2026-W18-PUB-E1
```

```
feat(miner): emit predictions in fractional units across baseline and trained models
```

Examples that fail review:

```
fix: lower MIN_INTERVAL_WIDTH from 3.0 to 0.03    # leaks value
fix: scoring change helps miners using XGBoost    # strategy hint
docs: explain that outcomes are -1.0 to +inf      # parameter disclosure
```

Reference issue numbers when applicable: `Fix scoreability check (#42)`.

## Code Standards

### Python

- Type hints on all public functions.
- `ruff check hope/ scripts/ tests/` must pass.
- No bare `print` in library code (`hope/`); use `logging` or return values.
- Tests required for every behaviour change.

### Tests

```bash
pytest tests/                    # Full unit suite
pytest tests/adversarial/ -v     # Adversarial scenarios
ruff check hope/ scripts/ tests/ # Lint
```

CI runs the same on every push to `main`.

## Pull Requests

- One feature or fix per PR.
- Description should say what changed and why.
- Link related issues.
- All CI checks must pass before merge.
- Maintainer review required.

## Protocol Changes

Anything that changes on-chain commit structure, scoring formulas, or miner-visible behaviour:

1. Open an issue first describing the change and rationale.
2. Update the relevant daily-stream doc (`docs/SN21_SCORING.md`, `docs/SN21_REWARDS.md`, `docs/SN21_TRANSITION_PLAN.md`, `docs/SN21_STAKING.md`). The weekly-era specs (REWARD_MECHANISM / EPOCH_STRUCTURE / MINER_ECONOMICS) are archived — do not extend them.
3. Land code + tests + docs in the same PR.
4. Tag the PR with `[PROTOCOL]`.

Protocol-affecting PRs need explicit maintainer approval before merge.
