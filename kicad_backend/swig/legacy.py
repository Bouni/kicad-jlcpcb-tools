"""Hand live footprints to code that still edits or selects them through SWIG.

Board edits (``board_part_edits``) and the editor's selection have not
moved behind the backend yet. The window finds their footprints in a
snapshot and fetches the live objects here by key. Fabrication, DRC and the
variant adapter still take the live board from the window's
``_legacy_board()``. This module goes once edits and selection go through
the backend.
"""

from collections.abc import Iterable
from typing import Any

from ..records import footprint_keys


def live_footprints(board: Any, keys: Iterable[str]) -> dict[str, Any]:
    """Return the board's footprints with these keys, by key.

    Keys are the snapshot's: a footprint's UUID, with "#n" for the nth
    footprint sharing one, counted in board order.
    """
    wanted = set(keys)
    footprints = list(board.GetFootprints())
    uuids = (str(footprint.m_Uuid.AsString()) for footprint in footprints)
    return {
        key: footprint
        for key, footprint in zip(footprint_keys(uuids), footprints)
        if key in wanted
    }
