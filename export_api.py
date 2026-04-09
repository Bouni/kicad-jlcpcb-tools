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

    def __init__(self, fabrication: Fabrication):
        self.fabrication = fabrication

    def generate_gerbers(self, layer_count: Optional[int] = None) -> None:
        """Generate Gerber files via existing SWIG-backed implementation."""
        self.fabrication._generate_gerber_impl(layer_count)

    def generate_drill_files(self) -> None:
        """Generate drill files via existing SWIG-backed implementation."""
        self.fabrication._generate_excellon_impl()


class IPCExportPlan(ExportPlan):
    """IPC-first export strategy with `kicad-cli` fallback.

    In current migration state, IPC export calls are scaffolded and fallback to
    `kicad-cli` export commands. This class is intentionally not wired as the
    active runtime default yet.
    """

    IPC_EXPORT_MINIMUM_VERSION = (11, 0, 0)

    def __init__(self, fabrication: Fabrication, command_runner=None):
        self.fabrication = fabrication
        self.command_runner = command_runner or subprocess.run

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
        return False

    def _run_ipc_gerber_export(self, _layer_count: Optional[int] = None) -> None:
        """Run direct IPC Gerber export when implemented."""
        raise NotImplementedError("Direct IPC Gerber export not implemented yet")

    def _run_ipc_drill_export(self) -> None:
        """Run direct IPC drill export when implemented."""
        raise NotImplementedError("Direct IPC drill export not implemented yet")

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
