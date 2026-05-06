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
#   ARCHIVE_TIER_2_URLS   Comma-separated Tier-2 archive base URLs
#
# Optional (defaults from render.yaml):
#   HOTKEY_NAME           Hotkey name (default: default)
#   NETUID                Subnet netuid (default: 21)
#   BT_NETWORK            Bittensor network (default: finney)

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
require ARCHIVE_TIER_2_URLS

WALLET_DIR="$HOME/.bittensor/wallets/$WALLET_NAME"
HOTKEY="${HOTKEY_NAME:-default}"

echo "Deploying wallet $WALLET_NAME / $HOTKEY"
mkdir -p "$WALLET_DIR/hotkeys"
echo "$HOTKEY_B64" | base64 -d > "$WALLET_DIR/hotkeys/$HOTKEY"
chmod 600 "$WALLET_DIR/hotkeys/$HOTKEY"
echo "$COLDKEYPUB_B64" | base64 -d > "$WALLET_DIR/coldkeypub.txt"
chmod 644 "$WALLET_DIR/coldkeypub.txt"

# Build --archive-tier-2 flags from the comma-separated env var
TIER2_ARGS=()
IFS=',' read -ra URLS <<< "$ARCHIVE_TIER_2_URLS"
for url in "${URLS[@]}"; do
    url="${url// /}"  # trim whitespace
    TIER2_ARGS+=(--archive-tier-2 "$url")
done

echo "Running validator: release=$RELEASE_KEY network=${BT_NETWORK:-finney} netuid=${NETUID:-21}"
exec hope-validator \
    --release "$RELEASE_KEY" \
    --network "${BT_NETWORK:-finney}" \
    --netuid "${NETUID:-21}" \
    --wallet-name "$WALLET_NAME" \
    --wallet-hotkey "$HOTKEY" \
    "${TIER2_ARGS[@]}"
