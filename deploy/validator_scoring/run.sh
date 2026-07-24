#!/usr/bin/env bash
# SN21 scoring-runner wrapper — reference implementation.
#
# Generic env-driven script that any operator running the canonical scoring
# code can use as the entrypoint for a scheduled job (Render Cron Job,
# systemd timer, Kubernetes CronJob, GitHub Actions cron, etc.).
#
# It does two things:
#   1. If RELEASE_KEY is not set, discover the latest release from the
#      operator data backend.
#   2. Invoke `hope-validator` with the right flags. The scoring runner is
#      idempotent — running multiple times in the scoring window is safe
#      (it checks `validator_already_scored_epoch` on chain and bails).
#
# All operator-specific values come from environment variables — never
# hardcode hostnames, wallet names, or keys here.

set -euo pipefail

# ---------------------------------------------------------------------------
# Required env
# ---------------------------------------------------------------------------
: "${HOPE_API_KEY:?HOPE_API_KEY must be set (operator data API key)}"
: "${WALLET_NAME:?WALLET_NAME must be set (Bittensor wallet name)}"
: "${HOTKEY_NAME:?HOTKEY_NAME must be set (Bittensor hotkey name)}"
: "${BT_NETWORK:?BT_NETWORK must be set (finney for mainnet, test for testnet)}"
: "${NETUID:?NETUID must be set (21 for mainnet, 466 for testnet)}"
: "${ED25519_KEY_FILE:?ED25519_KEY_FILE must be set (PEM path for inner_sig)}"

# ---------------------------------------------------------------------------
# Optional env (with sane defaults)
# ---------------------------------------------------------------------------
HOPE_API_URL="${HOPE_API_URL:-https://hope-ads-backend.example.com}"
ARCHIVE_TIER_1="${ARCHIVE_TIER_1:-}"
ARCHIVE_TIER_2="${ARCHIVE_TIER_2:-}"
BLOCKS_UNTIL_PRE_REVEAL="${BLOCKS_UNTIL_PRE_REVEAL:-300}"
BLOCKS_UNTIL_POST_REVEAL="${BLOCKS_UNTIL_POST_REVEAL:-600}"
BLOCKS_UNTIL_WEIGHTS_REVEAL="${BLOCKS_UNTIL_WEIGHTS_REVEAL:-360}"

# ---------------------------------------------------------------------------
# Pre-flight: confirm dependencies are on PATH
# ---------------------------------------------------------------------------
command -v hope-validator >/dev/null 2>&1 || {
    echo "ERROR: hope-validator CLI not on PATH. Install via 'pip install -e .'" >&2
    exit 1
}
command -v jq >/dev/null 2>&1 || {
    echo "ERROR: jq is required for release discovery. Install jq (apt/brew/etc)." >&2
    exit 1
}
[[ -r "${ED25519_KEY_FILE}" ]] || {
    echo "ERROR: ED25519_KEY_FILE not readable: ${ED25519_KEY_FILE}" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Release discovery
# ---------------------------------------------------------------------------
# If RELEASE_KEY is set in env, score that release. Otherwise query the
# operator backend for the latest release and score that. Discovery is
# pinned to the wire shape documented in hope/validator/data_client.py.
if [[ -z "${RELEASE_KEY:-}" ]]; then
    echo "RELEASE_KEY not set — discovering latest release from ${HOPE_API_URL}"
    RELEASE_KEY=$(
        curl -fsS \
            -H "X-API-Key: ${HOPE_API_KEY}" \
            "${HOPE_API_URL}/internal/bittensor/v1/releases" \
            | jq -er '.releases | sort_by(.created_at) | reverse | .[0].release_key'
    )
fi

echo "Scoring release: ${RELEASE_KEY}"
echo "Network:         ${BT_NETWORK} (netuid ${NETUID})"
echo "Wallet:          ${WALLET_NAME}/${HOTKEY_NAME}"
echo "Tier-1 archive:  ${ARCHIVE_TIER_1:-<none>}"
echo "Tier-2 archive:  ${ARCHIVE_TIER_2:-<none>}"

# ---------------------------------------------------------------------------
# Build the hope-validator invocation
# ---------------------------------------------------------------------------
ARGS=(
    --release "${RELEASE_KEY}"
    --network "${BT_NETWORK}"
    --netuid "${NETUID}"
    --wallet-name "${WALLET_NAME}"
    --wallet-hotkey "${HOTKEY_NAME}"
    --ed25519-key-file "${ED25519_KEY_FILE}"
    --blocks-until-pre-reveal "${BLOCKS_UNTIL_PRE_REVEAL}"
    --blocks-until-post-reveal "${BLOCKS_UNTIL_POST_REVEAL}"
    --blocks-until-weights-reveal "${BLOCKS_UNTIL_WEIGHTS_REVEAL}"
)

# Archive endpoints are repeatable; pass each non-empty entry as a separate
# --archive-tier-N flag. Multiple URLs can be supplied via space-separated
# env values (e.g. `ARCHIVE_TIER_2="https://a.example.com https://b.example.com"`).
if [[ -n "${ARCHIVE_TIER_1}" ]]; then
    for url in ${ARCHIVE_TIER_1}; do
        ARGS+=(--archive-tier-1 "${url}")
    done
fi
if [[ -n "${ARCHIVE_TIER_2}" ]]; then
    for url in ${ARCHIVE_TIER_2}; do
        ARGS+=(--archive-tier-2 "${url}")
    done
fi

# Hand control to the runner. exec replaces the shell so signals + exit
# codes propagate cleanly to the scheduler.
exec hope-validator "${ARGS[@]}"
