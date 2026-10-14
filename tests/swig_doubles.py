"""Stateful stand-ins for the KiCad SWIG objects the board backend reads.

They answer every call ``kicad_backend.swig`` makes, so tests that still
edit footprints through SWIG-shaped objects can read them through the real
SWIG backend.
"""

from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5

# PAD_ATTRIB values: PTH=0, SMD=1, CONN=2, NPTH=3.
PAD_ATTRIBUTES = {"pth": 0, "smd": 1, "conn": 2, "npth": 3}


def point(x: int, y: int) -> SimpleNamespace:
    """Return a VECTOR2I."""
    return SimpleNamespace(x=x, y=y)


class Field:
    """A footprint field with text and visibility."""

    def __init__(self, name: str, text: str, visible: bool = True) -> None:
        self.name, self.text, self.visible = name, text, visible

    def GetName(self) -> str:
        """Return the field name."""
        return self.name

    def GetText(self) -> str:
        """Return the field text."""
        return self.text

    def IsVisible(self) -> bool:
        """Return whether the field is shown."""
        return self.visible

    def SetVisible(self, visible: bool) -> None:
        """Show or hide the field."""
        self.visible = visible


class Pad:
    """A pad by number and kind."""

    def __init__(self, number: str = "1", kind: str = "smd") -> None:
        self.number, self.kind = number, kind

    def GetAttribute(self) -> int:
        """Return KiCad's PAD_ATTRIB value."""
        return PAD_ATTRIBUTES[self.kind]

    def HasHole(self) -> bool:
        """Return whether the pad is drilled."""
        return self.kind in {"pth", "npth"}


class Footprint:
    """A footprint with live fields, attribute bits, placement and pads."""

    def __init__(
        self,
        reference: str = "R1",
        *,
        value: str = "10k",
        footprint: str = "Lib:R_0603",
        fields: Optional[Mapping[str, str]] = None,  # noqa: UP045
        attributes: int = 0,
        dnp: bool = False,
        flipped: bool = False,
        position: tuple[int, int] = (0, 0),
        orientation: float = 0.0,
        pads: Iterable[Pad] = (),
        uuid: Optional[str] = None,  # noqa: UP045
        path: str = "",
    ) -> None:
        self.reference, self.value, self.footprint = reference, value, footprint
        self.fields = {name: Field(name, text) for name, text in (fields or {}).items()}
        self.attributes, self.dnp, self.flipped = attributes, dnp, flipped
        self.position, self.orientation = position, orientation
        self.pads = list(pads)
        self.uuid = uuid or str(uuid5(NAMESPACE_URL, f"swig-double:{reference}"))
        self.path = path
        self.modified = False
        self.descriptions = {"Datasheet": "", "Description": ""}
        self.m_Uuid = SimpleNamespace(AsString=lambda: self.uuid)

    def GetReference(self) -> str:
        """Return the reference."""
        return self.reference

    def GetValue(self) -> str:
        """Return the Value field's text."""
        return self.value

    def GetFPID(self) -> SimpleNamespace:
        """Return the library identifier."""
        return SimpleNamespace(GetLibItemName=lambda: self.footprint.split(":", 1)[-1])

    def GetFPIDAsString(self) -> str:
        """Return the library identifier as "Lib:Name"."""
        return self.footprint

    def GetFields(self) -> list[Field]:
        """Return Reference, Value, Datasheet, Description, then the user fields."""
        return [
            Field("Reference", self.reference),
            Field("Value", self.value),
            *(Field(name, text, False) for name, text in self.descriptions.items()),
            *self.fields.values(),
        ]

    def SetField(self, name: str, text: str) -> None:
        """Change a field, adding a user field when the name is new."""
        if name == "Reference":
            self.reference = text
        elif name == "Value":
            self.value = text
        elif name in self.descriptions:
            self.descriptions[name] = text
        else:
            self.fields.setdefault(name, Field(name, "")).text = text

    def RemoveField(self, name: str) -> None:
        """Remove a user field."""
        self.fields.pop(name, None)

    def GetAttributes(self) -> int:
        """Return the attribute bits."""
        return self.attributes

    def SetAttributes(self, attributes: int) -> None:
        """Replace the attribute bits."""
        self.attributes = attributes

    def IsDNP(self) -> bool:
        """Return the DNP flag."""
        return self.dnp

    def IsFlipped(self) -> bool:
        """Return whether the footprint is on the bottom."""
        return self.flipped

    def GetLayer(self) -> int:
        """Return F.Cu or B.Cu."""
        return 2 if self.flipped else 0

    def GetPosition(self) -> SimpleNamespace:
        """Return the anchor position."""
        return point(*self.position)

    def GetOrientation(self) -> SimpleNamespace:
        """Return the rotation."""
        return SimpleNamespace(AsDegrees=lambda: self.orientation)

    def GetPath(self) -> SimpleNamespace:
        """Return the symbol path."""
        return SimpleNamespace(AsString=lambda: self.path)

    def Pads(self) -> list[Pad]:
        """Return the pads."""
        return list(self.pads)

    def SetModified(self) -> None:
        """Record an edit."""
        self.modified = True


class Board:
    """A saved board holding footprints in order."""

    def __init__(
        self,
        footprints: Iterable[Footprint] = (),
        *,
        filename: str = "/project/board.kicad_pcb",
        variants: Optional[Iterable[str]] = None,  # noqa: UP045
    ) -> None:
        self.footprints = list(footprints)
        self.filename = filename
        self.uuid = "board-uuid"
        self.m_Uuid = SimpleNamespace(AsString=lambda: self.uuid)
        if variants is not None:
            names = list(variants)
            self.GetVariantNamesForUI = lambda: names

    def GetFileName(self) -> str:
        """Return the board file."""
        return self.filename

    def GetFootprints(self) -> list[Footprint]:
        """Return the footprints in board order."""
        return list(self.footprints)

    def FindFootprintByReference(self, reference: str) -> Optional[Footprint]:  # noqa: UP045
        """Return the first footprint with this reference."""
        return next((fp for fp in self.footprints if fp.reference == reference), None)


def editor(board: Any, version: str = "10.0.4") -> SimpleNamespace:
    """Return a pcbnew module of this KiCad version whose editor has ``board`` open."""
    return SimpleNamespace(
        GetBoard=lambda: board,
        GetBuildVersion=lambda: version,
        GetMajorMinorVersion=lambda: ".".join(version.split(".")[:2]),
    )
