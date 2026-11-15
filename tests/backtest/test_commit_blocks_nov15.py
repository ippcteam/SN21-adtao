"""The first block each hotkey was seen committing each digest (15 Nov 2026)."""
from hope.backtest import commit_blocks as CB

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def c(dg, repo="ghcr.io/x/m"):
    return f"sn21-model:v1:{repo}@{dg}"


def test_the_earliest_block_per_hotkey_and_digest_is_kept(tmp_path):
    root = str(tmp_path)
    CB.remember(root, {"h1": (100, c(A)), "h2": (110, c(A))})
    # h1 re-commits the same digest: the chain now shows a later block.
    CB.remember(root, {"h1": (150, c(A)), "h2": (110, c(A))})
    CB.remember(root, {"h1": (200, c(B))})
    assert CB.load(root) == {"h1": {"a" * 16: 100, "b" * 16: 200}, "h2": {"a" * 16: 110}}


def test_unknown_blocks_and_garbage_are_not_recorded(tmp_path):
    root = str(tmp_path)
    CB.remember(root, {"h1": (0, c(A)), "h2": (None, c(A)), "h3": (5, "not ours"),
                       "h4": "broken", "h5": (7, c(A))})
    assert CB.load(root) == {"h5": {"a" * 16: 7}}


def test_a_dry_run_writes_nothing(tmp_path):
    root = str(tmp_path)
    out = CB.remember(root, {"h1": (100, c(A))}, save_it=False)
    assert out == {"h1": {"a" * 16: 100}} and CB.load(root) == {}

