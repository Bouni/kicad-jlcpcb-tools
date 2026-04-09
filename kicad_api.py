"""KiCad SWIG API abstraction layer.

This module provides a facade for all KiCad SWIG interactions, enabling easy
swapping of the deprecated SWIG bindings for the new IPC API in the future.

The module detects the KiCad version at initialization and provides version-aware
wrappers that handle compatibility across v6, v7, v8, and v8.99+ versions.
"""

from abc import ABC, abstractmethod
import importlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    import pcbnew as kicad_pcbnew  # pylint: disable=import-error
except ImportError:
    kicad_pcbnew = None  # type: ignore

ActionPluginBase = (
    kicad_pcbnew.ActionPlugin if kicad_pcbnew is not None else object
)

logger = logging.getLogger(__name__)

IPC_MINIMUM_VERSION = (8, 99, 0)


# Constants for attribute bits
EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def _get_kicad_version() -> str:
    """Get KiCad version string from SWIG bindings.

    Returns:
        Version string (e.g., "8.0.1", "7.0.1-rc1")

    Raises:
        ImportError: If SWIG bindings are not available.

    """
    if kicad_pcbnew is None:
        raise ImportError("KiCad SWIG bindings not available")
    return kicad_pcbnew.GetBuildVersion()


def get_action_plugin_base() -> Any:
    """Return the ActionPlugin base class from KiCad when available."""
    return ActionPluginBase


def _parse_version(version_string: str) -> Tuple[int, int, int]:
    """Parse KiCad version string to (major, minor, patch) tuple.

    Args:
        version_string: Version string like "8.0.1" or "7.0.1-rc1"

    Returns:
        Tuple of (major, minor, patch) integers

    """
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version_string)
    if not match:
        raise ValueError(f"Cannot parse KiCad version: {version_string}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _is_version_at_least(
    version_tuple: Tuple[int, int, int], required: Tuple[int, int, int]
) -> bool:
    """Check if version is at least required version."""
    return version_tuple >= required


def _get_ipc_client_class() -> Any:
    """Import and return the IPC transport client class."""
    return importlib.import_module("ipc_client").KiCadIPCClient


def _get_ipc_adapter_classes() -> Tuple[Any, Any, Any]:
    """Import and return IPC adapter classes for board/footprint/utility."""
    ipc_impl = importlib.import_module("ipc_impl")
    return (
        ipc_impl.IPCBoardAdapter,
        ipc_impl.IPCFootprintAdapter,
        ipc_impl.IPCUtilityAdapter,
    )


def _is_ipc_launch_context() -> bool:
    """Return True when plugin appears to be launched by KiCad IPC runtime."""
    # KiCad IPC plugin launcher sets KICAD_API_SOCKET/KICAD_API_TOKEN.
    # Keep KICAD_IPC_SOCKET as a local dev override for backward compatibility.
    return bool(
        os.getenv("KICAD_API_SOCKET")
        or os.getenv("KICAD_API_TOKEN")
        or os.getenv("KICAD_IPC_SOCKET")
    )


# ============================================================================
# Abstract Base Classes (API Contracts)
# ============================================================================


class BoardAPI(ABC):
    """Abstract interface for KiCad board operations."""

    @abstractmethod
    def get_board(self) -> Any:
        """Get the current PCB board object."""

    @abstractmethod
    def get_board_filename(self) -> str:
        """Get the filename of the current board."""

    @abstractmethod
    def get_all_footprints(self) -> List[Any]:
        """Get all footprints on the board, sorted by reference."""

    @abstractmethod
    def get_footprints(self) -> List[Any]:
        """Get all footprints on the board in board iteration order."""

    @abstractmethod
    def get_footprint_by_reference(self, reference: str) -> Optional[Any]:
        """Find a footprint by its reference designator."""

    @abstractmethod
    def get_enabled_layers(self) -> List[int]:
        """Get list of enabled layer IDs."""

    @abstractmethod
    def get_layer_name(self, layer_id: int) -> str:
        """Get the name of a layer by ID."""

    @abstractmethod
    def get_design_settings(self) -> Any:
        """Get board design settings."""

    @abstractmethod
    def get_drawings(self) -> List[Any]:
        """Get all drawing objects (PCB_TEXT, PCB_SHAPE) on the board."""

    @abstractmethod
    def refresh_display(self) -> None:
        """Refresh the board display in KiCad UI."""

    @abstractmethod
    def get_current_selection(self) -> List[Any]:
        """Get currently selected board items."""

    @abstractmethod
    def get_copper_layer_count(self) -> int:
        """Get number of copper layers in the board stackup."""

    @abstractmethod
    def get_aux_origin(self) -> Any:
        """Get the board auxiliary origin point."""


