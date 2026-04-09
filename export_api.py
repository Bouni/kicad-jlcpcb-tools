"""Backend-neutral export plan abstraction.

This module introduces an explicit export strategy boundary so fabrication export
logic can transition from SWIG-specific behavior to IPC-backed implementations
without changing call sites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import subprocess
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fabrication import Fabrication


class ExportPlan(ABC):
    """Abstract export strategy for Gerber and drill generation."""

    @abstractmethod
    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber output files."""

    @abstractmethod
    def generate_drill_files(self) -> None:
        """Generate drill output files."""


class SWIGExportPlan(ExportPlan):
    """Current SWIG-backed export strategy.

    This is intentionally a mechanical extraction with no behavior change. It
    delegates to Fabrication's existing implementation internals.
    """

    def __init__(self, fabrication: Fabrication, gerber_adapter=None):
        self.fabrication = fabrication
        self._gerber_adapter = gerber_adapter

    def _resolve_gerber(self):
        """Lazily resolve and cache the gerber adapter.

        Resolution order:
        1. Constructor-injected adapter (tests / explicit override).
        2. Duck-typed ``fabrication.kicad.gerber`` (standalone shims and
           legacy test adapter-set fixtures that still carry a ``.gerber``
           attribute).
        3. Production path: create a fresh ``SWIGGerberAdapter`` from the raw
           ``pcbnew`` module stored on the adapter set.
        """
        if self._gerber_adapter is not None:
            return self._gerber_adapter
        # Backward-compat / test-injection path
        kicad_gerber = getattr(self.fabrication.kicad, "gerber", None)
        if kicad_gerber is not None:
            self._gerber_adapter = kicad_gerber
            return self._gerber_adapter
        # Production path: build our own adapter from the raw pcbnew module
        from kicad_api import SWIGGerberAdapter  # pylint: disable=import-outside-toplevel

        self._gerber_adapter = SWIGGerberAdapter(self.fabrication.kicad.pcbnew)
        return self._gerber_adapter

    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber files via SWIG adapter-backed export flow."""
        kicad = self.fabrication.kicad
        gerber = self._resolve_gerber()
        layers = kicad.utility.get_layer_constants()
        pctl = gerber.create_plot_controller(self.fabrication.board)
        popt = gerber.get_plot_options(pctl)

        gerber.set_output_directory(popt, self.fabrication.gerberdir)
        gerber.set_format(popt, 1)

        # General options
        gerber.set_plot_component_values(
            popt,
            self.fabrication.parent.settings.get("gerber", {}).get("plot_values", True),
        )
        gerber.set_plot_reference_designators(
            popt,
            self.fabrication.parent.settings.get("gerber", {}).get(
                "plot_references", True
            ),
        )
        gerber.set_sketch_pads_on_mask_layers(popt, False)

        # Gerber options
        gerber.set_use_protel_extensions(popt, False)
        gerber.set_create_job_file(popt, False)
        gerber.set_mask_color(popt, True)
        gerber.set_use_auxiliary_origin(popt, True)
        gerber.set_plot_vias_on_mask(
            popt,
            not self.fabrication.parent.settings.get("gerber", {}).get(
                "tented_vias", True
            ),
        )
        gerber.set_use_x2_format(popt, True)
        gerber.set_include_netlist_attributes(popt, True)
        gerber.set_disable_macros(popt, False)
        gerber.set_drill_marks(popt, kicad.utility.get_no_drill_shape())
        gerber.set_plot_frame_ref(popt, False)

        for filename in os.listdir(self.fabrication.gerberdir):
            os.remove(os.path.join(self.fabrication.gerberdir, filename))

        if not layer_count:
            layer_count = kicad.board.get_copper_layer_count()

        plot_plan = self._build_plot_plan(layer_count)
        for layer_info in plot_plan:
            if layer_info[1] <= layers["B_Cu"]:
                gerber.set_skip_plot_npth_pads(popt, True)
            else:
                gerber.set_skip_plot_npth_pads(popt, False)

            gerber.set_layer(pctl, layer_info[1])
            gerber.open_plot_file(
                pctl,
                layer_info[0],
                kicad.utility.get_plot_format_gerber(),
                layer_info[2],
            )
            plotted = gerber.plot_layer(pctl)
            if plotted is False:
                self.fabrication.logger.error("Error plotting %s", layer_info[2])
            self.fabrication.logger.info("Successfully plotted %s", layer_info[2])

        gerber.close_plot(pctl)

    def generate_drill_files(self) -> None:
        """Generate drill files via SWIG adapter-backed export flow."""
        kicad = self.fabrication.kicad
        gerber = self._resolve_gerber()
        drlwriter = gerber.create_excellon_writer(self.fabrication.board)
        mirror = False
        minimal_header = False
        offset = kicad.board.get_aux_origin()
        merge_npth = False
        gerber.set_drill_options(
            drlwriter,
            Options=(mirror, minimal_header, offset, merge_npth),
        )
        gerber.set_drill_format(drlwriter, False)
        gerber.generate_drill_files(drlwriter, self.fabrication.gerberdir)
        self.fabrication.logger.info("Finished generating Excellon files")

    def _build_plot_plan(self, layer_count: int) -> list[tuple[str, int, str]]:
        """Build the layer plot plan used for Gerber generation."""
        kicad = self.fabrication.kicad
        layers = kicad.utility.get_layer_constants()

        plot_plan_top = [
            ("CuTop", layers["F_Cu"], "Top layer"),
            ("SilkTop", layers["F_SilkS"], "Silk top"),
            ("MaskTop", layers["F_Mask"], "Mask top"),
            ("PasteTop", layers["F_Paste"], "Paste top"),
        ]
        plot_plan_bottom = [
            ("CuBottom", layers["B_Cu"], "Bottom layer"),
            ("SilkBottom", layers["B_SilkS"], "Silk bottom"),
            ("MaskBottom", layers["B_Mask"], "Mask bottom"),
            ("EdgeCuts", layers["Edge_Cuts"], "Edges"),
            ("PasteBottom", layers["B_Paste"], "Paste bottom"),
        ]

        if layer_count == 1:
            plot_plan = plot_plan_top + plot_plan_bottom[-2:]
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (
                        f"CuIn{layer}",
                        kicad.utility.get_inner_cu_layer(layer),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        enabled_layer_ids = kicad.board.get_enabled_layers()
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = kicad.board.get_layer_name(enabled_layer_id).upper()
            if "JLC_" in layer_name_string:
                plot_plan.append(
                    (layer_name_string, enabled_layer_id, layer_name_string)
                )

        return plot_plan


class IPCExportPlan(ExportPlan):
    """IPC-first export strategy with `kicad-cli` fallback.

    In current migration state, IPC export calls are scaffolded and fallback to
    `kicad-cli` export commands. This class is intentionally not wired as the
    active runtime default yet.
    """

    IPC_EXPORT_MINIMUM_VERSION = (11, 0, 0)

    def __init__(
        self,
        fabrication: Fabrication,
        command_runner=None,
        ipc_client=None,
    ):
        self.fabrication = fabrication
        self.command_runner = command_runner or subprocess.run
        self.ipc_client = (
            ipc_client if ipc_client is not None else self._create_ipc_client()
        )

    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber outputs via IPC when available, else `kicad-cli`."""
        self._ensure_supported_version()
        if self._ipc_export_available():
            try:
                self._run_ipc_gerber_export(layer_count)
                return
            except Exception:
                pass
        self._run_cli_gerber_export()

    def generate_drill_files(self) -> None:
        """Generate drill outputs via IPC when available, else `kicad-cli`."""
        self._ensure_supported_version()
        if self._ipc_export_available():
            try:
                self._run_ipc_drill_export()
                return
            except Exception:
                pass
        self._run_cli_drill_export()

    def _ensure_supported_version(self) -> None:
        version = getattr(getattr(self.fabrication, "kicad", None), "version", None)
        if not version or tuple(version) < self.IPC_EXPORT_MINIMUM_VERSION:
            minimum = ".".join(str(v) for v in self.IPC_EXPORT_MINIMUM_VERSION)
            raise RuntimeError(
                f"IPC export requires KiCad >= {minimum}; use SWIGExportPlan on older versions"
            )

    def _ipc_export_available(self) -> bool:
        """Return whether direct IPC export implementation is available."""
        if self.ipc_client is None:
            return False
        is_available = getattr(self.ipc_client, "is_available", None)
        if not callable(is_available):
            return False
        try:
            return bool(is_available())
        except Exception:
            return False

    def _run_ipc_gerber_export(self, _layer_count: Optional[int] = None) -> None:
        """Run direct IPC Gerber export when available."""
        if self.ipc_client is None:
            raise RuntimeError("IPC client not available")
        self.ipc_client.export_gerbers(
            board_file=self.fabrication.board.GetFileName(),
            output_dir=self.fabrication.gerberdir,
        )

    def _run_ipc_drill_export(self) -> None:
        """Run direct IPC drill export when available."""
        if self.ipc_client is None:
            raise RuntimeError("IPC client not available")
        self.ipc_client.export_drill(
            board_file=self.fabrication.board.GetFileName(),
            output_dir=self.fabrication.gerberdir,
        )

    @staticmethod
    def _create_ipc_client():
        """Create IPC client instance when transport module is available."""
        try:
            from ipc_client import KiCadIPCClient

            return KiCadIPCClient()
        except Exception:
            return None

    def _run_cli_gerber_export(self) -> None:
        board_file = self.fabrication.board.GetFileName()
        output_dir = self.fabrication.gerberdir
        self.command_runner(
            [
                "kicad-cli",
                "pcb",
                "export",
                "gerbers",
                "--output",
                output_dir,
                board_file,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _run_cli_drill_export(self) -> None:
        board_file = self.fabrication.board.GetFileName()
        output_dir = self.fabrication.gerberdir
        self.command_runner(
            [
                "kicad-cli",
                "pcb",
                "export",
                "drill",
                "--output",
                output_dir,
                board_file,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def _is_ipc_launch_context() -> bool:
    """Return True when running under KiCad IPC plugin launcher context."""
    return bool(
        os.getenv("KICAD_API_SOCKET")
        or os.getenv("KICAD_API_TOKEN")
        or os.getenv("KICAD_IPC_SOCKET")
    )


def create_export_plan(
    fabrication: Fabrication, prefer_ipc: Optional[bool] = None
) -> ExportPlan:
    """Create an export plan with safe defaults.

    Selection policy:
    - `prefer_ipc is None`: auto-detect from IPC launch context env vars.
    - `prefer_ipc is True`: prefer IPC plan when KiCad version is supported.
    - Otherwise use SWIG plan.

    IPC plan selection is version-gated to avoid runtime failures on versions
    without IPC export support.
    """
    if prefer_ipc is None:
        prefer_ipc = _is_ipc_launch_context()

    version = getattr(getattr(fabrication, "kicad", None), "version", None)
    if (
        prefer_ipc
        and version
        and tuple(version) >= IPCExportPlan.IPC_EXPORT_MINIMUM_VERSION
    ):
        return IPCExportPlan(fabrication)

    return SWIGExportPlan(fabrication)
