"""The puller's job is to fetch untrusted bytes and REFUSE anything that does
not match the committed digest or tries to escape the rootfs. Those refusals
are the security surface, so they are what the tests exercise."""

import gzip
import io
import os
import tarfile

import pytest

from hope.backtest.oci_pull import (
    ImageConfig,
    PullError,
    _apply_layer,
    _safe_within,
    _select_platform,
    parse_ref,
)


def test_parse_ref_registry_host():
    assert parse_ref("ghcr.io/whale/sn21-mv") == ("ghcr.io", "whale/sn21-mv")


def test_parse_ref_docker_hub_single_name_gets_library_prefix():
    assert parse_ref("busybox") == ("registry-1.docker.io", "library/busybox")


def test_parse_ref_docker_hub_user_image():
    assert parse_ref("assasindev/sn21-ml") == (
        "registry-1.docker.io", "assasindev/sn21-ml")


def test_parse_ref_docker_io_alias_maps_to_registry_host():
    """`docker.io` is the friendly name; the registry v2 API is served from
    registry-1.docker.io. Hitting docker.io returns HTML, not a manifest."""
    assert parse_ref("docker.io/twoided/sn21-model") == (
        "registry-1.docker.io", "twoided/sn21-model")


def test_parse_ref_docker_io_alias_single_name_gets_library():
    assert parse_ref("docker.io/busybox") == (
        "registry-1.docker.io", "library/busybox")


def test_parse_ref_localhost_and_port():
    assert parse_ref("localhost:5000/x/y") == ("localhost:5000", "x/y")


def test_image_config_argv_is_entrypoint_then_cmd():
    cfg = ImageConfig(entrypoint=["/app/run"], cmd=["--serve"])
    assert cfg.argv() == ["/app/run", "--serve"]


def test_select_platform_prefers_amd64():
    index = {"manifests": [
        {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
    ]}
    assert _select_platform(index) == "sha256:amd"


def test_select_platform_single_unspecified_entry_is_accepted():
    """A plain single-arch push carries no platform block — that lone entry is
    the image itself."""
    index = {"manifests": [{"digest": "sha256:only", "platform": {}}]}
    assert _select_platform(index) == "sha256:only"


def test_select_platform_skips_attestation_manifest():
    """buildx adds an unknown/unknown attestation manifest; it must never be
    selected as the runnable image."""
    index = {"manifests": [
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
    ]}
    assert _select_platform(index) == "sha256:amd"


def test_select_platform_rejects_arm64_only_with_clear_message():
    """The real fingerthanos0 case (2026-08-11): arm64 image + attestation, no
    amd64. The executor is amd64, so this cannot run — and the error says so."""
    index = {"manifests": [
        {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
    ]}
    with pytest.raises(PullError, match="amd64"):
        _select_platform(index)


def test_select_platform_refuses_when_no_amd64_and_a_choice():
    index = {"manifests": [
        {"digest": "sha256:a", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:b", "platform": {"os": "windows", "architecture": "amd64"}},
    ]}
    with pytest.raises(PullError):
        _select_platform(index)


def test_safe_within_rejects_parent_escape(tmp_path):
    root = str(tmp_path / "rootfs")
    os.makedirs(root)
    assert _safe_within(root, os.path.join(root, "etc/passwd"))
    assert not _safe_within(root, os.path.join(root, "../../etc/passwd"))


def _layer(members):
    """A gzipped tar built from (name, bytes|None, type) tuples."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb") as g:
        g.write(raw.getvalue())
    return gz.getvalue()


def _write(tmp_path, name, blob):
    p = tmp_path / name
    p.write_bytes(blob)
    return str(p)


def test_apply_layer_unpacks_a_normal_file(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz",
                  _layer([("app/model.py", b"print(1)", "file")]))
    _apply_layer(blob, str(rootfs))
    assert (rootfs / "app/model.py").read_bytes() == b"print(1)"


def test_apply_layer_refuses_path_traversal(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz",
                  _layer([("../../etc/evil", b"x", "file")]))
    with pytest.raises(PullError):
        _apply_layer(blob, str(rootfs))
    assert not (tmp_path.parent / "etc" / "evil").exists()


def test_apply_layer_allows_absolute_symlink_contained_by_chroot(tmp_path):
    """An absolute symlink is SAFE: at runtime the miner is chrooted into the
    rootfs, so `/etc/passwd` resolves to `rootfs/etc/passwd`, contained. It is
    created as-is; the extraction-time escape it could enable is blocked
    separately by the containment check on every following member."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz",
                  _layer([("sneaky", "/etc/passwd", "symlink")]))
    _apply_layer(blob, str(rootfs))
    assert os.path.islink(rootfs / "sneaky")


def test_apply_layer_skips_dotdot_symlink_escape(tmp_path):
    """A `..`-escaping symlink target would let a later member write outside
    the rootfs during extraction — it is skipped, not created."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "app").mkdir(parents=True)
    blob = _write(tmp_path, "l.tgz",
                  _layer([("app/sneaky", "../../../../etc", "symlink")]))
    _apply_layer(blob, str(rootfs))
    assert not (rootfs / "app" / "sneaky").exists()


def test_apply_layer_whiteout_deletes_prior_file(tmp_path):
    rootfs = tmp_path / "rootfs"
    (rootfs / "app").mkdir(parents=True)
    (rootfs / "app" / "old.txt").write_text("stale")
    blob = _write(tmp_path, "l.tgz",
                  _layer([("app/.wh.old.txt", b"", "file")]))
    _apply_layer(blob, str(rootfs))
    assert not (rootfs / "app" / "old.txt").exists()
