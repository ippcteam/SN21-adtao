"""Per-cell consensus — aggregate per-episode signal into the
(transition × account-shape × horizon) cells the app consumes."""

from .cell_consensus import (
    CellConsensus,
    CellConsensusBuilder,
    cell_label,
    shape_stratum,
)
from .epoch_consensus import (
    build_epoch_consensus,
    compute_and_persist_consensus,
    read_rolling_state,
    resolve_consensus_dir,
)

__all__ = [
    "CellConsensus",
    "CellConsensusBuilder",
    "cell_label",
    "shape_stratum",
    "build_epoch_consensus",
    "compute_and_persist_consensus",
    "read_rolling_state",
    "resolve_consensus_dir",
]