class FootprintAPI(ABC):
    """Abstract interface for KiCad footprint operations."""

    @abstractmethod
    def get_reference(self, footprint: Any) -> str:
        """Get the reference designator (e.g., 'R1', 'U1')."""

    @abstractmethod
    def get_value(self, footprint: Any) -> str:
        """Get the component value/name."""

    @abstractmethod
    def get_fpid_name(self, footprint: Any) -> str:
        """Get the footprint package name from LIB_ID."""

    @abstractmethod
    def get_layer(self, footprint: Any) -> int:
        """Get layer ID (0=front/F_Cu, 1=back/B_Cu)."""

    @abstractmethod
    def get_orientation(self, footprint: Any) -> float:
        """Get orientation in degrees."""

    @abstractmethod
    def get_position(self, footprint: Any) -> Tuple[float, float]:
        """Get position in board units (typically nanometers)."""

    @abstractmethod
    def get_attributes(self, footprint: Any) -> int:
        """Get attributes bitmask."""

    @abstractmethod
    def set_attributes(self, footprint: Any, attributes: int) -> None:
        """Set attributes bitmask."""

    @abstractmethod
    def get_lcsc_value(self, footprint: Any) -> str:
        """Get LCSC part number (C123456 format), empty if not found."""

    @abstractmethod
    def set_lcsc_value(self, footprint: Any, lcsc: str) -> None:
        """Set LCSC part number."""

    @abstractmethod
    def get_exclude_from_pos(self, footprint: Any) -> bool:
        """Check if footprint is excluded from POS file."""

    @abstractmethod
    def get_exclude_from_bom(self, footprint: Any) -> bool:
        """Check if footprint is excluded from BOM."""

    @abstractmethod
    def get_is_dnp(self, footprint: Any) -> bool:
        """Check if footprint is marked Do Not Place."""

    @abstractmethod
    def set_selected(self, footprint: Any) -> None:
        """Select footprint in UI."""

    @abstractmethod
    def clear_selected(self, footprint: Any) -> None:
        """Deselect footprint in UI."""

    @abstractmethod
    def toggle_exclude_from_pos(self, footprint: Any) -> bool:
        """Toggle exclude-from-POS attribute, return new state."""

    @abstractmethod
    def toggle_exclude_from_bom(self, footprint: Any) -> bool:
        """Toggle exclude-from-BOM attribute, return new state."""

    @abstractmethod
    def get_pads(self, footprint: Any) -> List[Any]:
        """Get list of pads in footprint."""


