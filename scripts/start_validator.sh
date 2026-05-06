#!/bin/bash
# ============================================================================
# OPERATOR-ONLY boot script. Public miners do NOT need this file — see
# docs/miner_quickstart.md instead.
#
# Used by the operator's Render worker to deploy the wallet from base64
# env vars and run one on-chain validator epoch (Layer 9.B–9.C).
#
# This file ships with the public repo for now and will move to a private
# deploy repo once that's wired up. The build / start commands here are
# exercised only by the operator's Render service.
# ============================================================================
#
# Required env vars (set in Render dashboard):
#   WALLET_NAME           Bittensor wallet name
#   RELEASE_KEY           Current epoch / release key
#   HOTKEY_B64            Base64-encoded hotkey file
#   COLDKEYPUB_B64        Base64-encoded coldkeypub.txt
#   HOPE_API_KEY          Operator data API key
#   HOPE_API_URL          Operator data API base URL
#   ED25519_KEY_B64       Base64-encoded ed25519 PEM private key for inner_sig
#                         (separate from the Bittensor hotkey, which is
#                         sr25519). Generate with `python scripts/sn21_keys.py
#                         generate --role validator --output validator.pem`
#                         then `base64 -i validator.pem | tr -d '\n'`.
#
# Optional:
#   ARCHIVE_TIER_2_URLS   Comma-separated Tier-2 archive base URLs (operator
#                         redundancy; miners' Tier-3 URLs from chain are the
#                         primary fetch source)
#
# Optional (defaults from render.yaml):
#   HOTKEY_NAME           Hotkey name (default: default)
#   NETUID                Subnet netuid — MUST match BT_NETWORK
#                         (mainnet finney → 21, testnet test → 466)
#   BT_NETWORK            Bittensor network (finney for mainnet, test for testnet)

set -e

require() {
    local name="$1"
    if [ -z "${!name}" ]; then
        echo "ERROR: $name is required"
        exit 1
    fi
}

require WALLET_NAME
require RELEASE_KEY
require HOTKEY_B64
require COLDKEYPUB_B64
require HOPE_API_KEY
require HOPE_API_URL
require ED25519_KEY_B64

# Defensive sanity-check: catch the easy-to-make network/netuid mismatch
# (mainnet finney = netuid 21, testnet test = netuid 466) before bittensor
# emits a confusing "Subnet mechanism N.0 does not exist" error.
NET="${BT_NETWORK:-finney}"
NID="${NETUID:-21}"
if [ "$NET" = "finney" ] && [ "$NID" != "21" ]; then
    echo "ERROR: BT_NETWORK=finney expects NETUID=21 (mainnet); got NETUID=$NID."
    echo "       For testnet, set BT_NETWORK=test (and NETUID=466)."
    exit 1
fi
if [ "$NET" = "test" ] && [ "$NID" != "466" ]; then
    echo "ERROR: BT_NETWORK=test expects NETUID=466 (testnet); got NETUID=$NID."
    exit 1
fi

WALLET_DIR="$HOME/.bittensor/wallets/$WALLET_NAME"
HOTKEY="${HOTKEY_NAME:-default}"

echo "Deploying wallet $WALLET_NAME / $HOTKEY"
mkdir -p "$WALLET_DIR/hotkeys"
echo "$HOTKEY_B64" | base64 -d > "$WALLET_DIR/hotkeys/$HOTKEY"
chmod 600 "$WALLET_DIR/hotkeys/$HOTKEY"
echo "$COLDKEYPUB_B64" | base64 -d > "$WALLET_DIR/coldkeypub.txt"
chmod 644 "$WALLET_DIR/coldkeypub.txt"

# Deploy the ed25519 PEM key for inner_sig (separate from the sr25519 hotkey)
ED25519_KEY_FILE="$HOME/.sn21/keys/validator.pem"
mkdir -p "$(dirname "$ED25519_KEY_FILE")"
echo "$ED25519_KEY_B64" | base64 -d > "$ED25519_KEY_FILE"
chmod 600 "$ED25519_KEY_FILE"

# Build --archive-tier-2 flags from the comma-separated env var (optional —
# miners' Tier-3 URLs from chain are the primary fetch source).
TIER2_ARGS=()
if [ -n "$ARCHIVE_TIER_2_URLS" ]; then
    IFS=',' read -ra URLS <<< "$ARCHIVE_TIER_2_URLS"
    for url in "${URLS[@]}"; do
        url="${url// /}"  # trim whitespace
        TIER2_ARGS+=(--archive-tier-2 "$url")
    done
fi

# --------------------------------------------------------------------------
# Boot order:
#   1. Start hope-validator-api (FastAPI episode server) in the foreground.
#      Binds $PORT, satisfies Render's port-binding requirement, and exposes
#      /v1/epochs/{id}/episodes etc. to miners through the deadline window.
#   2. The on-chain scoring runner (hope-validator) is a separate per-epoch
#      process that runs after the deadline. It's not invoked here — wire it
#      via a Render Cron Job or a manual trigger when ready.
# --------------------------------------------------------------------------
echo "Starting episode API: release=$RELEASE_KEY network=$NET netuid=$NID port=${PORT:-8080}"
exec hope-validator-api \
    --release "$RELEASE_KEY" \
    --network "$NET" \
    --netuid "$NID" \
    --wallet-name "$WALLET_NAME" \
    --wallet-hotkey "$HOTKEY"
