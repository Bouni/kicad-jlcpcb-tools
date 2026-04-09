"""IPC plugin entry point for KiCad 9.0+.

This module serves as the main entry point when the plugin is launched by KiCad
through the IPC plugin system. It:

1. Reads KICAD_API_SOCKET and KICAD_API_TOKEN from environment variables
2. Initializes the IPC client
3. Creates adapter set using the IPC backend
4. Launches the main window UI

"""

import importlib
import importlib.util
import logging
import os
import sys
import time

from ipc_client import KiCadIPCClient
from kicad_api import KicadProvider

logger = logging.getLogger(__name__)


def _import_mainwindow(plugin_dir: str):
    """Import JLCPCBTools with proper package context so relative imports work.

    When this module runs as a standalone script the plugin directory lands on
    sys.path as a plain directory, not as a package.  ``mainwindow.py`` (and
    most of the codebase) use relative imports (``from .corrections import …``),
    which require the directory to be registered as a package in sys.modules.
    We register it under a Python-safe alias derived from the directory name
    (hyphens → underscores) so all intra-package relative imports resolve.
    """
    pkg_name = os.path.basename(plugin_dir).replace("-", "_")

    if pkg_name not in sys.modules:
        init_path = os.path.join(plugin_dir, "__init__.py")
        spec = importlib.util.spec_from_file_location(
            pkg_name,
            init_path,
            submodule_search_locations=[plugin_dir],
        )
        pkg_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        pkg_mod.__path__ = [plugin_dir]  # type: ignore[assignment]
        sys.modules[pkg_name] = pkg_mod
        try:
            spec.loader.exec_module(pkg_mod)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            # __init__.py registers the SWIG plugin which isn't available here;
            # the ImportError is expected and harmless.
            pass

    mainwindow_mod = importlib.import_module(f"{pkg_name}.mainwindow")
    return mainwindow_mod.JLCPCBTools


def _wait_for_ipc(client: KiCadIPCClient, timeout_s: float = 8.0) -> bool:
    """Wait briefly for KiCad IPC endpoint to become connectable."""
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        if client.is_available():
            return True
        time.sleep(0.2)

    return client.is_available()


def main() -> int:
    """Launch the IPC plugin.

    Returns:
        int: Exit code (0 on success, non-zero on error).

    """
    logging.basicConfig(level=logging.INFO)
    try:
        # Read IPC configuration from environment variables
        socket_path = os.getenv("KICAD_API_SOCKET") or os.getenv("KICAD_IPC_SOCKET")
        token = os.getenv("KICAD_API_TOKEN")

        if not socket_path:
            logger.error(
                "KICAD_API_SOCKET environment variable not set. "
                "This plugin requires KiCad 9.0+ with IPC API support."
            )
            return 1

        # Initialize IPC client
        if token is None:
            ipc_client = KiCadIPCClient(socket_path=socket_path)
        else:
            ipc_client = KiCadIPCClient(
                socket_path=socket_path,
                token=token,
            )

        # Verify IPC is available (allow a short startup grace period)
        if not _wait_for_ipc(ipc_client):
            logger.error(
                "Unable to connect to KiCad IPC API. "
                "Ensure KiCad instance is running and IPC is enabled. "
                "socket=%r normalized_socket=%r token_set=%s",
                socket_path,
                ipc_client.socket_path,
                bool(token),
            )
            return 1

        logger.info("Connected to KiCad IPC API at %s", socket_path)

        # Create adapter set using IPC backend
        provider = KicadProvider()
        adapter_set = provider.create_adapter_set(
            launch_context="ipc",
            ipc_client=ipc_client,
        )

        logger.info("Adapter set created successfully")

        # Launch main window with IPC-backed adapters
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        JLCPCBTools = _import_mainwindow(plugin_dir)
        logger.info("Launching main window")
        window = JLCPCBTools(None, adapter_set=adapter_set)
        window.Center()
        window.Show()

        return 0

    except Exception:  # noqa: BLE001
        logger.exception("Error initializing IPC plugin")
        return 1


if __name__ == "__main__":
    sys.exit(main())