class GerberAPI(ABC):
    """Abstract interface for Gerber file generation."""

    @abstractmethod
    def create_plot_controller(self, board: Any) -> Any:
        """Create a plot controller for the given board."""

    @abstractmethod
    def get_plot_options(self, plot_controller: Any) -> Any:
        """Get plot options from controller."""

    @abstractmethod
    def set_output_directory(self, plot_options: Any, directory: str) -> None:
        """Set output directory for plots."""

    @abstractmethod
    def set_format(self, plot_options: Any, format_id: int) -> None:
        """Set plot format (1 = Gerber)."""

    @abstractmethod
    def set_plot_component_values(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot component values."""

    @abstractmethod
    def set_plot_reference_designators(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot reference designators."""

    @abstractmethod
    def set_sketch_pads_on_mask_layers(self, plot_options: Any, value: bool) -> None:
        """Set whether to sketch pads on mask layers."""

    @abstractmethod
    def set_use_protel_extensions(self, plot_options: Any, value: bool) -> None:
        """Set whether to use Protel file extensions."""

    @abstractmethod
    def set_create_job_file(self, plot_options: Any, value: bool) -> None:
        """Set whether to create job file."""

    @abstractmethod
    def set_mask_color(self, plot_options: Any, value: bool) -> None:
        """Set mask color option."""

    @abstractmethod
    def set_use_auxiliary_origin(self, plot_options: Any, value: bool) -> None:
        """Set whether to use auxiliary origin."""

    @abstractmethod
    def set_plot_vias_on_mask(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot vias on mask (v8.0+ only)."""

    @abstractmethod
    def set_use_x2_format(self, plot_options: Any, value: bool) -> None:
        """Set whether to use X2 format."""

    @abstractmethod
    def set_include_netlist_attributes(self, plot_options: Any, value: bool) -> None:
        """Set whether to include netlist attributes."""

    @abstractmethod
    def set_disable_macros(self, plot_options: Any, value: bool) -> None:
        """Set whether to disable aperture macros."""

    @abstractmethod
    def set_plot_frame_ref(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot the frame reference/title block."""

    @abstractmethod
    def set_skip_plot_npth_pads(self, plot_options: Any, value: bool) -> None:
        """Set whether NPTH pads should be omitted from plotted output."""

    @abstractmethod
    def set_layer(self, plot_controller: Any, layer_id: int) -> None:
        """Set the current layer to plot."""

    @abstractmethod
    def open_plot_file(
        self, plot_controller: Any, filename: str, extension: str, plot_title: str
    ) -> None:
        """Open a plot file for writing."""

    @abstractmethod
    def plot_layer(self, plot_controller: Any) -> bool:
        """Plot the current layer, return success status."""

    @abstractmethod
    def close_plot(self, plot_controller: Any) -> None:
        """Close the current plot."""

    @abstractmethod
    def set_drill_marks(self, plot_options: Any, mark_type: int) -> None:
        """Set drill mark type."""

    @abstractmethod
    def create_excellon_writer(self, board: Any) -> Any:
        """Create an Excellon drill writer."""

    @abstractmethod
    def set_drill_options(self, writer: Any, **kwargs: Any) -> None:
        """Set drill writer options."""

    @abstractmethod
    def set_drill_format(self, writer: Any, metric: bool) -> None:
        """Set drill output format."""

    @abstractmethod
    def generate_drill_files(self, writer: Any, output_directory: str) -> None:
        """Generate drill files."""


class UtilityAPI(ABC):
    """Abstract interface for utility functions."""

    @abstractmethod
    def from_mm(self, value: float) -> int:
        """Convert millimeters to board units."""

    @abstractmethod
    def to_mm(self, value: int) -> float:
        """Convert board units to millimeters."""

    @abstractmethod
    def get_layer_constants(self) -> Dict[str, int]:
        """Get mapping of layer names to layer IDs."""

    @abstractmethod
    def get_pcb_constants(self) -> Dict[str, Any]:
        """Get KiCad PCB constants and types used by UI/business logic."""

    @abstractmethod
    def get_no_drill_shape(self) -> int:
        """Get no-drill-shape constant for current KiCad version."""

    @abstractmethod
    def get_plot_format_gerber(self) -> int:
        """Get Gerber plot format constant."""

    @abstractmethod
    def get_inner_cu_layer(self, layer: int) -> int:
        """Get layer constant for inner copper layer index."""

    @abstractmethod
    def create_vector2i(self, x: int, y: int) -> Any:
        """Create KiCad VECTOR2I value."""

    @abstractmethod
    def create_wx_point(self, x: float, y: float) -> Any:
        """Create KiCad wxPoint value."""

    @abstractmethod
    def refill_zones(self, board: Any) -> None:
        """Refill all zones on board and refresh UI."""


# ============================================================================
# SWIG Implementation
# ============================================================================


class SWIGBoardAdapter(BoardAPI):
    """KiCad SWIG implementation of BoardAPI."""

    def __init__(self, pcbnew_module: Any) -> None:
        self.pcbnew = pcbnew_module
        self._version = _parse_version(_get_kicad_version())
        logger.debug(
            "SWIGBoardAdapter initialized for KiCad %s.%s.%s",
            self._version[0],
            self._version[1],
            self._version[2],
        )

    def get_board(self) -> Any:
        """Get the current PCB board object."""
        return self.pcbnew.GetBoard()

    def get_board_filename(self) -> str:
        """Get the filename of the current board."""
        return self.get_board().GetFileName()

    def get_all_footprints(self) -> List[Any]:
        """Get all footprints on the board, sorted by reference."""
        board = self.get_board()
        return sorted(board.GetFootprints(), key=lambda x: x.GetReference())

    def get_footprints(self) -> List[Any]:
        """Get all footprints on the board in board iteration order."""
        return list(self.get_board().Footprints())

    def get_footprint_by_reference(self, reference: str) -> Optional[Any]:
        """Find a footprint by its reference designator."""
        board = self.get_board()
        return board.FindFootprintByReference(reference)

    def get_enabled_layers(self) -> List[int]:
        """Get list of enabled layer IDs."""
        board = self.get_board()
        return list(board.GetEnabledLayers().Seq())

    def get_layer_name(self, layer_id: int) -> str:
        """Get the name of a layer by ID."""
        board = self.get_board()
        return str(board.GetLayerName(layer_id))

    def get_design_settings(self) -> Any:
        """Get board design settings."""
        return self.get_board().GetDesignSettings()

    def get_drawings(self) -> List[Any]:
        """Get all drawing objects (PCB_TEXT, PCB_SHAPE) on the board."""
        return list(self.get_board().GetDrawings())

    def refresh_display(self) -> None:
        """Refresh the board display in KiCad UI."""
        self.pcbnew.Refresh()

    def get_current_selection(self) -> List[Any]:
        """Get currently selected board items."""
        return list(self.pcbnew.GetCurrentSelection())

    def get_copper_layer_count(self) -> int:
        """Get number of copper layers in the board stackup."""
        return self.get_board().GetCopperLayerCount()

    def get_aux_origin(self) -> Any:
        """Get the board auxiliary origin point."""
        return self.get_board().GetDesignSettings().GetAuxOrigin()


class SWIGFootprintAdapter(FootprintAPI):
    """KiCad SWIG implementation of FootprintAPI."""

    def __init__(self, pcbnew_module: Any) -> None:
        self.pcbnew = pcbnew_module
        self._version = _parse_version(_get_kicad_version())
        logger.debug(
            "SWIGFootprintAdapter initialized for KiCad %s.%s.%s",
            self._version[0],
            self._version[1],
            self._version[2],
        )

    def get_reference(self, footprint: Any) -> str:
        """Get the reference designator (e.g., 'R1', 'U1')."""
        return footprint.GetReference()

    def get_value(self, footprint: Any) -> str:
        """Get the component value/name."""
        return footprint.GetValue()

    def get_fpid_name(self, footprint: Any) -> str:
        """Get the footprint package name from LIB_ID."""
        return footprint.GetFPID().GetLibItemName()

    def get_layer(self, footprint: Any) -> int:
        """Get layer ID (0=front/F_Cu, 1=back/B_Cu)."""
        return footprint.GetLayer()

    def get_orientation(self, footprint: Any) -> float:
        """Get orientation in degrees."""
        angle = footprint.GetOrientation()
        # v6.99+ returns EDA_ANGLE with AsDegrees() method
        if hasattr(angle, "AsDegrees"):
            return angle.AsDegrees()
        # Pre-v6.99 returns tenths of degrees
        return angle / 10.0

    def get_position(self, footprint: Any) -> Tuple[float, float]:
        """Get position in board units (typically nanometers)."""
        # Try to get position from first pad bounding box (more reliable)
        pads = footprint.Pads()
        if pads:
            bbox = pads[0].GetBoundingBox()
            return (bbox.GetCenter().x, bbox.GetCenter().y)
        # Fallback to footprint center
        return (footprint.GetCenter().x, footprint.GetCenter().y)

    def get_attributes(self, footprint: Any) -> int:
        """Get attributes bitmask."""
        return footprint.GetAttributes()

    def set_attributes(self, footprint: Any, attributes: int) -> None:
        """Set attributes bitmask."""
        footprint.SetAttributes(attributes)

    def get_lcsc_value(self, footprint: Any) -> str:
        """Get LCSC part number (C123456 format), empty if not found."""
        # v7.99+: Use GetFields()
        try:
            for field in footprint.GetFields():
                if re.match(r"lcsc|jlc", field.GetName(), re.IGNORECASE):
                    text = field.GetText()
                    if re.match(r"^C\d+$", text):
                        return text
        # v7 and earlier: Use GetProperties()
        except AttributeError:
            for key, value in footprint.GetProperties().items():
                if re.match(r"lcsc|jlc", key, re.IGNORECASE):
                    if re.match(r"^C\d+$", value):
                        return value
        return ""

    def set_lcsc_value(self, footprint: Any, lcsc: str) -> None:
        """Set LCSC part number."""
        lcsc_field = None
        # Try to find existing LCSC/JLC field
        try:
            for field in footprint.GetFields():
                if re.match(r"lcsc|jlc", field.GetName(), re.IGNORECASE):
                    lcsc_field = field
                    break
        except AttributeError:
            pass

        if lcsc_field:
            footprint.SetField(lcsc_field.GetName(), lcsc)
        else:
            footprint.SetField("LCSC", lcsc)
            # Make field invisible if possible
            if hasattr(footprint, "GetFieldByName"):
                footprint.GetFieldByName("LCSC").SetVisible(False)
            else:
                try:
                    for field in footprint.GetFields():
                        if field.GetName() == "LCSC":
                            field.SetVisible(False)
                            break
                except AttributeError:
                    pass

    def get_exclude_from_pos(self, footprint: Any) -> bool:
        """Check if footprint is excluded from POS file."""
        if not footprint:
            return False
        val = self.get_attributes(footprint)
        return bool(val & (1 << EXCLUDE_FROM_POS))

    def get_exclude_from_bom(self, footprint: Any) -> bool:
        """Check if footprint is excluded from BOM."""
        if not footprint:
            return False
        val = self.get_attributes(footprint)
        return bool(val & (1 << EXCLUDE_FROM_BOM))

    def get_is_dnp(self, footprint: Any) -> bool:
        """Check if footprint is marked Do Not Place."""
        if not footprint:
            return False
        is_dnp = getattr(footprint, "IsDNP", None)
        if not callable(is_dnp):
            return False
        return bool(is_dnp())

    def set_selected(self, footprint: Any) -> None:
        """Select footprint in UI."""
        footprint.SetSelected()

    def clear_selected(self, footprint: Any) -> None:
        """Deselect footprint in UI."""
        footprint.ClearSelected()

    def toggle_exclude_from_pos(self, footprint: Any) -> bool:
        """Toggle exclude-from-POS attribute, return new state."""
        if not footprint:
            return False
        val = self.get_attributes(footprint)
        val ^= 1 << EXCLUDE_FROM_POS
        self.set_attributes(footprint, val)
        return bool(val & (1 << EXCLUDE_FROM_POS))

    def toggle_exclude_from_bom(self, footprint: Any) -> bool:
        """Toggle exclude-from-BOM attribute, return new state."""
        if not footprint:
            return False
        val = self.get_attributes(footprint)
        val ^= 1 << EXCLUDE_FROM_BOM
        self.set_attributes(footprint, val)
        return bool(val & (1 << EXCLUDE_FROM_BOM))

    def get_pads(self, footprint: Any) -> List[Any]:
        """Get list of pads in footprint."""
        return list(footprint.Pads())


class SWIGGerberAdapter(GerberAPI):
    """KiCad SWIG implementation of GerberAPI."""

    def __init__(self, pcbnew_module: Any) -> None:
        self.pcbnew = pcbnew_module
        self._version = _parse_version(_get_kicad_version())
        logger.debug(
            "SWIGGerberAdapter initialized for KiCad %s.%s.%s",
            self._version[0],
            self._version[1],
            self._version[2],
        )

    def create_plot_controller(self, board: Any) -> Any:
        """Create a plot controller for the given board."""
        return self.pcbnew.PLOT_CONTROLLER(board)

    def get_plot_options(self, plot_controller: Any) -> Any:
        """Get plot options from controller."""
        return plot_controller.GetPlotOptions()

    def set_output_directory(self, plot_options: Any, directory: str) -> None:
        """Set output directory for plots."""
        plot_options.SetOutputDirectory(directory)

    def set_format(self, plot_options: Any, format_id: int) -> None:
        """Set plot format (1 = Gerber)."""
        plot_options.SetFormat(format_id)

    def set_plot_component_values(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot component values."""
        plot_options.SetPlotValue(value)

    def set_plot_reference_designators(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot reference designators."""
        plot_options.SetPlotReference(value)

    def set_sketch_pads_on_mask_layers(self, plot_options: Any, value: bool) -> None:
        """Set whether to sketch pads on mask layers."""
        if hasattr(plot_options, "SetSketchPadsOnMaskLayers"):
            plot_options.SetSketchPadsOnMaskLayers(value)
        elif hasattr(plot_options, "SetSketchPadsOnFabLayers"):
            plot_options.SetSketchPadsOnFabLayers(value)

    def set_use_protel_extensions(self, plot_options: Any, value: bool) -> None:
        """Set whether to use Protel file extensions."""
        if hasattr(plot_options, "SetUseProtelExtensions"):
            plot_options.SetUseProtelExtensions(value)
        elif hasattr(plot_options, "SetUseGerberProtelExtensions"):
            plot_options.SetUseGerberProtelExtensions(value)

    def set_create_job_file(self, plot_options: Any, value: bool) -> None:
        """Set whether to create job file."""
        if hasattr(plot_options, "SetCreateJobFile"):
            plot_options.SetCreateJobFile(value)
        elif hasattr(plot_options, "SetCreateGerberJobFile"):
            plot_options.SetCreateGerberJobFile(value)

    def set_mask_color(self, plot_options: Any, value: bool) -> None:
        """Set mask color option."""
        if hasattr(plot_options, "SetMaskColor"):
            plot_options.SetMaskColor(value)
        elif hasattr(plot_options, "SetSubtractMaskFromSilk"):
            plot_options.SetSubtractMaskFromSilk(value)

    def set_use_auxiliary_origin(self, plot_options: Any, value: bool) -> None:
        """Set whether to use auxiliary origin."""
        plot_options.SetUseAuxOrigin(value)

    def set_plot_vias_on_mask(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot vias on mask (v8.0+ only)."""
        # This method only exists in v8.0+
        if hasattr(plot_options, "SetPlotViaOnMaskLayer"):
            plot_options.SetPlotViaOnMaskLayer(value)
        else:
            logger.debug("SetPlotViaOnMaskLayer not available in this KiCad version")

    def set_use_x2_format(self, plot_options: Any, value: bool) -> None:
        """Set whether to use X2 format."""
        if hasattr(plot_options, "SetUseX2Format"):
            plot_options.SetUseX2Format(value)
        elif hasattr(plot_options, "SetUseGerberX2format"):
            plot_options.SetUseGerberX2format(value)

    def set_include_netlist_attributes(self, plot_options: Any, value: bool) -> None:
        """Set whether to include netlist attributes."""
        if hasattr(plot_options, "SetIncludeNetlistAttributes"):
            plot_options.SetIncludeNetlistAttributes(value)
        elif hasattr(plot_options, "SetIncludeGerberNetlistInfo"):
            plot_options.SetIncludeGerberNetlistInfo(value)

    def set_disable_macros(self, plot_options: Any, value: bool) -> None:
        """Set whether to disable aperture macros."""
        if hasattr(plot_options, "SetDisableApertureMacros"):
            plot_options.SetDisableApertureMacros(value)
        elif hasattr(plot_options, "SetDisableGerberMacros"):
            plot_options.SetDisableGerberMacros(value)

    def set_plot_frame_ref(self, plot_options: Any, value: bool) -> None:
        """Set whether to plot the frame reference/title block."""
        plot_options.SetPlotFrameRef(value)

    def set_skip_plot_npth_pads(self, plot_options: Any, value: bool) -> None:
        """Set whether NPTH pads should be omitted from plotted output."""
        plot_options.SetSkipPlotNPTH_Pads(value)

    def set_layer(self, plot_controller: Any, layer_id: int) -> None:
        """Set the current layer to plot."""
        plot_controller.SetLayer(layer_id)

    def open_plot_file(
        self, plot_controller: Any, filename: str, extension: str, plot_title: str
    ) -> None:
        """Open a plot file for writing."""
        plot_controller.OpenPlotfile(
            filename, self.pcbnew.PLOT_FORMAT_GERBER, plot_title
        )

    def plot_layer(self, plot_controller: Any) -> bool:
        """Plot the current layer, return success status."""
        return plot_controller.PlotLayer()

    def close_plot(self, plot_controller: Any) -> None:
        """Close the current plot."""
        plot_controller.ClosePlot()

    def set_drill_marks(self, plot_options: Any, mark_type: int) -> None:
        """Set drill mark type."""
        plot_options.SetDrillMarksType(mark_type)

    def create_excellon_writer(self, board: Any) -> Any:
        """Create an Excellon drill writer."""
        return self.pcbnew.EXCELLON_WRITER(board)

    def set_drill_options(self, writer: Any, **kwargs: Any) -> None:
        """Set drill writer options."""
        # Excellon writer options are set via DRILL_MARKS constants and methods
        for key, value in kwargs.items():
            setter_name = f"Set{key}"
            if hasattr(writer, setter_name):
                setter = getattr(writer, setter_name)
                if isinstance(value, (tuple, list)):
                    setter(*value)
                else:
                    setter(value)

    def set_drill_format(self, writer: Any, metric: bool) -> None:
        """Set drill output format."""
        writer.SetFormat(metric)

    def generate_drill_files(self, writer: Any, output_directory: str) -> None:
        """Generate drill files."""
        writer.CreateDrillandMapFilesSet(output_directory, True, False)


class SWIGUtilityAdapter(UtilityAPI):
    """KiCad SWIG implementation of UtilityAPI."""

    def __init__(self, pcbnew_module: Any) -> None:
        self.pcbnew = pcbnew_module
        self._version = _parse_version(_get_kicad_version())
        logger.debug(
            "SWIGUtilityAdapter initialized for KiCad %s.%s.%s",
            self._version[0],
            self._version[1],
            self._version[2],
        )

    def from_mm(self, value: float) -> int:
        """Convert millimeters to board units."""
        return self.pcbnew.FromMM(value)

    def to_mm(self, value: int) -> float:
        """Convert board units to millimeters."""
        return self.pcbnew.ToMM(value)

    def get_layer_constants(self) -> Dict[str, int]:
        """Get mapping of layer names to layer IDs."""
        return {
            "F_Cu": self.pcbnew.F_Cu,
            "B_Cu": self.pcbnew.B_Cu,
            "F_SilkS": self.pcbnew.F_SilkS,
            "B_SilkS": self.pcbnew.B_SilkS,
            "F_Mask": self.pcbnew.F_Mask,
            "B_Mask": self.pcbnew.B_Mask,
            "F_Paste": self.pcbnew.F_Paste,
            "B_Paste": self.pcbnew.B_Paste,
            "Edge_Cuts": self.pcbnew.Edge_Cuts,
        }

    def get_pcb_constants(self) -> Dict[str, Any]:
        """Get KiCad PCB constants and types used by UI/business logic."""
        constants = self.get_layer_constants()
        constants.update(
            {
                "PCB_TEXT": self.pcbnew.PCB_TEXT,
                "PCB_SHAPE": self.pcbnew.PCB_SHAPE,
                "S_RECT": self.pcbnew.S_RECT,
            }
        )
        return constants

    def get_no_drill_shape(self) -> int:
        """Get no-drill-shape constant for current KiCad version."""
        if hasattr(self.pcbnew, "DRILL_MARKS_NO_DRILL_SHAPE"):
            return self.pcbnew.DRILL_MARKS_NO_DRILL_SHAPE
        return self.pcbnew.PCB_PLOT_PARAMS.NO_DRILL_SHAPE

    def get_plot_format_gerber(self) -> int:
        """Get Gerber plot format constant."""
        return self.pcbnew.PLOT_FORMAT_GERBER

    def get_inner_cu_layer(self, layer: int) -> int:
        """Get layer constant for inner copper layer index."""
        return getattr(self.pcbnew, f"In{layer}_Cu")

    def create_vector2i(self, x: int, y: int) -> Any:
        """Create KiCad VECTOR2I value."""
        return self.pcbnew.VECTOR2I(x, y)

    def create_wx_point(self, x: float, y: float) -> Any:
        """Create KiCad wxPoint value."""
        return self.pcbnew.wxPoint(x, y)

    def refill_zones(self, board: Any) -> None:
        """Refill all zones on board and refresh UI."""
        filler = self.pcbnew.ZONE_FILLER(board)
        filler.Fill(board.Zones())
        self.pcbnew.Refresh()


# ============================================================================
# Adapter Bundle & Provider Factory
# ============================================================================


class KicadAdapterSet:
    """Bundle of all KiCad adapters."""

    def __init__(
        self,
        board: BoardAPI,
        footprint: FootprintAPI,
        gerber: GerberAPI,
        utility: UtilityAPI,
        pcbnew_module: Any,
        version: Tuple[int, int, int],
    ) -> None:
        self.board = board
        self.footprint = footprint
        self.gerber = gerber
        self.utility = utility
        self.pcbnew = pcbnew_module
        self.version = version
        logger.info(
            "KicadAdapterSet initialized for KiCad %s.%s.%s",
            version[0],
            version[1],
            version[2],
        )


class KicadProvider:
    """Factory for creating KiCad adapter sets."""

    @staticmethod
    def create_adapter_set(prefer_ipc: Optional[bool] = None) -> KicadAdapterSet:
        """Create and initialize adapter set for current KiCad version.

        Args:
            prefer_ipc: Backend selection override.
                - None (default): auto-select IPC only in IPC launch context.
                - True: force IPC attempt regardless of launch context.
                - False: force SWIG.

        Returns:
            KicadAdapterSet with all adapters initialized and ready to use

        Raises:
            ImportError: If SWIG bindings are not available
            ValueError: If version cannot be parsed

        """
        if kicad_pcbnew is None:
            logger.error("KiCad SWIG bindings not available")
            raise ImportError("KiCad SWIG bindings not available")

        version_string = _get_kicad_version()
        version_tuple = _parse_version(version_string)
        logger.info("Detected KiCad version: %s", version_string)

        gerber_adapter = SWIGGerberAdapter(kicad_pcbnew)

        if prefer_ipc is None:
            prefer_ipc = _is_ipc_launch_context()

        use_ipc = prefer_ipc and _is_version_at_least(version_tuple, IPC_MINIMUM_VERSION)
        if use_ipc:
            try:
                ipc_client_class = _get_ipc_client_class()
                ipc_board_adapter, ipc_footprint_adapter, ipc_utility_adapter = (
                    _get_ipc_adapter_classes()
                )
                ipc_client = ipc_client_class()
                if ipc_client.is_available():
                    logger.info("Using IPC adapters for KiCad %s", version_string)
                    return KicadAdapterSet(
                        board=ipc_board_adapter(ipc_client),
                        footprint=ipc_footprint_adapter(ipc_client),
                        gerber=gerber_adapter,
                        utility=ipc_utility_adapter(ipc_client),
                        pcbnew_module=kicad_pcbnew,
                        version=version_tuple,
                    )
                logger.info("KiCad IPC server unavailable; falling back to SWIG adapters")
            except Exception as exc:
                logger.warning(
                    "IPC adapter initialization failed; falling back to SWIG: %s", exc
                )
        elif prefer_ipc and not _is_version_at_least(version_tuple, IPC_MINIMUM_VERSION):
            logger.info(
                "IPC requested but KiCad %s is below minimum supported version %s.%s.%s; "
                "using SWIG adapters",
                version_string,
                IPC_MINIMUM_VERSION[0],
                IPC_MINIMUM_VERSION[1],
                IPC_MINIMUM_VERSION[2],
            )
        else:
            logger.info("Using SWIG adapters (IPC launch context not detected)")

        # Create SWIG adapter instances
        board_adapter = SWIGBoardAdapter(kicad_pcbnew)
        footprint_adapter = SWIGFootprintAdapter(kicad_pcbnew)
        utility_adapter = SWIGUtilityAdapter(kicad_pcbnew)

        return KicadAdapterSet(
            board=board_adapter,
            footprint=footprint_adapter,
            gerber=gerber_adapter,
            utility=utility_adapter,
            pcbnew_module=kicad_pcbnew,
            version=version_tuple,
        )
