#!/bin/bash
# Daily shadow-day convenience: runs today's basket through the reference
# model onto the attested ledger. Operator-triggered until the validator
# host (docker-capable) takes over this duty at M3-ops handover.
# Usage: ./scripts/shadow_daily.sh   (idempotent: shadow ledger appends per-day files)
KEY="BD-$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d 'yesterday' +%Y-%m-%d)"
cd "$(dirname "$0")/.." || exit 1
python3 scripts/run_shadow_day_bd.py "$KEY" --ledger-root ./sn21_ledger
