"""Build a described board in real KiCad and read it through the SWIG backend."""

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ..native_kicad_support import set_native_field
from .support import Board, Part, uuid_owner

_PAD_LAYOUTS = {
    "smd": ("PAD_ATTRIB_SMD", "SMDMask", False),
    "pth": ("PAD_ATTRIB_PTH", "PTHMask", True),
    "npth": ("PAD_ATTRIB_NPTH", "UnplatedHoleMask", True),
    "conn": ("PAD_ATTRIB_CONN", "ConnSMDMask", False),
}


class _Editor:
    """The pcbnew module, with this board open in its editor."""

    def __init__(self, pcbnew: Any, board: Any) -> None:
        self._pcbnew = pcbnew
        self.board = board

    def GetBoard(self) -> Any:
        """Return the open board, or None once it is closed."""
        return self.board

    def __getattr__(self, name: str) -> Any:
        """Reach every other pcbnew function."""
        return getattr(self._pcbnew, name)


def _add_field(pcbnew: Any, footprint: Any, name: str, text: str) -> None:
    """Add a field after the others, as KiCad's footprint dialog adds one.

    The dialog accepts a name the footprint already has, so this does too.
    KiCad 10 numbers user fields as one kind; KiCad 9 gives each its own id,
    and KiCad 8 numbers a new one after the others.
    """
    kind = getattr(pcbnew, "FIELD_T_USER", None)
    if kind is None:
        next_id = getattr(footprint, "GetNextFieldId", None)
        kind = next_id() if callable(next_id) else footprint.GetFieldCount()
    field = pcbnew.PCB_FIELD(footprint, kind, name)
    field.SetText(text)
    footprint.Add(field)


def _footprint(pcbnew: Any, board: Any, part: Part) -> Any:
    """Create one footprint the way the PCB editor stores it."""
    mm = pcbnew.FromMM
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(part.reference)
    footprint.SetValue(part.value)
    library, name = part.footprint.split(":", 1)
    footprint.SetFPID(pcbnew.LIB_ID(library, name))
    # KiCad 7 keeps the fields in a property map, where a repeated name
    # replaces the earlier text.
    keeps_repeats = hasattr(footprint, "GetFields")
    named = set()
    for name, text in part.fields:
        if name in named and keeps_repeats:
            _add_field(pcbnew, footprint, name, text)
        else:
            named.add(name)
            set_native_field(footprint, name, text)
    if part.side == "bottom":
        footprint.SetLayerAndFlip(pcbnew.B_Cu)
    kinds = {pad.kind for pad in part.pads}
    footprint.SetAttributes(
        (0x01 if "pth" in kinds else 0)
        | (0x02 if "smd" in kinds else 0)
        | (0x04 if part.exclude_from_pos else 0)
        | (0x08 if part.exclude_from_bom else 0)
    )
    if hasattr(footprint, "SetDNP"):  # KiCad 7 has no DNP flag
        footprint.SetDNP(part.dnp)
    # FOOTPRINT.Add puts each pad first, so add them last to first.
    for spec in reversed(part.pads):
        attribute, mask, drilled = _PAD_LAYOUTS[spec.kind]
        pad = pcbnew.PAD(footprint)
        pad.SetNumber(spec.number)
        pad.SetAttribute(getattr(pcbnew, attribute))
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        size = pcbnew.VECTOR2I(mm(1.0), mm(1.0))
        pad.SetSize(size)
        if drilled:
            pad.SetDrillSize(
                size if spec.kind == "npth" else pcbnew.VECTOR2I(mm(0.8), mm(0.8))
            )
        pad.SetLayerSet(getattr(pcbnew.PAD, mask)())
        footprint.Add(pad)
    board.Add(footprint)
    return footprint


def swig_board(
    modules: SimpleNamespace, parts: Sequence[Part], request: pytest.FixtureRequest
) -> Board:
    """Create a saved KiCad board holding the parts, open in a stand-in editor."""
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    filename = str(request.getfixturevalue("tmp_path") / "board.kicad_pcb")
    board = pcbnew.NewBoard(filename)
    board.SetFileName(filename)
    # BOARD.Add puts each footprint first, so add them last to first.
    footprints = [_footprint(pcbnew, board, part) for part in reversed(parts)][::-1]
    uuids = [str(footprint.m_Uuid.AsString()) for footprint in footprints]
    shared = {
        uuids[index]: uuids[uuid_owner(parts, index)]
        for index in range(len(parts))
        if uuid_owner(parts, index) != index
    }
    if shared:
        # KiCad never gives two footprints one UUID, but a board file can
        # hold them, and opening the file keeps both.
        pcbnew.SaveBoard(filename, board)
        path = Path(filename)
        text = path.read_text(encoding="utf-8")
        for uuid, owner in shared.items():
            text = text.replace(uuid, owner)
        path.write_text(text, encoding="utf-8")
        board = pcbnew.LoadBoard(filename)
    editor = _Editor(pcbnew, board)

    def close() -> None:
        editor.board = None

    return Board(
        modules.swig.SwigBackend(editor), close, **_capabilities(pcbnew, board)
    )


def _capabilities(pcbnew: Any, board: Any) -> dict[str, Any]:
    """Say what this KiCad's footprints hold, from a new footprint left unplaced."""
    footprint = pcbnew.FOOTPRINT(board)
    if hasattr(footprint, "GetFields"):
        builtin = tuple(str(field.GetName()) for field in footprint.GetFields())
    else:
        builtin = ("Reference", "Value")  # KiCad 7 keeps these apart
    return {
        "builtin_fields": builtin,
        "holds_dnp": hasattr(footprint, "SetDNP"),
        "keeps_repeated_fields": hasattr(footprint, "GetFields"),
    }
