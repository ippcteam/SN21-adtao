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
    resolve_in_rootfs,
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
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = data
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


# ---- links resolved as the chrooted model sees them ------------------------

def test_resolve_in_rootfs_restarts_absolute_links_at_the_rootfs(tmp_path):
    root = tmp_path / "rootfs"
    (root / "usr" / "bin").mkdir(parents=True)
    os.symlink("/usr/bin", root / "bin")
    assert resolve_in_rootfs(str(root), "bin/tar") == str(root / "usr" / "bin" / "tar")


def test_resolve_in_rootfs_never_climbs_above_the_root(tmp_path):
    root = tmp_path / "rootfs"
    root.mkdir()
    assert resolve_in_rootfs(str(root), "../../etc/passwd") == str(root / "etc" / "passwd")


def test_resolve_in_rootfs_refuses_a_link_loop(tmp_path):
    root = tmp_path / "rootfs"
    root.mkdir()
    os.symlink("b", root / "a")
    os.symlink("a", root / "b")
    with pytest.raises(PullError, match="symbolic links"):
        resolve_in_rootfs(str(root), "a/x")


def test_a_merged_usr_image_with_an_absolute_bin_link_unpacks(tmp_path):
    """11 Sept 2026: two digests from one miner were rejected with
    `layer member escapes rootfs: 'bin/tar'`. A lower layer made `bin` an
    absolute link to /usr/bin; a later layer wrote bin/tar through it. In the
    chroot that is usr/bin/tar — it must unpack there, not be refused."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    base = _write(tmp_path, "base.tgz", _layer([
        ("usr", None, "dir"), ("usr/bin", None, "dir"), ("bin", "/usr/bin", "symlink")]))
    upper = _write(tmp_path, "upper.tgz", _layer([("bin/tar", b"tar!", "file")]))
    _apply_layer(base, str(rootfs))
    _apply_layer(upper, str(rootfs))
    assert (rootfs / "usr" / "bin" / "tar").read_bytes() == b"tar!"
    assert os.path.islink(rootfs / "bin")


def test_a_write_through_an_absolute_link_never_reaches_the_host(tmp_path):
    """The link target is an absolute path that EXISTS on this host. Resolved
    through the host, the file would be written there."""
    host_dir = tmp_path / "host_bin"
    host_dir.mkdir()
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    base = _write(tmp_path, "base.tgz", _layer([("bin", str(host_dir), "symlink")]))
    upper = _write(tmp_path, "upper.tgz", _layer([("bin/payload", b"x", "file")]))
    _apply_layer(base, str(rootfs))
    _apply_layer(upper, str(rootfs))
    assert not (host_dir / "payload").exists()
    assert (rootfs / str(host_dir).lstrip("/") / "payload").read_bytes() == b"x"


def test_a_relative_link_in_a_lower_layer_is_honoured(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    base = _write(tmp_path, "base.tgz", _layer([
        ("usr", None, "dir"), ("usr/lib", None, "dir"), ("lib", "usr/lib", "symlink")]))
    upper = _write(tmp_path, "upper.tgz", _layer([("lib/libm.so", b"elf", "file")]))
    _apply_layer(base, str(rootfs))
    _apply_layer(upper, str(rootfs))
    assert (rootfs / "usr" / "lib" / "libm.so").read_bytes() == b"elf"


def test_a_file_replaces_a_lower_link_instead_of_writing_through_it(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "etc").mkdir()
    (rootfs / "etc" / "passwd").write_text("root")
    base = _write(tmp_path, "base.tgz", _layer([
        ("app", None, "dir"), ("app/config", "/etc/passwd", "symlink")]))
    upper = _write(tmp_path, "upper.tgz", _layer([("app/config", b"mine", "file")]))
    _apply_layer(base, str(rootfs))
    _apply_layer(upper, str(rootfs))
    assert not os.path.islink(rootfs / "app" / "config")
    assert (rootfs / "app" / "config").read_bytes() == b"mine"
    assert (rootfs / "etc" / "passwd").read_text() == "root"


def test_a_whiteout_through_an_absolute_link_deletes_inside_the_rootfs(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    base = _write(tmp_path, "base.tgz", _layer([
        ("usr", None, "dir"), ("usr/bin", None, "dir"),
        ("usr/bin/old", b"old", "file"), ("bin", "/usr/bin", "symlink")]))
    upper = _write(tmp_path, "upper.tgz", _layer([("bin/.wh.old", b"", "file")]))
    _apply_layer(base, str(rootfs))
    _apply_layer(upper, str(rootfs))
    assert not (rootfs / "usr" / "bin" / "old").exists()


def test_a_hardlink_is_created_inside_the_rootfs(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz", _layer([
        ("usr", None, "dir"), ("usr/bin", None, "dir"),
        ("usr/bin/python3.11", b"py", "file"),
        ("usr/bin/python3", "usr/bin/python3.11", "hardlink")]))
    _apply_layer(blob, str(rootfs))
    assert (rootfs / "usr" / "bin" / "python3").read_bytes() == b"py"


def test_an_absolute_member_name_is_refused(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz", _layer([("/etc/evil", b"x", "file")]))
    with pytest.raises(PullError, match="escapes rootfs"):
        _apply_layer(blob, str(rootfs))


def test_a_link_loop_in_an_image_fails_the_pull(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    blob = _write(tmp_path, "l.tgz", _layer([
        ("a", "b", "symlink"), ("b", "a", "symlink"), ("a/x", b"x", "file")]))
    with pytest.raises(PullError, match="symbolic links"):
        _apply_layer(blob, str(rootfs))


def test_the_entrypoint_is_found_inside_the_image_not_on_the_host(tmp_path):
    """An image whose `usr` is an absolute link and which ships no python3 must
    not borrow this host's /usr/bin/python3 when its command is resolved."""
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("host has no /usr/bin/python3 to be confused by")
    from types import SimpleNamespace

    from hope.backtest.local_executor import _resolve_entrypoint

    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    os.symlink("/usr", rootfs / "usr")
    image = SimpleNamespace(rootfs=str(rootfs), config=ImageConfig(cmd=["python3"]))
    assert _resolve_entrypoint(image, None) == ["python3"]

    (rootfs / "opt").mkdir()
    os.unlink(rootfs / "usr")
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "python3").write_bytes(b"py")
    assert _resolve_entrypoint(image, None) == ["/usr/bin/python3"]
