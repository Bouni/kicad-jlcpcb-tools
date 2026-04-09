"""Stubs for standalone usage of the plugin."""

from typing import Any, Dict, List, Optional, Tuple


class Point_Stub:
    """Simple point-like object compatible with x/y access and tuple unpacking."""

    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

    def __iter__(self):
        yield self.x
        yield self.y


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

    def Footprints(self):
        """Footprint iterator-style access used by some KiCad versions."""
        return self.footprints

    def GetCopperLayerCount(self) -> int:
        """Copper layer count."""
        return 2

    def GetEnabledLayers(self):
        """Enabled layer IDs wrapper."""

        class EnabledLayers_Stub:
            def Seq(self_nonlocal):
                return [0, 1, 2, 3, 4, 5, 7, 8, 9]

        return EnabledLayers_Stub()

    def GetLayerName(self, layer_id):
        """Layer name lookup."""
        layer_names = {
            0: "F_Cu",
            1: "B_Cu",
            2: "F_SilkS",
            3: "B_SilkS",
            4: "F_Mask",
            5: "B_Mask",
            7: "F_Paste",
            8: "B_Paste",
            9: "Edge_Cuts",
        }
        return layer_names.get(layer_id, str(layer_id))

    def GetDesignSettings(self):
        """Board design settings wrapper."""

        class DesignSettings_Stub:
            def GetAuxOrigin(self_nonlocal):
                return Point_Stub(0, 0)

        return DesignSettings_Stub()


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


class StubBoardAdapter:
    """Standalone adapter for board-level API."""

    def __init__(self, pcbnew: PcbnewStub):
        self.pcbnew = pcbnew

    def get_board(self) -> BoardStub:
        return self.pcbnew.GetBoard()

    def get_board_filename(self) -> str:
        return self.get_board().GetFileName()

    def get_all_footprints(self) -> List[Footprint_Stub]:
        return sorted(self.get_board().GetFootprints(), key=lambda x: x.GetReference())

    def get_footprints(self) -> List[Footprint_Stub]:
        return list(self.get_board().Footprints())

    def get_footprint_by_reference(self, reference: str) -> Optional[Footprint_Stub]:
        return self.get_board().FindFootprintByReference(reference)

    def get_enabled_layers(self) -> List[int]:
        return []

    def get_layer_name(self, layer_id: int) -> str:
        return str(layer_id)

    def get_design_settings(self) -> Any:
        return None

    def get_drawings(self) -> List[Any]:
        return []

    def refresh_display(self) -> None:
        self.pcbnew.Refresh()

    def get_current_selection(self) -> List[Any]:
        return list(self.pcbnew.GetCurrentSelection())

    def get_copper_layer_count(self) -> int:
        return self.get_board().GetCopperLayerCount()

    def get_aux_origin(self) -> Point_Stub:
        return self.get_board().GetDesignSettings().GetAuxOrigin()


class StubFootprintAdapter:
    """Standalone adapter for footprint-level API."""

    def get_reference(self, footprint: Footprint_Stub) -> str:
        return footprint.GetReference()

    def get_value(self, footprint: Footprint_Stub) -> str:
        return footprint.GetValue()

    def get_fpid_name(self, footprint: Footprint_Stub) -> str:
        return footprint.GetFPID().GetLibItemName()

    def get_layer(self, footprint: Footprint_Stub) -> int:
        return footprint.GetLayer()

    def get_orientation(self, footprint: Footprint_Stub) -> float:
        return 0.0

    def get_position(self, footprint: Footprint_Stub) -> Tuple[float, float]:
        return (0.0, 0.0)

    def get_attributes(self, footprint: Footprint_Stub) -> int:
        return footprint.GetAttributes()

    def set_attributes(self, footprint: Footprint_Stub, attributes: int) -> None:
        return None

    def get_lcsc_value(self, footprint: Footprint_Stub) -> str:
        return ""

    def set_lcsc_value(self, footprint: Footprint_Stub, lcsc: str) -> None:
        footprint.SetField("LCSC", lcsc)

    def get_exclude_from_pos(self, footprint: Footprint_Stub) -> bool:
        return False

    def get_exclude_from_bom(self, footprint: Footprint_Stub) -> bool:
        return False

    def get_is_dnp(self, footprint: Footprint_Stub) -> bool:
        return False

    def set_selected(self, footprint: Footprint_Stub) -> None:
        footprint.SetSelected()

    def clear_selected(self, footprint: Footprint_Stub) -> None:
        return None

    def toggle_exclude_from_pos(self, footprint: Footprint_Stub) -> bool:
        return False

    def toggle_exclude_from_bom(self, footprint: Footprint_Stub) -> bool:
        return False

    def get_pads(self, footprint: Footprint_Stub) -> List[Any]:
        return []


