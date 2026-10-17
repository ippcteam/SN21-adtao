"""Unpacked images are removed even with read-only directories, and stale ones
left by killed runs are swept (17 Oct 2026: 6.5 GB had collected)."""
import os
import time

from hope.backtest.ns_sandbox import cleanup_rootfs, sweep_stale_images


def _image(root, name):
    d = os.path.join(root, name, "rootfs", "usr", "lib")
    os.makedirs(d)
    with open(os.path.join(d, "model.bin"), "wb") as f:
        f.write(b"x" * 4096)
    os.chmod(d, 0o555)                                   # read-only, as many images ship
    os.chmod(os.path.dirname(d), 0o555)
    return os.path.join(root, name)


def test_read_only_directories_no_longer_leave_the_image_behind(tmp_path):
    img = _image(str(tmp_path), "sn21-img-a")
    import shutil
    shutil.rmtree(img, ignore_errors=True)               # the old cleanup
    assert os.path.exists(img)                           # ...left it there
    cleanup_rootfs(img)
    assert not os.path.exists(img)


def test_cleanup_of_a_missing_folder_is_quiet(tmp_path):
    cleanup_rootfs(str(tmp_path / "gone"))


def test_sweep_removes_only_stale_image_folders(tmp_path):
    old = _image(str(tmp_path), "sn21-img-old")
    fresh = _image(str(tmp_path), "sn21-img-fresh")
    warm = _image(str(tmp_path), "sha256_abc")           # the forecast's warm image
    past = time.time() - 7 * 3600
    for p in (old, warm):
        os.utime(p, (past, past))
    out = sweep_stale_images(str(tmp_path))
    assert out["removed"] == 1 and out["kept"] == 1 and out["freed_mb"] == 0
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(warm)


def test_sweep_of_a_missing_root_is_quiet(tmp_path):
    assert sweep_stale_images(str(tmp_path / "none")) == {"removed": 0, "freed_mb": 0, "kept": 0}
