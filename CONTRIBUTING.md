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

- Imperative mood: "Add feature", not "Added feature".
- First line under 72 characters.
- Reference issue numbers when applicable: `Fix scoreability check (#42)`.

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
2. Update the relevant spec doc (`docs/whitepaper.md`, `docs/SN21_REWARD_MECHANISM.md`, `docs/SN21_EPOCH_STRUCTURE.md`).
3. Land code + tests + docs in the same PR.
4. Tag the PR with `[PROTOCOL]`.

Protocol-affecting PRs need explicit maintainer approval before merge.
