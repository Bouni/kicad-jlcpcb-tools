"""Backend-neutral export plan abstraction.

This module introduces an explicit export strategy boundary so fabrication export
logic can transition from SWIG-specific behavior to IPC-backed implementations
without changing call sites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fabrication import Fabrication


logger = logging.getLogger(__name__)


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
        self._requested_layer_count = layer_count
        if self._supports_direct_ipc_export() and self._ipc_export_available():
            try:
                self._run_ipc_gerber_export(layer_count)
                return
            except Exception:
                logger.exception("IPC gerber export failed; falling back to kicad-cli")
        self._run_cli_gerber_export()

    def generate_drill_files(self) -> None:
        """Generate drill outputs via IPC when available, else `kicad-cli`."""
        if self._supports_direct_ipc_export() and self._ipc_export_available():
            try:
                self._run_ipc_drill_export()
                return
            except Exception:
                logger.exception("IPC drill export failed; falling back to kicad-cli")
        self._run_cli_drill_export()

    def _supports_direct_ipc_export(self) -> bool:
        """Return whether direct IPC export endpoints are expected to exist."""
        version = getattr(getattr(self.fabrication, "kicad", None), "version", None)
        return bool(version and tuple(version) >= self.IPC_EXPORT_MINIMUM_VERSION)

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
        board_file = self._board_filename()
        self.ipc_client.export_gerbers(
            board_file=board_file,
            output_dir=self.fabrication.gerberdir,
        )

    def _run_ipc_drill_export(self) -> None:
        """Run direct IPC drill export when available."""
        if self.ipc_client is None:
            raise RuntimeError("IPC client not available")
        board_file = self._board_filename()
        self.ipc_client.export_drill(
            board_file=board_file,
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
        board_file = os.path.abspath(self._board_filename())
        output_dir = os.path.abspath(self.fabrication.gerberdir)
        if not os.path.isfile(board_file):
            raise RuntimeError(
                "Board file does not exist or is not accessible: "
                f"{board_file}. "
                "If KiCad provides a relative board path in IPC mode, set KIPRJMOD "
                "or open the board from its project directory."
            )
        self._clear_output_directory(output_dir)
        command = self._cli_gerber_command(board_file=board_file, output_dir=output_dir)
        self._run_cli_command(
            command,
            cwd=os.path.dirname(board_file) or None,
        )

    def _run_cli_drill_export(self) -> None:
        board_file = os.path.abspath(self._board_filename())
        output_dir = os.path.abspath(self.fabrication.gerberdir)
        if not os.path.isfile(board_file):
            raise RuntimeError(
                "Board file does not exist or is not accessible: "
                f"{board_file}. "
                "If KiCad provides a relative board path in IPC mode, set KIPRJMOD "
                "or open the board from its project directory."
            )
        self._run_cli_command(
            [
                self._resolve_kicad_cli(),
                "pcb",
                "export",
                "drill",
                "--output",
                output_dir,
                board_file,
            ],
            cwd=os.path.dirname(board_file) or None,
        )

    def _run_cli_command(self, command: list[str], cwd: Optional[str] = None) -> None:
        """Run a kicad-cli command with consistent options and error mapping."""
        try:
            self.command_runner(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            tried = ", ".join(self._candidate_kicad_cli_paths())
            raise RuntimeError(
                "Could not find kicad-cli executable. "
                "Set KICAD_CLI_PATH or settings['gerber']['kicad_cli_path']. "
                f"Tried: {tried}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            details = stderr or stdout or str(exc)
            raise RuntimeError(f"kicad-cli export failed: {details}") from exc

    def _resolve_kicad_cli(self) -> str:
        """Resolve path to `kicad-cli` for sandboxed KiCad plugin runtimes."""
        for candidate in self._candidate_kicad_cli_paths():
            if candidate == "kicad-cli":
                if shutil.which("kicad-cli"):
                    return "kicad-cli"
                continue
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return "kicad-cli"

    def _candidate_kicad_cli_paths(self) -> list[str]:
        """Return ordered candidate paths for `kicad-cli` resolution."""
        settings = getattr(getattr(self.fabrication, "parent", None), "settings", {})
        gerber_settings = settings.get("gerber", {}) if isinstance(settings, dict) else {}
        configured_path = (
            os.getenv("KICAD_CLI_PATH")
            or gerber_settings.get("kicad_cli_path")
            or ""
        )

        candidates: list[str] = []
        if configured_path:
            candidates.append(configured_path)

        candidates.append("kicad-cli")

        # Standard macOS app bundle paths.
        candidates.extend(
            [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/Applications/KiCad/nightly/KiCad.app/Contents/MacOS/kicad-cli",
            ]
        )

        # Derive app bundle path from Python executable when running inside KiCad.
        python_exe = os.path.realpath(sys.executable)
        marker = ".app/Contents/"
        marker_index = python_exe.find(marker)
        if marker_index != -1:
            app_contents = python_exe[: marker_index + len(marker)]
            candidates.append(os.path.join(app_contents, "MacOS", "kicad-cli"))

        # Preserve order but remove duplicates.
        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    @staticmethod
    def _clear_output_directory(output_dir: str) -> None:
        """Remove existing files from output directory before generating Gerbers."""
        if not os.path.isdir(output_dir):
            return
        for filename in os.listdir(output_dir):
            file_path = os.path.join(output_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)

    def _cli_gerber_command(self, board_file: str, output_dir: str) -> list[str]:
        """Build the constrained `kicad-cli` Gerber export command."""
        settings = getattr(getattr(self.fabrication, "parent", None), "settings", {})
        gerber_settings = settings.get("gerber", {}) if isinstance(settings, dict) else {}

        command = [
            self._resolve_kicad_cli(),
            "pcb",
            "export",
            "gerbers",
            "--output",
            output_dir,
            "--layers",
            ",".join(self._cli_gerber_layers()),
            "--no-protel-ext",
            "--use-drill-file-origin",
        ]

        if not gerber_settings.get("plot_references", True):
            command.append("--exclude-refdes")
        if not gerber_settings.get("plot_values", True):
            command.append("--exclude-value")
        if not gerber_settings.get("tented_vias", True):
            command.append("--subtract-soldermask")

        command.append(board_file)
        return command

    def _cli_gerber_layers(self) -> list[str]:
        """Return a constrained CLI layer list equivalent to legacy SWIG behavior."""
        layer_count = getattr(self, "_requested_layer_count", None)
        if not layer_count:
            layer_count = getattr(self.fabrication, "layer_count", None)
        if not layer_count:
            layer_count = self.fabrication.kicad.board.get_copper_layer_count()

        layer_count = int(layer_count)

        top_layers = ["F.Cu", "F.SilkS", "F.Mask", "F.Paste"]
        bottom_layers = ["B.Cu", "B.SilkS", "B.Mask", "Edge.Cuts", "B.Paste"]

        if layer_count == 1:
            layers = top_layers + bottom_layers[-2:]
        elif layer_count == 2:
            layers = top_layers + bottom_layers
        else:
            inner_layers = [f"In{idx}.Cu" for idx in range(1, layer_count - 1)]
            layers = top_layers + inner_layers + bottom_layers

        for enabled_layer_id in self.fabrication.kicad.board.get_enabled_layers():
            raw_name = str(self.fabrication.kicad.board.get_layer_name(enabled_layer_id))
            cli_name = self._to_cli_layer_name(raw_name)
            if "JLC_" in raw_name.upper() and cli_name not in layers:
                layers.append(cli_name)

        return layers

    @staticmethod
    def _to_cli_layer_name(layer_name: str) -> str:
        """Convert adapter layer naming to `kicad-cli` untranslated layer names."""
        normalized = layer_name.strip()
        if not normalized:
            return normalized

        if normalized.lower() == "undefined":
            return ""

        match = re.match(r"^In-?(\d+)[._]Cu$", normalized, flags=re.IGNORECASE)
        if match:
            return f"In{int(match.group(1))}.Cu"

        normalized = normalized.replace("_", ".")
        return normalized

    def _board_filename(self) -> str:
        """Resolve board filename across old/new fabrication test doubles."""
        getter = getattr(self.fabrication, "get_board_filename", None)
        if callable(getter):
            return getter()

        board = getattr(self.fabrication, "board", None)
        if hasattr(board, "GetFileName"):
            return board.GetFileName()
        if isinstance(board, dict):
            return str(board.get("path", ""))
        return ""


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
    - `prefer_ipc is True`: prefer IPC plan in IPC runtime context.
    - Otherwise use SWIG plan.

    Note: `IPCExportPlan` can safely run on older versions by falling back to
    `kicad-cli` when direct IPC export endpoints are unavailable.
    """
    if prefer_ipc is None:
        prefer_ipc = _is_ipc_launch_context()

    if prefer_ipc:
        return IPCExportPlan(fabrication)

    return SWIGExportPlan(fabrication)
