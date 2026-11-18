"""A plain (uncompressed) tar layer is unpacked like a gzipped one (UID 50, 18 Nov 2026).

OCI allows `application/vnd.oci.image.layer.v1.tar`. The puller read every layer as gzip, so such an image
failed with "Not a gzipped file (b'./')" and was rejected at the gate by our own error. The layer is now read
by what it is; a zstd layer is not read yet and fails as a pull error, which is ours.
"""
import gzip
import io
import tarfile

import pytest

from hope.backtest.oci_pull import PullError, _apply_layer
from hope.scoring.absence_penalty import operator_fault


def _tar(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _blob(tmp_path, data):
    p = tmp_path / "layer-0.tar.gz"      # the puller's own name for every layer, whatever it holds
    p.write_bytes(data)
    return str(p)


def test_a_plain_tar_layer_is_unpacked(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    data = _tar([("./app/model.py", b"print(1)")])
    assert data[:2] == b"./"             # what the gate saw: "Not a gzipped file (b'./')"
    _apply_layer(_blob(tmp_path, data), str(rootfs))
    assert (rootfs / "app/model.py").read_bytes() == b"print(1)"


def test_a_gzipped_layer_is_unpacked_as_before(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    _apply_layer(_blob(tmp_path, gzip.compress(_tar([("app/m.py", b"x")]))), str(rootfs))
    assert (rootfs / "app/m.py").read_bytes() == b"x"


def test_a_plain_tar_layer_cannot_escape_the_rootfs(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    with pytest.raises(PullError):
        _apply_layer(_blob(tmp_path, _tar([("../../etc/x", b"bad")])), str(rootfs))


def test_a_plain_tar_layer_honours_whiteouts(tmp_path):
    rootfs = tmp_path / "rootfs"
    (rootfs / "app").mkdir(parents=True)
    (rootfs / "app/old.py").write_bytes(b"old")
    _apply_layer(_blob(tmp_path, _tar([("app/.wh.old.py", b"")])), str(rootfs))
    assert not (rootfs / "app/old.py").exists()


def test_a_zstd_layer_fails_as_our_pull_error_so_the_model_stays_pending(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    with pytest.raises(PullError) as e:
        _apply_layer(_blob(tmp_path, b"\x28\xb5\x2f\xfd" + b"\0" * 64), str(rootfs))
    assert operator_fault(f"pull_failed: {e.value}")
