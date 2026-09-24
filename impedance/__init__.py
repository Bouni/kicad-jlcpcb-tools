"""Controlled-impedance documentation, independent of the KiCad UI at import time."""

from pathlib import Path
import sys

# Example tools import this package directly, without the plugin bootstrap.
_bundled_libraries = str(Path(__file__).resolve().parent.parent / "lib")
if _bundled_libraries not in sys.path:
    sys.path.append(_bundled_libraries)
