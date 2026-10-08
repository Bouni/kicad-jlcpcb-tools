"""Run JLCPCB Tools as a KiCad IPC API plugin (KiCad 10.99 and newer).

``ipc_plugin.py`` selects the IPC backend and calls ``main``. The window and
board logic are the same as the SWIG action plugin; ``ipc_pcbnew`` supplies the
pcbnew objects, and every board edit is one KiCad commit so it can be undone.
"""

# pyright: reportMissingImports=false
# ruff: noqa: UP045

from collections.abc import Callable
import hashlib
import logging
from pathlib import Path
import sys
import traceback
from typing import Any, Optional

import wx  # pylint: disable=import-error

from . import ipc_pcbnew
from .board_context import BoardContextChanged
from .core.version import is_supported_version
from .fabrication import Fabrication
from .mainwindow import JLCPCBTools, KicadProvider

logger = logging.getLogger(__name__)

COMMIT_MESSAGE = "JLCPCB Tools"


class IpcFabrication(Fabrication):
    """Fabrication whose variant plots run in KiCad instead of a SWIG board clone."""

    def _board_content(self) -> bytes:
        """Return the live board as KiCad would save it."""
        return self.board.serialize()

    def _get_plot_board(self) -> Any:
        """Plot the live board in the explicit variant without switching the editor."""
        self._require_output_snapshot()
        self.validate_generation()
        operation = getattr(self, "_generation", None)
        if operation is None:
            return self.board
        if operation.plot_board is None:
            temporary_path = Path(operation.directory.name) / "plot-source.kicad_pcb"
            source_digest = self._serialize_board(temporary_path)
            plot_board = self.board.plot_view(self.variant_name)
            self.validate_generation()
            operation.plot_board = plot_board
            operation.plot_source_digest = source_digest
            operation.plot_project_properties = self._capture_plot_project_properties(
                plot_board
            )
        return operation.plot_board


class IpcKicadProvider(KicadProvider):
    """Provide the IPC pcbnew facade and its fabrication implementation."""

    create_fabrication = IpcFabrication

    def get_pcbnew(self) -> Any:
        """Return the IPC-backed pcbnew module."""
        return ipc_pcbnew


def run_board_action(action: Callable[[], None]) -> None:
    """Apply a board edit as one undoable KiCad commit."""
    board = ipc_pcbnew.GetBoard()
    if board is None:
        raise BoardContextChanged("The PCB editor no longer has an open board")
    with board.commit(COMMIT_MESSAGE):
        action()


def _show_error(message: str) -> None:
    wx.MessageBox(message, "JLCPCB Tools", wx.OK | wx.ICON_ERROR)


def _open_window(app: wx.App) -> Optional[Any]:
    """Validate the editor state and show the main window."""
    ipc_pcbnew.connect()
    version = ipc_pcbnew.GetBuildVersion()
    if not is_supported_version(version):
        _show_error(f"JLCPCB Tools does not support KiCad {version}.")
        return None
    board = ipc_pcbnew.GetBoard()
    if board is None:
        _show_error("Open a board in the PCB Editor before starting JLCPCB Tools.")
        return None
    # KiCad starts a new process per click; keep one window per board.
    key = hashlib.sha256(board.GetFileName().encode("utf-8")).hexdigest()[:16]
    app.instance_checker = wx.SingleInstanceChecker(f"kicad-jlcpcb-tools-{key}")
    if app.instance_checker.IsAnotherRunning():
        _show_error("JLCPCB Tools is already open for this board.")
        return None
    window = JLCPCBTools(
        None, kicad_provider=IpcKicadProvider(), board_action=run_board_action
    )
    window.Center()
    window.Show()
    window.Raise()
    logger.warning(
        "KiCad %s IPC API: editing a footprint rewrites it, which can reset "
        "fab-layer text orientation on rotated parts and drop dimensions or "
        "unit pin maps inside that footprint. Copper, mask, paste and "
        "silkscreen are unaffected.",
        version,
    )
    return window


def main() -> int:
    """Open the JLCPCB Tools window and run until it is closed."""
    app = wx.App(False)
    try:
        if _open_window(app) is None:
            return 1
    except Exception as error:
        logger.exception("JLCPCB Tools failed to start")
        traceback.print_exc(file=sys.stderr)
        _show_error(f"JLCPCB Tools failed to start:\n\n{error}")
        return 1
    app.MainLoop()
    return 0
