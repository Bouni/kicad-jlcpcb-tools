"""Stubs for standalone usage of the plugin."""

from typing import Optional


class LIB_ID_Stub:
    """Implementation of pcbnew.LIB_ID."""

    def __init__(self, item_name):
        self.item_name = item_name

    def GetLibItemName(self) -> str:
        """Item name."""
        return self.item_name


class Field_Stub:
    """Implementation of pcbnew.Field."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text
        self.visible = True

    def GetName(self) -> str:
        """Field name."""
        return self.name

    def GetText(self) -> str:
        """Field text."""
        return self.text

    def SetVisible(self, visible: bool) -> None:
        """Set the field visibility."""
        self.visible = visible

    def IsVisible(self) -> bool:
        """Return the field visibility."""
        return self.visible


class Footprint_Stub:
    """Implementation of pcbnew.Footprint."""

    def __init__(self, reference: str, value: str, fpid: LIB_ID_Stub) -> None:
        self.reference = reference
        self.value = value
        self.fpid = fpid
        self.fields: dict[str, Field_Stub] = {}
        self.attributes = 0

    def GetReference(self) -> str:
        """Retrieve the reference designator string."""
        return self.reference

    def GetValue(self) -> str:
        """Value string."""
        return self.value

    def GetFPID(self) -> LIB_ID_Stub:
        """Footprint LIB_ID."""
        return self.fpid

    def GetProperties(self) -> dict[str, str]:
        """Properties."""
        return {name: field.GetText() for name, field in self.fields.items()}

    def GetAttributes(self) -> int:
        """Attributes."""
        return self.attributes

    def SetAttributes(self, attributes: int) -> None:
        """Set the footprint flags."""
        self.attributes = attributes

    def GetFields(self) -> list[Field_Stub]:
        """Fields."""
        return list(self.fields.values())

    def SetField(self, name: str, text: str) -> None:
        """Set a field without replacing its existing visibility or identity."""
        if name in self.fields:
            self.fields[name].text = text
        else:
            self.fields[name] = Field_Stub(name, text)

    def GetFieldByName(self, name: str) -> Optional[Field_Stub]:
        """Get a field by name."""
        return self.fields.get(name)

    def GetLayer(self) -> int:
        """Layer number."""
        # TODO: maybe this is defined in a python module we can import and reuse here?
        return 3  # F_Cu, see https://docs.kicad.org/doxygen/layer__ids_8h.html#ae0ad6e574332a997f501d1b091c3f53f

    def SetSelected(self):
        """Select this item."""


class BoardStub:
    """Implementation of pcbnew.Board."""

    def __init__(self):
        self.footprints = []
        self.footprints.append(Footprint_Stub("R1", "100", LIB_ID_Stub("resistors")))

    def GetFileName(self):
        """Board filename."""
        return "fake_test_board.kicad_pcb"

    def GetFootprints(self):
        """Footprint list."""
        return self.footprints

    def FindFootprintByReference(self, reference: str) -> Optional[Footprint_Stub]:
        """Return the existing footprint matching a reference, if present."""
        return next(
            (fp for fp in self.footprints if fp.GetReference() == reference), None
        )

    def Drawings(self):
        """Return board drawings.

        Standalone mode has no real drawing geometry, so expose an empty list.
        """
        return []

    def GetLayerName(self, _layer_id):
        """Return a layer name for a layer id.

        Included for compatibility with code paths that inspect board layer
        names in standalone mode.
        """
        return "Dwgs.User"


class PcbnewStub:
    """Stub implementation of pcbnew."""

    def __init__(self):
        self.board = BoardStub()

    def GetBoard(self):
        """Get the board."""
        return self.board

    def GetBuildVersion(self):
        """Get the kicad build version."""
        return "8.0.1"

    def GetCurrentSelection(self):
        """Get the currently selected board items."""
        return []

    def Refresh(self):
        """Redraw the screen."""


class KicadStub:
    """Stub implementation of Kicad."""

    def __init__(self):
        self.pcbnew = PcbnewStub()

    def get_pcbnew(self):
        """Get the pcbnew stub."""
        return self.pcbnew
