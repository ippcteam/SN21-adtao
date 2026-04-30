#!/bin/bash
# Startup script for Render — deploys wallet and starts validator WITH chain.
#
# Required env vars:
#   WALLET_NAME        — e.g. sn21-testnet-1
#   HOTKEY_NAME        — e.g. validator
#   HOTKEY_B64         — base64-encoded hotkey file
#   COLDKEYPUB_B64     — base64-encoded coldkeypub.txt
#   RELEASE_KEY        — e.g. WR-2026-W18-PUB-E1
#   NETUID             — e.g. 466
#   BT_NETWORK         — test or finney
#   PORT               — e.g. 8080
#   BURN_FRACTION      — e.g. 0.95

set -e

WALLET_DIR="$HOME/.bittensor/wallets/${WALLET_NAME:-sn21-testnet-1}"
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
    echo "Starting validator WITH chain (network=${BT_NETWORK:-test}, netuid=${NETUID:-466})"
    exec hope-validator \
        --release "${RELEASE_KEY:-WR-2026-W18-PUB-E1}" \
        --port "${PORT:-8080}" \
        --network "${BT_NETWORK:-test}" \
        --netuid "${NETUID:-466}" \
        --wallet-name "${WALLET_NAME:-sn21-testnet-1}" \
        --wallet-hotkey "${HOTKEY}" \
        --burn "${BURN_FRACTION:-0.95}"
else
    echo "Starting validator WITHOUT chain (--no-chain)"
    exec hope-validator \
        --release "${RELEASE_KEY:-WR-2026-W18-PUB-E1}" \
        --port "${PORT:-8080}" \
        --no-chain
fi
