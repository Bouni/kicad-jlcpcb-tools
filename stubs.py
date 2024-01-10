"""Stubs for pcbnew classes used in the plugin."""

class LIB_ID_Stub:
    """Implementation of pcbnew.LIB_ID."""

    def __init__(self, item_name):
        self.item_name = item_name

    def GetLibItemName(self) -> str:
        """Item name."""
        return self.item_name


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

    def GetLayer(self) -> int:
        """Layer number."""
        # TODO: maybe this is defined in a python module we can import and reuse here?
        return 3  # F_Cu, see https://docs.kicad.org/doxygen/layer__ids_8h.html#ae0ad6e574332a997f501d1b091c3f53f


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
