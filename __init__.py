"""Init file for plugin."""

import os
import sys

lib_path = os.path.join(os.path.dirname(__file__), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)

from .plugin import JLCPCBPlugin  # noqa: I001, E402

if __name__ != "__main__":
    JLCPCBPlugin().register()
