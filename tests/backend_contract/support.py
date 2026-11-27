"""Describe a board once and build it in each backend under test.

A contract test describes footprints with ``Part``; a builder returns a
``Board``: a backend holding those footprints and a way to close the board
as its editor would.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class PadSpec:
    """A pad by number and kind."""

    number: str
    kind: str = "smd"


@dataclass(frozen=True)
class Part:
    """One footprint as a test describes it."""

    reference: str
    value: str = "10k"
    footprint: str = "Lib:R_0603"
    fields: tuple[tuple[str, str], ...] = ()
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    side: str = "top"
    pads: tuple[PadSpec, ...] = ()
    uuid_of: str = ""  # share the UUID of the first part with this reference


def uuid_owner(parts: Sequence[Part], index: int) -> int:
    """Return the index of the part whose UUID this part has."""
    reference = parts[index].uuid_of
    if not reference:
        return index
    return next(i for i, part in enumerate(parts) if part.reference == reference)


@dataclass
class Board:
    """A backend holding the described parts, and how to close its board.

    The rest says what the backend's KiCad holds: the fields every footprint
    has, in order, whether a footprint has a DNP flag, and whether it keeps
    two fields with one name. The SWIG builder takes them from its KiCad's
    dialect; the defaults, KiCad 9's and 10's, are the fake's.
    """

    backend: Any
    close: Callable[[], None]
    builtin_fields: tuple[str, ...] = ("Reference", "Value", "Datasheet", "Description")
    holds_dnp: bool = True
    keeps_repeated_fields: bool = True


def fake_board(modules: SimpleNamespace, parts: Sequence[Part], _request: Any) -> Board:
    """Hold the parts in memory."""
    fake = modules.fake
    backend = fake.FakeBackend(
        fake.component(
            part.reference,
            uuid=f"uuid-{uuid_owner(parts, index)}",
            value=part.value,
            footprint=part.footprint,
            fields=part.fields,
            dnp=part.dnp,
            exclude_from_bom=part.exclude_from_bom,
            exclude_from_pos=part.exclude_from_pos,
            side=part.side,
            pads=[fake.pad(pad.kind) for pad in part.pads],
        )
        for index, part in enumerate(parts)
    )

    def close() -> None:
        backend.board = None

    return Board(backend, close)
