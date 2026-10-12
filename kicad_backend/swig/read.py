"""Read the board open in KiCad's PCB editor into records.

The snapshot's reads of the board and its footprints are made here, and
nothing returned holds a SWIG object. Code outside the backend still makes
its own SWIG reads for what the records do not hold yet.
"""

from contextlib import suppress
import hashlib
from inspect import getattr_static
from typing import Any

from .. import BoardChanged
from ..records import (
    BoardInfo,
    BoardSnapshot,
    ComponentRecord,
    FieldRecord,
    PadRecord,
    VariantDefinition,
    component_state,
)

# FOOTPRINT_ATTR_T bits, the same in KiCad 7 to 10. KiCad 10 moved FP_DNP,
# so DNP is read through IsDNP() instead.
_EXCLUDE_FROM_POS = 0x04
_EXCLUDE_FROM_BOM = 0x08
# PAD_ATTRIB values: PTH=0, SMD=1, CONN=2, NPTH=3.
_PAD_KINDS = ("pth", "smd", "conn", "npth")


def board_identity(board: Any) -> str:
    """Identify an open board without keeping a native handle.

    The identity includes the native address, the board UUID and the file
    name. KiCad can return several SWIG proxies for one board. Its saved UUID
    alone cannot distinguish a board reopened in the editor, and Save As
    changes the project whose settings and output paths the plugin must use.
    Objects that are not SWIG proxies fall back to Python identity.
    """
    if board is None:
        raise BoardChanged("The PCB editor no longer has an open board")
    pointer = board
    seen: set[int] = set()
    for _ in range(8):
        if getattr_static(pointer, "this", None) is None or id(pointer) in seen:
            break
        seen.add(id(pointer))
        pointer = pointer.this
    address = id(board)
    if seen:
        with suppress(TypeError, ValueError):
            address = int(pointer)
    uuid = getattr(board, "m_Uuid", None)
    uuid_text = uuid.AsString() if callable(getattr(uuid, "AsString", None)) else ""
    parts = (address, str(uuid_text), str(board.GetFileName()))
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def read_info(board: Any) -> BoardInfo:
    """Identify the open board and its file."""
    return BoardInfo(board_identity(board), str(board.GetFileName()))


def _pad(pad: Any) -> PadRecord:
    """Read one pad: its kind and whether it has a hole."""
    return PadRecord(_PAD_KINDS[int(pad.GetAttribute())], bool(pad.HasHole()))


def _fields(footprint: Any) -> list[FieldRecord]:
    """Read a footprint's fields in KiCad's order, every one of them.

    KiCad 7 has no field objects: Reference and Value stand apart, and the
    other fields are a property map that is never drawn. KiCad's footprint
    dialog accepts a second field with a name the footprint already has, and
    the footprint keeps both until the board is reopened; the state built
    from them resolves the name as reopening does.
    """
    try:
        fields = footprint.GetFields()
    except AttributeError:
        return [
            FieldRecord("Reference", str(footprint.GetReference())),
            FieldRecord("Value", str(footprint.GetValue())),
            *(
                FieldRecord(str(name), str(text))
                for name, text in footprint.GetProperties().items()
            ),
        ]
    return [FieldRecord(str(field.GetName()), str(field.GetText())) for field in fields]


def _component(footprint: Any) -> ComponentRecord:
    """Read one footprint with its Default state."""
    reference = str(footprint.GetReference())
    attributes = int(footprint.GetAttributes())
    # KiCad 7 has no DNP flag.
    is_dnp = getattr(footprint, "IsDNP", None)
    state = component_state(
        _fields(footprint),
        dnp=bool(is_dnp()) if callable(is_dnp) else False,
        exclude_from_bom=bool(attributes & _EXCLUDE_FROM_BOM),
        exclude_from_pos=bool(attributes & _EXCLUDE_FROM_POS),
    )
    return ComponentRecord(
        str(footprint.m_Uuid.AsString()),
        reference,
        str(footprint.GetFPIDAsString()),
        str(footprint.GetPath().AsString()),
        "bottom" if footprint.IsFlipped() else "top",
        tuple(_pad(pad) for pad in footprint.Pads()),
        {"": state},
    )


def read_snapshot(board: Any) -> BoardSnapshot:
    """Read every footprint of the open board in its Default state."""
    info = read_info(board)
    names = getattr(board, "GetVariantNamesForUI", None)
    # KiCad 10 lists its translated label for Default first.
    label = str(tuple(names())[0]) if callable(names) else "Default"
    components = tuple(_component(footprint) for footprint in board.GetFootprints())
    return BoardSnapshot(info, (VariantDefinition("", label),), components)
