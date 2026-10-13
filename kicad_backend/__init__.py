"""Reach the open KiCad board through one interface, whichever way KiCad offers.

Shared code reads the board through ``BoardBackend`` and sees only the
immutable records in ``records``; ``fake`` holds a board in memory for
tests.
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
    "VariantDefinition",
    "component_state",
    "footprint_keys",
]
