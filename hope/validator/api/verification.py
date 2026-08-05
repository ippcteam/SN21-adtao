"""Verification endpoints — post-scoring reveal for transparency.

The `/verification` endpoint reads the validator's 9.C.2 post-scoring
artifacts directly from chain ``RevealedCommitments`` (via a short-lived
subprocess to keep the long-running daemon's memory flat). The 9.C.2
plaintext is chain-public once auto-decrypted, so the endpoint surfaces
exactly the data any chain explorer would see — no per-miner score
breakdowns, no privileged calculations: just the cryptographic
commitments (final_score_root, scoring_hash, weights_commit_block_hash)
that a third-party verifier needs to reproduce the run locally with
`scripts/verify_epoch.py`.

The legacy `/scores` and `/my-predictions` endpoints rely on in-memory state
that was populated only in the now-superseded single-binary architecture.
They remain 501 — deliberately, and not as unfinished work.

They are WEEKLY-epoch endpoints (`epoch_id`-scoped), and the subnet now runs a
DAILY stream. Resurrecting them to serve daily data would mean answering an
epoch-shaped question with day-shaped data, which is how eras get conflated in
a miner's head. The daily equivalents are real, public and receipt-backed:

    GET /v1/daily/{day}/scores          aggregate, no hotkeys (IA D-05)
    GET /v1/daily/{day}/miner/{hotkey}  one miner's entries + components
    scripts/verify_day.py --url ...     reproduce every score yourself

The 501 bodies now name those routes. Previously they pointed only at
verify_epoch.py — a weekly-only tool, and therefore a dead end for exactly the
miner most likely to be asking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys

from fastapi import APIRouter, Depends, HTTPException, Request

from hope.validator.api.auth import MinerIdentity, verify_miner

logger = logging.getLogger(__name__)
router = APIRouter()

# Subprocess time budget. Chain reads should complete in <10s on a healthy
# RPC; 30s gives generous headroom against websocket reconnection latency.
_VERIFICATION_SUBPROCESS_TIMEOUT_SECS = 30


@router.get("/{epoch_id}/verification")
async def get_verification(epoch_id: str, request: Request):
    """Return the validator's 9.C.2 post-scoring artifact for an epoch.

    Reads ``Commitments.RevealedCommitments`` for the validator's hotkey
    via a short-lived subprocess and locates the 9.C.2 plaintext whose
    ``epoch_id`` field matches. Returns the chain-public fields verbatim
    (bytes hex-encoded); the response is identical to what any chain
    explorer would surface for the same query, so this endpoint adds no
    privileged data — only convenience.

    Response codes:
      200 — 9.C.2 found and returned
      404 — wallet hotkey / netuid not configured on this daemon
      409 — chain has not yet auto-decrypted the 9.C.2 commit for this
            epoch (TLE'd, awaiting drand reveal_round + subnet epoch
            tick). Retry in a few minutes.
      503 — chain RPC unreachable or subprocess failure
    """
    state = request.app.state.validator
    cfg = state.get("_chain_config", {})
    validator_ss58 = state.get("validator_hotkey_ss58")
    netuid = cfg.get("netuid")
    network = cfg.get("network")

    if cfg.get("no_chain"):
        raise HTTPException(
            status_code=503,
            detail="Validator is running with --no-chain; verification unavailable",
        )
    if not validator_ss58 or netuid is None or not network:
        raise HTTPException(
            status_code=404,
            detail="Validator hotkey / netuid not configured on this daemon",
        )

    cmd = [
        sys.executable, "-m", "hope.validator.chain_verification_dump",
        "--network", network,
        "--netuid", str(netuid),
        "--validator-hotkey", validator_ss58,
        "--epoch-id", epoch_id,
    ]
    # Wrap the synchronous subprocess in asyncio.to_thread so the FastAPI
    # event loop continues serving other requests while the chain read is
    # in flight. Without this, a flood of /verification polls froze the
    # whole daemon for up to _VERIFICATION_SUBPROCESS_TIMEOUT_SECS per
    # call — every other request (including /health) timed out at the platform's
    # load-balancer ceiling.
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd, capture_output=True, text=True,
            timeout=_VERIFICATION_SUBPROCESS_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Chain read timed out after {_VERIFICATION_SUBPROCESS_TIMEOUT_SECS}s; "
                "RPC may be slow or unreachable. Retry shortly."
            ),
        )
    except Exception as e:
        logger.warning("verification subprocess failed to start: %s", e)
        raise HTTPException(status_code=503, detail=f"Chain read subprocess failed: {e}")

    if result.returncode != 0:
        logger.warning(
            "verification subprocess exit %d: %s",
            result.returncode, result.stderr.strip()[:300],
        )
        raise HTTPException(
            status_code=503,
            detail="Chain read failed; retry shortly",
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=503,
            detail="Chain read returned unparseable output",
        )

    if not payload.get("found"):
        n_revealed = payload.get("n_revealed", 0)
        raise HTTPException(
            status_code=409,
            detail=(
                f"9.C.2 post-scoring artifact for epoch_id={epoch_id} is not "
                f"yet visible in RevealedCommitments for this validator "
                f"({n_revealed} commits revealed so far). The chain has "
                f"committed the artifact but not yet auto-decrypted it "
                f"(TLE-encrypted, awaiting the drand reveal_round + the next "
                f"subnet epoch tick). Retry in a few minutes."
            ),
        )

    return {
        "epoch_id": epoch_id,
        "status": "revealed",
        "source": "chain.RevealedCommitments",
        "artifact": payload["data"],
        "verification_instructions": (
            "This artifact contains the cryptographic commitments needed to "
            "independently verify the validator's scoring run. To verify your "
            "own miner score:  (1) fetch the operator's 9.A.2 outcomes for "
            "this epoch_id;  (2) re-decrypt your own AES_ct using the K "
            "revealed in your 9.B bundle commit;  (3) re-run the SN21 scoring "
            "code (see scripts/verify_epoch.py in the SN21-adtao public repo) "
            "with the revealed outcomes + your decrypted predictions;  (4) "
            "check that your locally-computed score is consistent with the "
            "`final_score_root` field above (it's the IMT root over all "
            "scored miners; your Merkle inclusion proof can be reconstructed "
            "locally from the same inputs)."
        ),
    }


@router.get("/{epoch_id}/scores")
async def get_scores(epoch_id: str, request: Request):
    """Deprecated under the two-binary architecture.

    The previous implementation read per-miner score breakdowns from an
    in-memory dict that was only populated when scoring ran in the same
    Python process as the API daemon. Under the current two-binary split
    (`hope-validator-api` + `hope-validator`), the scoring cron does not
    write back to the daemon's memory, so this endpoint cannot serve the
    granular per-component breakdowns it used to without re-introducing
    cross-process state.

    The chain-derived `/verification` endpoint provides the publicly-
    verifiable scoring commitment (final_score_root + Merkle proof
    reconstruction); per-miner breakdowns are no longer surfaced via
    this API.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "error": f"GET /{epoch_id}/scores is not served under the "
                     f"two-binary validator architecture",
            "era": "This is a WEEKLY-epoch endpoint. The weekly era concluded "
                   "3 Aug 2026; the subnet now runs a daily stream.",
            "use_instead": {
                "daily_aggregate": "GET /v1/daily/{YYYY-MM-DD}/scores",
                "daily_per_miner": "GET /v1/daily/{YYYY-MM-DD}/miner/{hotkey}",
                "reproduce_it_yourself": "scripts/verify_day.py --url <this "
                                         "validator> --day YYYY-MM-DD",
            },
            "weekly_history": f"GET /{epoch_id}/verification for the "
                              f"chain-derived commitment; scripts/verify_epoch.py "
                              f"reproduces weekly-era per-miner scores.",
        },
    )


