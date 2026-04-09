"""KiCad IPC transport managed by ``kicad-python`` (``kipy``).

This module handles only connection lifecycle: socket path resolution,
normalisation, and availability checks.  All board/footprint operations
live in ``ipc_impl.py``.
"""

import os
from pathlib import Path
import sys
from typing import Optional
from urllib.parse import unquote, urlparse

from kipy import KiCad
from kipy.errors import ApiError as KiPyApiError, ConnectionError as KiPyConnectionError


class KiCadIPCError(RuntimeError):
    """Raised when an IPC transport or RPC error occurs."""


class KiCadIPCClient:
    """Thin connection manager over the official ``kipy`` KiCad IPC client.

    Callers obtain an open ``kipy.board.Board`` via :meth:`board` and drive
    board operations directly through the kipy API.  All serialisation and
    business logic lives in ``ipc_impl.py``.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        timeout: float = 1.0,
        token: Optional[str] = None,
    ):
        resolved_socket = socket_path or self.default_socket_path()
        self.socket_path = self._normalize_socket_path(resolved_socket)
        self.timeout = timeout
        self.token = token if token is not None else os.getenv("KICAD_API_TOKEN")
        self._kicad: Optional[KiCad] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _kicad_client(self) -> KiCad:
        """Create (or reuse) the ``kipy.KiCad`` transport client."""
        if self._kicad is None:
            timeout_ms = max(1, int(self.timeout * 1000))
            self._kicad = KiCad(
                socket_path=self.socket_path,
                kicad_token=self.token,
                timeout_ms=timeout_ms,
            )
        return self._kicad

    def board(self):
        """Return the active ``kipy.board.Board`` object."""
        return self._kicad_client().get_board()

    def is_available(self) -> bool:
        """Check whether the IPC socket appears reachable."""
        if not self.socket_path:
            return False
        try:
            self._kicad_client().ping()
        except (KiPyConnectionError, KiPyApiError, OSError):
            return False
        return True

    # ------------------------------------------------------------------
    # Export stubs (full kipy export API not yet available)
    # ------------------------------------------------------------------

    def export_gerbers(self, board_file: str, output_dir: str) -> None:
        """Export Gerber files via IPC (not yet implemented — raises)."""
        raise KiCadIPCError("export_gerbers is not yet implemented in the kipy adapter")

    def export_drill(self, board_file: str, output_dir: str) -> None:
        """Export drill files via IPC (not yet implemented — raises)."""
        raise KiCadIPCError("export_drill is not yet implemented in the kipy adapter")

    # ------------------------------------------------------------------
    # Socket path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_socket_path(socket_path: Optional[str]) -> Optional[str]:
        """Normalize KiCad socket settings to the endpoint format ``kipy`` expects.

        KiCad launchers may provide plain filesystem paths or URI-style values
        like ``ipc:///tmp/kicad/api.sock``.
        """
        if not socket_path:
            return None

        value = socket_path.strip()
        if not value:
            return None

        if value.startswith("ipc://"):
            return value

        if value.startswith(("unix://", "file://")):
            parsed = urlparse(value)
            if parsed.path:
                return f"ipc://{unquote(parsed.path)}"
            if parsed.netloc:
                return f"ipc://{unquote(parsed.netloc)}"
            return None

        if value.startswith("unix:"):
            parsed = urlparse(value)
            if parsed.path:
                return f"ipc://{unquote(parsed.path)}"
            remainder = value[len("unix:"):]
            return f"ipc://{unquote(remainder)}" if remainder else None

        if value.startswith("/"):
            return f"ipc://{value}"

        return value

    @staticmethod
    def default_socket_path() -> Optional[str]:
        """Return the default KiCad IPC socket path for the current platform."""
        api_socket = os.getenv("KICAD_API_SOCKET")
        if api_socket:
            return api_socket

        override = os.getenv("KICAD_IPC_SOCKET")
        if override:
            return override

        home = Path.home()
        if sys.platform == "darwin":
            return str(
                home
                / "Library"
                / "Application Support"
                / "kicad"
                / "scripting"
                / "kicad-ipc.sock"
            )
        if sys.platform.startswith("linux"):
            return str(
                home
                / ".local"
                / "share"
                / "kicad"
                / "scripting"
                / "kicad-ipc.sock"
            )
        return None
