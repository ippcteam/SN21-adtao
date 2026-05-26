"""Registration-status endpoint — miner pre-flight oracle.

Miners hit this before doing any work to learn whether their hotkey
appears in the validator's loaded registration index. The answer is
authoritative when ``gate_enabled`` is true; when false, the validator
has no reg-index loaded and submissions are not gated at all, so the
miner should treat that as "cannot verify, proceed at own risk".

Auth is required — the response is keyed off the authenticated hotkey,
not a query parameter, so this endpoint can't be used to enumerate
which hotkeys are registered.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from hope.validator.api.auth import MinerIdentity, verify_miner

router = APIRouter()


@router.get("/registration-status")
async def registration_status(
    request: Request,
    miner: MinerIdentity = Depends(verify_miner),
):
    """Return whether the authenticated hotkey has a verified
    sn21-reg-v1 binding on chain.

    Response shape:
      - registered:   bool — true when the hotkey is in the loaded index
      - gate_enabled: bool — true when the validator has a reg-index path
                              configured (POST /predictions enforces)
      - hint:         str  — actionable text for the miner to display

    Miners should treat ``gate_enabled=false`` as "cannot verify";
    treat ``gate_enabled=true && registered=false`` as a hard fail.
    """
    state = request.app.state.validator
    registered_set = state.get("registered_ed25519_hotkeys") or set()
    gate_enabled = bool(state.get("_reg_index_path"))
    is_registered = miner.hotkey in registered_set

    if not gate_enabled:
        hint = (
            "Validator has no registration index configured; submission "
            "gate is open. Verify your sn21-reg-v1 binding manually."
        )
    elif is_registered:
        hint = "Hotkey is registered — predictions will be accepted."
    else:
        hint = (
            "Hotkey is NOT in the registration index. Run "
            "`python scripts/sn21_keys.py register --role miner` ONCE, "
            "then re-check. See docs/miner_quickstart.md for details."
        )

    return {
        "hotkey": miner.hotkey[:16] + "...",
        "registered": is_registered,
        "gate_enabled": gate_enabled,
        "hint": hint,
    }
