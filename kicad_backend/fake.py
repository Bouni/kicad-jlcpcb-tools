"""A board held in memory, read through the same interface as KiCad's.

Tests build components with ``component`` and ``pad`` and change
``FakeBackend.components`` to stand for edits made in the PCB editor.
"""

from collections.abc import Iterable, Mapping
from typing import Optional, Union
from uuid import NAMESPACE_URL, uuid5

from . import BoardChanged
from .records import (
    BoardInfo,
    BoardSnapshot,
    ComponentRecord,
    FieldRecord,
    PadRecord,
    VariantDefinition,
    component_state,
)


def board_info(
    file_name: str = "/project/board.kicad_pcb", *, token: str = "fake-board"
) -> BoardInfo:
    """Describe a board saved as ``file_name``."""
    return BoardInfo(token, file_name)


def pad(
    kind: str = "smd",
    *,
    has_hole: Optional[bool] = None,  # noqa: UP045
) -> PadRecord:
    """Build a pad; plated and bare holes have holes unless ``has_hole`` says."""
    return PadRecord(kind, kind in {"pth", "npth"} if has_hole is None else has_hole)


def component(
    reference: str,
    *,
    uuid: Optional[str] = None,  # noqa: UP045
    value: str = "",
    footprint: str = "Lib:Footprint",
    fields: Union[Mapping[str, str], Iterable[tuple[str, str]], None] = None,
    dnp: bool = False,
    exclude_from_bom: bool = False,
    exclude_from_pos: bool = False,
    schematic_path: str = "",
    side: str = "top",
    pads: Iterable[PadRecord] = (),
) -> ComponentRecord:
    """Build a footprint with Default only, fields in KiCad's order.

    KiCad lists Reference, Value, Datasheet and Description, then the
    footprint's own fields: here ``fields`` in order. A name already listed,
    one of those four or one that comes again in (name, text) pairs, adds
    another field of that name after the others, as a KiCad footprint can
    hold. The UUID defaults to one derived from the reference, so two
    footprints sharing a reference need their own.
    """
    texts = {"Reference": reference, "Value": value, "Datasheet": "", "Description": ""}
    given = set(texts)
    repeated = []
    pairs = fields.items() if isinstance(fields, Mapping) else fields or ()
    for name, text in pairs:
        if name in given:
            repeated.append((name, text))
        else:
            given.add(name)
            texts[name] = text
    state = component_state(
        (FieldRecord(name, text) for name, text in (*texts.items(), *repeated)),
        dnp=dnp,
        exclude_from_bom=exclude_from_bom,
        exclude_from_pos=exclude_from_pos,
    )
    return ComponentRecord(
        str(uuid5(NAMESPACE_URL, f"fake-footprint:{reference}"))
        if uuid is None
        else uuid,
        reference,
        footprint,
        schematic_path,
        side,
        tuple(pads),
        {"": state},
    )


class FakeBackend:
    """Serve a board from memory with the checks a real backend makes.

    Set ``board`` to None to close it, as the PCB editor can.
    """

    def __init__(
        self,
        components: Iterable[ComponentRecord] = (),
        *,
        info: Optional[BoardInfo] = None,  # noqa: UP045
    ) -> None:
        self.components = tuple(components)
        self.board: Optional[BoardInfo] = info or board_info()  # noqa: UP045
        self.reads = 0

    def snapshot(self) -> BoardSnapshot:
        """Read every footprint, as a real backend reads the live board.

        A closed board raises BoardChanged.
        """
        if self.board is None:
            raise BoardChanged("The PCB editor no longer has an open board")
        self.reads += 1
        return BoardSnapshot(
            self.board, (VariantDefinition("", "Default"),), tuple(self.components)
        )
