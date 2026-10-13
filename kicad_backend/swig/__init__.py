"""The board backend over KiCad's SWIG bindings, for KiCad 7 to 10.

It reaches KiCad through the ``pcbnew`` module its caller passes in, and
the records it returns hold no SWIG object.
"""

from typing import Any

from .. import BoardChanged
from ..records import BoardSnapshot
from .read import read_snapshot


class SwigBackend:
    """Read the board open in KiCad's PCB editor."""

    def __init__(self, pcbnew: Any) -> None:
        self._pcbnew = pcbnew

    def _board(self) -> Any:
        """Return the editor's board, or raise BoardChanged when it has none."""
        board = self._pcbnew.GetBoard()
        if board is None:
            raise BoardChanged("The PCB editor no longer has an open board")
        return board

    def snapshot(self) -> BoardSnapshot:
        """Read every footprint of the open board in its Default state.

        Raise BoardChanged when no board is open.
        """
        return read_snapshot(self._board())
