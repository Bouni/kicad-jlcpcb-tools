"""IPC plugin entry point for KiCad 9.0+.

This module serves as the main entry point when the plugin is launched by KiCad
through the IPC plugin system. It:

1. Reads KICAD_API_SOCKET and KICAD_API_TOKEN from environment variables
2. Initializes the IPC client
3. Creates adapter set using the IPC backend
4. Launches the main window UI

"""

import logging
import os
import sys

from ipc_client import KiCadIPCClient
from kicad_api import KicadProvider

logger = logging.getLogger(__name__)


def main() -> int:
    """Launch the IPC plugin.

    Returns:
        int: Exit code (0 on success, non-zero on error).

    """
    logging.basicConfig(level=logging.INFO)
    try:
        # Read IPC configuration from environment variables
        socket_path = os.getenv("KICAD_API_SOCKET")

        if not socket_path:
            logger.error(
                "KICAD_API_SOCKET environment variable not set. "
                "This plugin requires KiCad 9.0+ with IPC API support."
            )
            return 1

        # Initialize IPC client
        ipc_client = KiCadIPCClient(socket_path=socket_path)

        # Verify IPC is available
        if not ipc_client.is_available():
            logger.error(
                "Unable to connect to KiCad IPC API. "
                "Ensure KiCad instance is running and IPC is enabled."
            )
            return 1

        logger.info("Connected to KiCad IPC API at %s", socket_path)

        # Create adapter set using IPC backend (prefer_ipc=True forces IPC selection)
        provider = KicadProvider()
        adapter_set = provider.create_adapter_set(prefer_ipc=True)

        logger.info("Adapter set created successfully")

        # Import mainwindow here to avoid issues with wxPython initialization
        from mainwindow import MainWindow

        # Launch main window with IPC-backed adapters
        logger.info("Launching main window")
        window = MainWindow(adapter_set=adapter_set)
        window.show()

        return 0

    except Exception:  # noqa: BLE001
        logger.exception("Error initializing IPC plugin")
        return 1


if __name__ == "__main__":
    sys.exit(main())