@router.get("/{epoch_id}/my-predictions")
async def get_my_prediction_proof(
    epoch_id: str,
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Deprecated under the two-binary architecture.

    Originally returned a miner's per-prediction receipts + Merkle
    inclusion proofs from in-memory state. The two-binary split means
    that state isn't populated; rebuilding it from chain would require
    re-decrypting the miner's own 9.B bundle + reconstructing the
    Merkle tree locally — both of which the miner can already do
    themselves with the data in /verification. Returning 501 with
    guidance rather than synthesising a partial response.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "error": f"GET /{epoch_id}/my-predictions is not served under the "
                     f"two-binary validator architecture",
            "era": "This is a WEEKLY-epoch endpoint. The weekly era concluded "
                   "3 Aug 2026; the subnet now runs a daily stream.",
            "use_instead": {
                "your_entries": "GET /v1/daily/{YYYY-MM-DD}/miner/{hotkey} — "
                                "your predictions verbatim, their score "
                                "components, and the receipt sha256",
                "reproduce_it_yourself": "scripts/verify_day.py --url <this "
                                         "validator> --day YYYY-MM-DD "
                                         "--miner <hotkey>",
            },
            "weekly_history": "Reconstruct locally from your 9.B bundle on "
                              "chain + the operator's 9.A.2 outcomes + "
                              "final_score_root from /verification. See "
                              "scripts/verify_epoch.py.",
        },
    )
