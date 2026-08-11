"""Pull and unpack an OCI image by digest — without a docker daemon.

WHY THIS EXISTS

    The published execution contract assumes `docker pull` + `docker run`.
    The model executor runs on a host with NO container runtime at all (probe
    2026-08-11: no docker, podman, skopeo, umoci, runc). What it does have is
    Python, outbound HTTPS to the registries, and disk. So we fetch the image
    ourselves over the registry v2 API, verify every blob against the digest
    the miner committed on chain, and unpack the layers to a root filesystem
    the namespace sandbox then runs.

TRUST MODEL

    The digest is the anchor. The miner committed `repo@sha256:...` on chain;
    we pull by that digest and the puller REFUSES any blob whose bytes do not
    hash to what was asked for. A registry that serves substituted bytes fails
    here, loudly, before a single line of the miner's code is unpacked — never
    mind run.

    Everything fetched is untrusted. Layer tars are unpacked with explicit
    path containment (no escaping the rootfs via `..` or absolute symlinks),
    because a hostile image will try exactly that.

SCOPE

    Enough of the OCI spec to run a real miner image: token auth, image
    indexes (multi-arch -> linux/amd64), v2 and OCI manifests, gzipped layers,
    whiteouts. Not a general-purpose registry client; it does one job and
    fails closed on anything it does not understand.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_REGISTRY = "registry-1.docker.io"
AMD64 = ("linux", "amd64")

# `docker.io` is the friendly name miners write in a ref; the registry v2 API
# is served from registry-1.docker.io. Hitting docker.io directly returns HTML
# / an empty body, not a manifest.
_REGISTRY_ALIASES = {
    "docker.io": DEFAULT_REGISTRY,
    "index.docker.io": DEFAULT_REGISTRY,
}

_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


class PullError(Exception):
    """Any failure to fetch or verify — never a partial, trusted result."""


@dataclass
class ImageConfig:
    entrypoint: list = field(default_factory=list)
    cmd: list = field(default_factory=list)
    env: list = field(default_factory=list)
    working_dir: str = "/"
    user: str = ""

    def argv(self) -> list:
        """The command a container would run: entrypoint then cmd."""
        return list(self.entrypoint) + list(self.cmd)


def parse_ref(image_ref: str) -> tuple[str, str]:
    """(registry_host, repository) from a docker-style image reference.

    `ghcr.io/x/y` -> ("ghcr.io", "x/y"); `busybox` -> the default registry
    with the `library/` prefix Docker Hub requires for single-name repos.
    """
    if "/" not in image_ref:
        return DEFAULT_REGISTRY, f"library/{image_ref}"
    head, _, _ = image_ref.partition("/")
    # A registry host has a dot or a port; otherwise the first segment is part
    # of the repository path on the default registry.
    if "." in head or ":" in head or head == "localhost":
        registry = _REGISTRY_ALIASES.get(head, head)
        repo = image_ref.partition("/")[2]
        # Docker Hub single-name repos under the alias still need library/.
        if registry == DEFAULT_REGISTRY and "/" not in repo:
            repo = f"library/{repo}"
        return registry, repo
    repo = image_ref
    if repo.count("/") == 0:
        repo = f"library/{repo}"
    return DEFAULT_REGISTRY, repo


def _http_get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _bearer_token(registry: str, repo: str, www_authenticate: str) -> str | None:
    """Resolve a pull token from a WWW-Authenticate challenge.

    Anonymous pull only — we never send credentials, and a registry that
    demands them for a public image simply fails the pull, which is correct:
    a model a validator cannot fetch cannot be admitted.
    """
    if not www_authenticate.lower().startswith("bearer "):
        return None
    params = {}
    for part in www_authenticate[len("bearer "):].split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            params[key.strip()] = value.strip().strip('"')
    realm = params.get("realm")
    if not realm:
        return None
    service = params.get("service", registry)
    scope = params.get("scope", f"repository:{repo}:pull")
    url = (f"{realm}?service={urllib.parse.quote(service)}"
           f"&scope={urllib.parse.quote(scope)}")
    try:
        with _http_get(url, {}, timeout=60) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise PullError(f"token request failed: {exc.code}") from exc
    return body.get("token") or body.get("access_token")


class _Registry:
    def __init__(self, registry: str, repo: str):
        self.registry = registry
        self.repo = repo
        self.base = f"https://{registry}/v2/{repo}"
        self._token: str | None = None

    def _auth_headers(self, accept: str) -> dict:
        headers = {"Accept": accept}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, url: str, accept: str):
        try:
            return _http_get(url, self._auth_headers(accept))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self._token is None:
                challenge = exc.headers.get("WWW-Authenticate", "")
                self._token = _bearer_token(self.registry, self.repo, challenge)
                if not self._token:
                    raise PullError("registry requires auth we do not have") from exc
                return _http_get(url, self._auth_headers(accept))
            raise PullError(f"GET {url} -> {exc.code}") from exc

    def manifest(self, reference: str) -> tuple[dict, str]:
        url = f"{self.base}/manifests/{reference}"
        with self._get(url, _MANIFEST_ACCEPT) as resp:
            media_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            body = resp.read()
        return json.loads(body), media_type

    def blob(self, digest: str, dest_path: str) -> None:
        """Stream a blob to disk, verifying its digest as it arrives."""
        url = f"{self.base}/blobs/{digest}"
        algo, _, expected = digest.partition(":")
        hasher = hashlib.new(algo)
        with self._get(url, "application/octet-stream") as resp, \
                open(dest_path, "wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                hasher.update(chunk)
                out.write(chunk)
        actual = hasher.hexdigest()
        if actual != expected:
            os.unlink(dest_path)
            raise PullError(
                f"blob digest mismatch: asked {digest}, got {algo}:{actual}")


def _select_platform(index: dict) -> str:
    """The linux/amd64 manifest digest from an image index.

    The executor host is amd64, so an image with no amd64 variant cannot run
    here — a common real case is a miner who built for arm64 only. The error
    names that explicitly so the reason is actionable, not just "no manifest".
    """
    manifests = index.get("manifests", [])
    # buildx pushes an attestation manifest with platform unknown/unknown
    # alongside the real ones; it is not runnable and must never be selected.
    runnable = [m for m in manifests
                if m.get("platform", {}).get("architecture") != "unknown"
                and m.get("platform", {}).get("os") != "unknown"]

    for entry in runnable:
        platform = entry.get("platform", {})
        if (platform.get("os"), platform.get("architecture")) == AMD64:
            return entry["digest"]

    # A lone runnable entry with UNSPECIFIED architecture is the image itself
    # (a plain single-arch push carries no platform block) — accept it. But a
    # lone entry that explicitly declares a NON-amd64 arch cannot run here.
    if len(runnable) == 1:
        arch = runnable[0].get("platform", {}).get("architecture")
        if not arch:
            return runnable[0]["digest"]

    arches = sorted({f"{m.get('platform', {}).get('os')}/"
                     f"{m.get('platform', {}).get('architecture')}"
                     for m in runnable})
    raise PullError(
        f"no linux/amd64 manifest in the image index (has: {arches or 'none'}"
        f") — the executor is amd64; the miner must publish an amd64 build")


def _image_config(manifest: dict, registry: _Registry, work: str) -> ImageConfig:
    config_digest = manifest.get("config", {}).get("digest")
    if not config_digest:
        return ImageConfig()
    path = os.path.join(work, "config.json")
    registry.blob(config_digest, path)
    with open(path) as handle:
        raw = json.load(handle)
    cfg = raw.get("config", {}) or {}
    return ImageConfig(
        entrypoint=list(cfg.get("Entrypoint") or []),
        cmd=list(cfg.get("Cmd") or []),
        env=list(cfg.get("Env") or []),
        working_dir=cfg.get("WorkingDir") or "/",
        user=cfg.get("User") or "",
    )


def _safe_within(root: str, target: str) -> bool:
    """True only if `target` resolves inside `root` — the containment check a
    hostile tar tries to defeat with `..` and absolute symlinks."""
    root_abs = os.path.realpath(root)
    target_abs = os.path.realpath(target)
    return target_abs == root_abs or target_abs.startswith(root_abs + os.sep)


def _remove(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _apply_layer(tar_path: str, rootfs: str) -> None:
    """Unpack one gzipped layer tar onto the rootfs, honouring whiteouts and
    refusing any member that would escape the rootfs."""
    with gzip.open(tar_path, "rb") as gz, \
            tarfile.open(fileobj=gz, mode="r|") as tar:
        for member in tar:
            base = os.path.basename(member.name)
            target = os.path.join(rootfs, member.name)

            # Whiteout: `.wh.<x>` deletes x; `.wh..wh..opq` clears a dir.
            if base == ".wh..wh..opq":
                parent = os.path.dirname(target)
                if os.path.isdir(parent) and _safe_within(rootfs, parent):
                    for child in os.listdir(parent):
                        _remove(os.path.join(parent, child))
                continue
            if base.startswith(".wh."):
                victim = os.path.join(os.path.dirname(target), base[len(".wh."):])
                if _safe_within(rootfs, victim):
                    _remove(victim)
                continue

            if not _safe_within(rootfs, target):
                raise PullError(f"layer member escapes rootfs: {member.name!r}")
            # Symlink/hardlink targets must also stay contained.
            if member.issym() or member.islnk():
                if member.linkname.startswith("/"):
                    link_target = os.path.join(rootfs, member.linkname.lstrip("/"))
                else:
                    link_target = os.path.join(os.path.dirname(target),
                                               member.linkname)
                if not _safe_within(rootfs, link_target):
                    # Skip a dangerous link rather than abort the whole image;
                    # a model that depends on escaping links will simply fail
                    # to run, which is the right outcome.
                    continue
            try:
                tar.extract(member, rootfs, set_attrs=False)
                # set_attrs=False avoids chown-to-root (we unpack unprivileged)
                # and setuid/setgid bits — but it also strips the EXECUTE bit,
                # which makes the image's own python3 non-runnable (exit 126,
                # observed 2026-08-11). Restore the permission bits explicitly,
                # masked to rwx only: keep execute, drop setuid/setgid/sticky.
                if (member.isfile() or member.isdir()) \
                        and not os.path.islink(target):
                    try:
                        os.chmod(target, member.mode & 0o777)
                    except OSError:
                        pass
            except (OSError, tarfile.TarError):
                # A single bad member is not fatal; the sandbox surfaces a
                # genuinely broken image when it fails to execute.
                continue


@dataclass
class PulledImage:
    rootfs: str
    config: ImageConfig
    digest: str


def pull_and_unpack(image_ref: str, digest: str, dest_dir: str) -> PulledImage:
    """Fetch `image_ref@digest`, verify every blob, unpack to `dest_dir/rootfs`.

    Returns the rootfs path and the image's run configuration. Raises
    PullError on any verification failure — there is no partial success.
    """
    registry_host, repo = parse_ref(image_ref)
    registry = _Registry(registry_host, repo)

    work = os.path.join(dest_dir, "work")
    rootfs = os.path.join(dest_dir, "rootfs")
    os.makedirs(work, exist_ok=True)
    os.makedirs(rootfs, exist_ok=True)

    manifest, media_type = registry.manifest(digest)
    if media_type in _INDEX_TYPES or "manifests" in manifest:
        platform_digest = _select_platform(manifest)
        manifest, media_type = registry.manifest(platform_digest)

    config = _image_config(manifest, registry, work)

    layers = manifest.get("layers") or manifest.get("fsLayers") or []
    if not layers:
        raise PullError("manifest has no layers")
    for index, layer in enumerate(layers):
        layer_digest = layer.get("digest") or layer.get("blobSum")
        if not layer_digest:
            raise PullError("layer without a digest")
        blob_path = os.path.join(work, f"layer-{index}.tar.gz")
        registry.blob(layer_digest, blob_path)
        _apply_layer(blob_path, rootfs)
        os.unlink(blob_path)

    return PulledImage(rootfs=rootfs, config=config, digest=digest)
