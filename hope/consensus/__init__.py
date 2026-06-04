"""Per-cell consensus — aggregate per-episode signal into the
(transition × account-shape × horizon) cells the app consumes."""

from .cell_consensus import (
    CellConsensus,
    CellConsensusBuilder,
    cell_label,
    shape_stratum,
)

__all__ = [
    "CellConsensus",
    "CellConsensusBuilder",
    "cell_label",
    "shape_stratum",
]
