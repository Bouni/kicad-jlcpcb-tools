"""Reach the open KiCad board through one interface, whichever way KiCad offers.

Shared code reads the board through ``BoardBackend`` and sees only the
immutable records in ``records``; ``fake`` holds a board in memory for
tests. The ``swig`` package implements the interface over KiCad's SWIG
bindings. Other code still works on SWIG objects, among them fabrication,
DRC, the variant adapter and the board edits, until later changes move it
behind the interface.
"""

from typing import Protocol

from .records import (
    BoardInfo,
    BoardSnapshot,
    ComponentRecord,
    ComponentState,
    FieldRecord,
    PadRecord,
    VariantDefinition,
    component_state,
    footprint_keys,
)


class BackendError(RuntimeError):
    """A board operation could not be completed."""


class BoardChanged(BackendError):
    """The board was closed, or another board is open in its place."""


class Unsupported(BackendError):
    """The running KiCad cannot do what was asked: its major has no dialect."""


class BoardBackend(Protocol):
    """The board operations shared code may use."""

    def snapshot(self) -> BoardSnapshot:
        """Read every footprint of the open board.

        Raise BoardChanged when no board is open.
        """
        ...


__all__ = [
    "BackendError",
    "BoardBackend",
    "BoardChanged",
    "BoardInfo",
    "BoardSnapshot",
    "ComponentRecord",
    "ComponentState",
    "FieldRecord",
    "PadRecord",
    "Unsupported",
    "VariantDefinition",
    "component_state",
    "footprint_keys",
]
