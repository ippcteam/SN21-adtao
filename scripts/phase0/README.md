# Phase 0 — Testnet Diagnostic Scripts

These scripts empirically resolve the L-confidence open questions from
`docs/verifiable_scoring_architecture.md` §10 before Phase A/B implementation
proceeds.

## What runs here

| Script | Resolves | Time | Cost |
|---|---|---|---|
| `drand_tle_roundtrip.py` | TLE library compatibility with drand quicknet | ~60s wall-clock | $0 (no chain) |
| `q13_fee_measurement.py` | Q13 — actual TAO fee for set_commitment | ~30s | < 0.001 TAO |
| `q11_ratelimit_window.py` | Q11 — RateLimit window of pallet_commitments | 1-4 hours | < 0.1 TAO |

## Prerequisites

1. **Testnet wallet** with a registered hotkey on netuid 21 (or whichever testnet
   subnet ID is being used for SN21 dev). Fund with > 0.5 TAO.
2. **Python deps**: `pip install -e ".[dev]"` plus `pip install timelock`.
3. **Network access**: outbound to `api.drand.sh` (drand HTTP) and the testnet
   subtensor RPC.

## Running

### Drand round-trip (no wallet needed)
```
python scripts/phase0/drand_tle_roundtrip.py
```
Produces stdout PASS/FAIL. Run anytime to confirm TLE primitive.

### Fee measurement
```
python scripts/phase0/q13_fee_measurement.py \
    --wallet-name testnet-validator \
    --wallet-hotkey default \
    --netuid 21
```
Writes `scripts/phase0/results/q13_fee.json`.

### Rate-limit window
```
python scripts/phase0/q11_ratelimit_window.py \
    --wallet-name testnet-validator \
    --wallet-hotkey default \
    --netuid 21
```
Writes `scripts/phase0/results/q11_ratelimit.json`. Long-running.

## Outputs

Results go in `scripts/phase0/results/` (gitignored). Numbers feed into the
architecture doc's §10.1 Q resolution updates.

## Safety

- **TESTNET ONLY.** All scripts default to `network=test`. Mainnet is rejected.
- Each script uses a small footprint per submission (≤32-byte hash) to keep
  costs negligible.
- Q11 may submit ~100 extrinsics; ensure your wallet is funded.
