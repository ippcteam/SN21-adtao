"""Model registry — miners' on-chain model commitments -> the active-model set.

M0 contract: a miner commits their container image digest on-chain (same
metadata extrinsics the prediction era used; new payload meaning). Format:

    sn21-model:v1:<hotkey-owned image digest, sha256:...>

The registry resolves each hotkey's LATEST valid commitment into a
ShadowModel entry; admission status comes from the gate-service verdict
feed, not from the chain (the chain proves WHAT was submitted and WHEN;
the attested verdict feed proves whether it passed).

Pure: chain access injected as a reader fn (hotkey -> [(block, raw_str)]).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from hope.backtest.shadow import ShadowModel

MODEL_COMMIT_PREFIX = "sn21-model:v1:"


def parse_model_commitment(raw: str) -> Optional[str]:
    """Extract the image digest from a commitment string; None if not ours.
    Strict: digest must be sha256:<64 hex> — anything else is ignored (the
    chain is a shared namespace; garbage in commitments must never crash
    registry assembly)."""
    if not isinstance(raw, str) or not raw.startswith(MODEL_COMMIT_PREFIX):
        return None
    digest = raw[len(MODEL_COMMIT_PREFIX):].strip()
    if not digest.startswith("sha256:"):
        return None
    hexpart = digest[7:]
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart.lower()):
        return None
    return digest


def build_registry(hotkeys: Iterable[str],
                   read_commitments: Callable[[str], list[tuple[int, str]]],
                   admitted_digests: set[str],
                   as_of_iso: str) -> dict:
    """Assemble the active-model registry.

    read_commitments: hotkey -> [(block_number, raw_commitment)] (chain reader,
    injected). Latest VALID commitment per hotkey wins (by block). A model
    enters the runnable set only if its digest is in admitted_digests (the
    gate-service verdict feed's admitted set).
    """
    active: dict[str, ShadowModel] = {}
    pending: dict[str, str] = {}
    for hk in hotkeys:
        best: tuple[int, str] | None = None
        for block, raw in read_commitments(hk) or []:
            digest = parse_model_commitment(raw)
            if digest and (best is None or block > best[0]):
                best = (block, digest)
        if best is None:
            continue
        digest = best[1]
        if digest in admitted_digests:
            active[hk] = ShadowModel(hotkey=hk, image_digest=digest,
                                     admitted_at=as_of_iso)
        else:
            pending[hk] = digest
    return {"active": active, "pending_admission": pending,
            "active_count": len(active), "pending_count": len(pending)}
