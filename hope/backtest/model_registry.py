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


# The chain Raw field caps at 128 bytes (on_chain.RAW_FIELD_MAX_BYTES) —
# with the prefix + "@sha256:" + 64 hex, repo paths must stay <= ~42 chars.
# Published in the miner spec (deep-study 2026-07-30).
_REPO_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789./-_:")


def parse_model_commitment(raw: str) -> Optional[dict]:
    """Parse a model commitment; None if not ours or malformed.

    Format (deep-study 2026-07-30 — matches the system's own prediction-path
    precedent of committing LOCATION + FINGERPRINT in one bundle, cf.
    hope/miner/onchain_submitter's {K, sha256, url} single commit):

        sn21-model:v1:<repo>@sha256:<64hex>     -> {"image_ref", "digest"}
        sn21-model:v1:sha256:<64hex>            -> {"image_ref": None, "digest"}
                                                   (legacy digest-only; the
                                                   intake needs an out-of-band
                                                   repo — deprecated at launch)

    Strict on miner-controlled input: digest must be sha256:<64 hex>, repo
    must be lowercase-registry grammar with no tags/@/whitespace — garbage in
    the shared chain namespace must never crash registry assembly."""
    if not isinstance(raw, str) or not raw.startswith(MODEL_COMMIT_PREFIX):
        return None
    body = raw[len(MODEL_COMMIT_PREFIX):].strip()

    def _valid_digest(d: str) -> bool:
        if not d.startswith("sha256:"):
            return False
        hexpart = d[7:]
        return len(hexpart) == 64 and all(
            c in "0123456789abcdef" for c in hexpart.lower())

    if "@" in body:
        repo, _, digest = body.partition("@")
        if not repo or len(repo) > 100 or not _valid_digest(digest):
            return None
        if repo != repo.lower() or any(c not in _REPO_ALLOWED for c in repo):
            return None
        if repo.startswith(("-", ".", "/")) or ":" in repo.split("/")[-1]:
            return None  # no leading junk; no tag on the name segment
        return {"image_ref": repo, "digest": digest}

    if _valid_digest(body):
        return {"image_ref": None, "digest": body}
    return None


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
        best: tuple[int, dict] | None = None
        for block, raw in read_commitments(hk) or []:
            parsed = parse_model_commitment(raw)
            if parsed and (best is None or block > best[0]):
                best = (block, parsed)
        if best is None:
            continue
        parsed = best[1]
        digest = parsed["digest"]
        # image_digest carries the RUNNABLE identity: the pinned ref when the
        # bundle format supplied a repo (repo@sha256:...), else the bare
        # digest (legacy, needs out-of-band repo — deprecated at launch).
        runnable = (f"{parsed['image_ref']}@{digest}"
                    if parsed.get("image_ref") else digest)
        if digest in admitted_digests:
            active[hk] = ShadowModel(hotkey=hk, image_digest=runnable,
                                     admitted_at=as_of_iso)
        else:
            pending[hk] = digest
    return {"active": active, "pending_admission": pending,
            "active_count": len(active), "pending_count": len(pending)}