class StubGerberAdapter:
    """Standalone no-op adapter for gerber generation API."""

    def create_plot_controller(self, board: Any) -> Any:
        return None

    def get_plot_options(self, plot_controller: Any) -> Any:
        return None

    def set_output_directory(self, plot_options: Any, directory: str) -> None:
        return None

    def set_format(self, plot_options: Any, format_id: int) -> None:
        return None

    def set_plot_component_values(self, plot_options: Any, value: bool) -> None:
        return None

    def set_plot_reference_designators(self, plot_options: Any, value: bool) -> None:
        return None

    def set_sketch_pads_on_mask_layers(self, plot_options: Any, value: bool) -> None:
        return None

    def set_use_protel_extensions(self, plot_options: Any, value: bool) -> None:
        return None

    def set_create_job_file(self, plot_options: Any, value: bool) -> None:
        return None

    def set_mask_color(self, plot_options: Any, value: bool) -> None:
        return None

    def set_use_auxiliary_origin(self, plot_options: Any, value: bool) -> None:
        return None

    def set_plot_vias_on_mask(self, plot_options: Any, value: bool) -> None:
        return None

    def set_use_x2_format(self, plot_options: Any, value: bool) -> None:
        return None

    def set_include_netlist_attributes(self, plot_options: Any, value: bool) -> None:
        return None

    def set_disable_macros(self, plot_options: Any, value: bool) -> None:
        return None

    def set_plot_frame_ref(self, plot_options: Any, value: bool) -> None:
        return None

    def set_skip_plot_npth_pads(self, plot_options: Any, value: bool) -> None:
        return None

    def set_layer(self, plot_controller: Any, layer_id: int) -> None:
        return None

    def open_plot_file(
        self, plot_controller: Any, filename: str, extension: str, plot_title: str
    ) -> None:
        return None

    def plot_layer(self, plot_controller: Any) -> bool:
        return True

    def close_plot(self, plot_controller: Any) -> None:
        return None

    def set_drill_marks(self, plot_options: Any, mark_type: int) -> None:
        return None

    def create_excellon_writer(self, board: Any) -> Any:
        return None

    def set_drill_options(self, writer: Any, **kwargs: Any) -> None:
        return None

    def set_drill_format(self, writer: Any, metric: bool) -> None:
        return None

    def generate_drill_files(self, writer: Any, output_directory: str) -> None:
        return None


class StubUtilityAdapter:
    """Standalone utility adapter."""

    def from_mm(self, value: float) -> int:
        return int(value)

    def to_mm(self, value: int) -> float:
        return float(value)

    def get_layer_constants(self) -> Dict[str, int]:
        return {
            "F_Cu": 0,
            "B_Cu": 1,
            "F_SilkS": 2,
            "B_SilkS": 3,
            "F_Mask": 4,
            "B_Mask": 5,
            "F_Paste": 7,
            "B_Paste": 8,
            "Edge_Cuts": 9,
        }

    def get_pcb_constants(self) -> Dict[str, Any]:
        constants = self.get_layer_constants()
        constants.update(
            {
                "PCB_TEXT": object,
                "PCB_SHAPE": object,
                "S_RECT": 0,
            }
        )
        return constants

    def get_no_drill_shape(self) -> int:
        return 0

    def get_plot_format_gerber(self) -> int:
        return 1

    def get_inner_cu_layer(self, layer: int) -> int:
        return layer

    def create_vector2i(self, x: int, y: int) -> Point_Stub:
        return Point_Stub(x, y)

    def create_wx_point(self, x: float, y: float) -> Point_Stub:
        return Point_Stub(x, y)

    def refill_zones(self, board: Any) -> None:
        return None


class StubAdapterSet:
    """Bundle that mimics KicadAdapterSet for standalone mode."""

    def __init__(self, pcbnew: PcbnewStub):
        self.pcbnew = pcbnew
        self.board = StubBoardAdapter(pcbnew)
        self.footprint = StubFootprintAdapter()
        self.gerber = StubGerberAdapter()
        self.utility = StubUtilityAdapter()
        self.version = (8, 0, 1)


def create_adapter_set() -> StubAdapterSet:
    """Create a standalone adapter set compatible with JLCPCBTools."""
    stub = KicadStub()
    return StubAdapterSet(stub.get_pcbnew())
