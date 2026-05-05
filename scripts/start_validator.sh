#!/bin/bash
# ============================================================================
# OPERATOR-ONLY deployment script. Public miners do NOT need this file.
# Used by the operator's Render web-service to provision the validator
# container at boot. If you're running a miner, see docs/miner_quickstart.md.
# ============================================================================
#
# Startup script for Render — deploys wallet and starts validator WITH chain.
#
# Required env vars:
#   WALLET_NAME        — Bittensor wallet name
#   HOTKEY_NAME        — hotkey name (default: validator)
#   HOTKEY_B64         — base64-encoded hotkey file
#   COLDKEYPUB_B64     — base64-encoded coldkeypub.txt
#   RELEASE_KEY        — current release key
#   NETUID             — subnet netuid
#   BT_NETWORK         — test or finney
#   PORT               — HTTP port (default: 8080)
#   BURN_FRACTION      — burn rate (default: 0.95)
#   HOPE_API_KEY       — data API key
#   HOPE_API_URL       — data API base URL

set -e

# Validate required env vars
if [ -z "$WALLET_NAME" ]; then
    echo "ERROR: WALLET_NAME is required"
    exit 1
fi
if [ -z "$RELEASE_KEY" ]; then
    echo "ERROR: RELEASE_KEY is required"
    exit 1
fi
if [ -z "$NETUID" ]; then
    echo "ERROR: NETUID is required"
    exit 1
fi

WALLET_DIR="$HOME/.bittensor/wallets/$WALLET_NAME"
HOTKEY="${HOTKEY_NAME:-validator}"

# Deploy hotkey
if [ -n "$HOTKEY_B64" ]; then
    echo "Deploying hotkey to $WALLET_DIR/hotkeys/$HOTKEY"
    mkdir -p "$WALLET_DIR/hotkeys"
    echo "$HOTKEY_B64" | base64 -d > "$WALLET_DIR/hotkeys/$HOTKEY"
    chmod 600 "$WALLET_DIR/hotkeys/$HOTKEY"
    echo "Hotkey deployed"
else
    echo "WARNING: HOTKEY_B64 not set — running without hotkey (--no-chain)"
fi

# Deploy coldkeypub (public only — not the private key)
if [ -n "$COLDKEYPUB_B64" ]; then
    echo "Deploying coldkeypub to $WALLET_DIR/coldkeypub.txt"
    echo "$COLDKEYPUB_B64" | base64 -d > "$WALLET_DIR/coldkeypub.txt"
    chmod 644 "$WALLET_DIR/coldkeypub.txt"
    echo "Coldkeypub deployed"
fi

# Determine if we can run with chain
if [ -n "$HOTKEY_B64" ] && [ -n "$COLDKEYPUB_B64" ]; then
    echo "Starting validator WITH chain (network=${BT_NETWORK:-test}, netuid=$NETUID)"
    exec hope-validator \
        --release "$RELEASE_KEY" \
        --port "${PORT:-8080}" \
        --network "${BT_NETWORK:-test}" \
        --netuid "$NETUID" \
        --wallet-name "$WALLET_NAME" \
        --wallet-hotkey "$HOTKEY" \
        --burn "${BURN_FRACTION:-0.95}"
else
    echo "Starting validator WITHOUT chain (--no-chain)"
    exec hope-validator \
        --release "$RELEASE_KEY" \
        --port "${PORT:-8080}" \
        --no-chain
fi
