"""Stubs for standalone usage of the plugin."""


class LIB_ID_Stub:
    """Implementation of pcbnew.LIB_ID."""

    def __init__(self, item_name):
        self.item_name = item_name

    def GetLibItemName(self) -> str:
        """Item name."""
        return self.item_name


class Field_Stub:
    """Implementation of pcbnew.Field."""

    def __init__(self, name, text):
        self.name = name
        self.text = text

    def GetName(self) -> str:
        """Field name."""
        return self.name

    def GetText(self) -> str:
        """Field text."""
        return self.text

    def SetVisible(self, visible):
        """Set the field visibility."""
        pass


class Footprint_Stub:
    """Implementation of pcbnew.Footprint."""

    def __init__(self, reference, value, fpid):
        self.reference = reference
        self.value = value
        self.fpid = fpid

    def GetReference(self) -> str:
        """Retrieve the reference designator string."""
        return self.reference

    def GetValue(self) -> str:
        """Value string."""
        return self.value

    def GetFPID(self) -> LIB_ID_Stub:
        """Footprint LIB_ID."""
        return self.fpid

    def GetProperties(self) -> dict:
        """Properties."""
        return {}

    def GetAttributes(self) -> int:
        """Attributes."""
        return 0

    def GetFields(self) -> list:
        """Fields."""
        return []

    def SetField(self, name, text):
        """Set a field."""
        pass

    def GetFieldByName(self, name) -> Field_Stub:
        """Get a field by name."""
        return Field_Stub(name, "stub")

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

    def FindFootprintByReference(self, reference):
        """Get a list of footprints that match a reference."""
        return Footprint_Stub(reference, "stub", 100)


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
