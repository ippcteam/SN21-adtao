"""Operator copy of a released model — docs/SN21_MODEL_PRIVACY.md.

When a model is released it is copied, byte for byte, to a public repository
the operator controls, so the release cannot be undone by the miner deleting
or closing their own repository. The copy keeps the committed digest: the
manifest is pushed exactly as fetched, so `mirror@sha256:<committed>` is the
same image anyone can verify against the chain.

Every blob is checked against its digest on the way through (the puller
refuses anything else), and nothing is unpacked or run. Blobs the mirror
already holds are skipped, so a re-run after a failure only moves what is
missing. An image index is copied with every platform it names: a registry
may refuse an index whose children are missing, and dropping platforms would
change nothing about what was released.

Pure network code with no scheduling: the daily step that decides WHAT to
copy lives in release_step.py.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse

from hope.backtest.oci_pull import _INDEX_TYPES, _MANIFEST_ACCEPT, PullError, _Registry, parse_ref

_CHUNK = 1 << 20


def _blobs_of(manifest: dict) -> list[str]:
    out = []
    cfg = (manifest.get("config") or {}).get("digest")
    if cfg:
        out.append(cfg)
    for layer in manifest.get("layers") or []:
        if layer.get("digest"):
            out.append(layer["digest"])
    return out


def mirror_tag(digest: str) -> str:
    """A tag for the copy, so registries that hide untagged images still list
    it. Derived from the digest alone, so it is stable and collision-free."""
    return digest.replace(":", "-")


class _Target:
    def __init__(self, ref: str, credentials=None):
        host, repo = parse_ref(ref)
        self.reg = _Registry(host, repo, credentials=credentials, actions="pull,push")

    def has_blob(self, digest: str) -> bool:
        status, _, _ = self.reg.request("HEAD", f"{self.reg.base}/blobs/{digest}")
        return status == 200

    def has_manifest(self, digest: str) -> bool:
        status, _, _ = self.reg.request(
            "HEAD", f"{self.reg.base}/manifests/{digest}",
            headers={"Accept": _MANIFEST_ACCEPT})   # */* gets a 404 for an index
        return status == 200

    def put_blob(self, digest: str, path: str) -> None:
        status, headers, body = self.reg.request("POST", f"{self.reg.base}/blobs/uploads/", body=b"")
        if status not in (202, 201):
            raise PullError(f"mirror upload start -> {status}: {body[:200]!r}")
        location = headers.get("Location") or ""
        if not location:
            raise PullError("mirror upload start gave no location")
        if location.startswith("/"):
            location = f"https://{self.reg.registry}{location}"
        sep = "&" if "?" in location else "?"
        url = f"{location}{sep}digest={urllib.parse.quote(digest)}"
        # Streamed from disk: a layer can be gigabytes and the daily run's
        # memory is the constraint. If the token expires on the way, the
        # request is retried once and sends the file again from the start.
        with open(path, "rb") as f:
            status, _, body = self.reg.request(
                "PUT", url, body=f,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(os.path.getsize(path))})
        if status not in (201, 204):
            raise PullError(f"mirror blob {digest} -> {status}: {body[:200]!r}")

    def put_manifest(self, reference: str, body: bytes, media_type: str) -> None:
        status, _, resp = self.reg.request(
            "PUT", f"{self.reg.base}/manifests/{reference}", body=body,
            headers={"Content-Type": media_type})
        if status not in (201, 204):
            raise PullError(f"mirror manifest {reference} -> {status}: {resp[:200]!r}")


def _copy_blob(src: _Registry, dst: _Target, digest: str, work: str) -> bool:
    if dst.has_blob(digest):
        return False
    path = os.path.join(work, digest.replace(":", "_"))
    src.blob(digest, path)                     # verified against the digest
    try:
        dst.put_blob(digest, path)
    finally:
        os.unlink(path)
    return True


def mirror_image(source_ref: str, digest: str, target_ref: str,
                 source_credentials=None, target_credentials=None,
                 work_dir: str | None = None) -> dict:
    """Copy `source_ref@digest` to `target_ref@digest` (plus its digest tag).

    Returns {"target": "<target_ref>@<digest>", "blobs_copied", "manifests",
    "already": bool}. Raises PullError on any failure; nothing partial is
    reported as a copy, and a re-run picks up where it stopped.
    """
    host, repo = parse_ref(source_ref)
    src = _Registry(host, repo, credentials=source_credentials)
    dst = _Target(target_ref, credentials=target_credentials)
    target = f"{target_ref}@{digest}"
    if dst.has_manifest(digest):
        return {"target": target, "blobs_copied": 0, "manifests": 0, "already": True}

    top, top_type = src.manifest_bytes(digest)          # verified against the digest
    top_json = json.loads(top)
    children: list[tuple[str, bytes, str]] = []
    if top_type in _INDEX_TYPES or "manifests" in top_json:
        for entry in top_json.get("manifests") or []:
            child_digest = entry.get("digest")
            if not child_digest:
                raise PullError("index entry without a digest")
            body, media_type = src.manifest_bytes(child_digest)
            children.append((child_digest, body, media_type or entry.get("mediaType") or ""))
    else:
        children = []

    copied = 0
    with tempfile.TemporaryDirectory(dir=work_dir) as work:
        manifests = children if children else [(digest, top, top_type)]
        for child_digest, body, _ in manifests:
            for blob in _blobs_of(json.loads(body)):
                copied += _copy_blob(src, dst, blob, work)
        for child_digest, body, media_type in children:
            dst.put_manifest(child_digest, body, media_type)
        dst.put_manifest(digest, top, top_type)
        dst.put_manifest(mirror_tag(digest), top, top_type)

    return {"target": target, "blobs_copied": copied,
            "manifests": len(children) + 1, "already": False}
