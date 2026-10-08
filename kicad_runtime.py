"""Select the KiCad binding without importing the wrong native module.

KiCad 10.99 removed the SWIG ``pcbnew`` module and runs plugins out of process
over its IPC API. That process may still find an older KiCad's ``pcbnew.py`` on
``sys.path`` (plugin environments include system site-packages), so the IPC
entry point selects its backend explicitly instead of relying on ImportError.
"""

from importlib import import_module
import os
from types import ModuleType

BACKEND_ENV = "KICAD_JLCPCB_TOOLS_BACKEND"
IPC_BACKEND = "ipc"


def using_ipc() -> bool:
    """Return whether this process was started by KiCad's IPC plugin runner."""
    return os.environ.get(BACKEND_ENV) == IPC_BACKEND


def import_pcbnew() -> ModuleType:
    """Return SWIG pcbnew, or its IPC-backed equivalent in an IPC plugin process."""
    if using_ipc():
        return import_module(".ipc_pcbnew", __package__)
    return import_module("pcbnew")
