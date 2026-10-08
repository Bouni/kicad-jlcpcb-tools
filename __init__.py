"""Init file for plugin."""

import os
import sys

lib_path = os.path.join(os.path.dirname(__file__), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

try:
    from .kicad_runtime import using_ipc  # noqa: I001, E402

    # KiCad 10.99+ starts ipc_plugin.py instead; it has no SWIG action plugins.
    if not using_ipc():
        from .plugin import JLCPCBPlugin  # noqa: E402

        if __name__ != "__main__":
            JLCPCBPlugin().register()
except ImportError:
    # Plugin import may fail in test environments where KiCad is not available
    pass
