"""KiCad IPC plugin entry point (KiCad 10.99 and newer); see plugin.json.

KiCad runs this file with the plugin's own Python environment. It loads this
directory as a package with the IPC backend selected, then opens the window.
"""

# pyright: reportMissingImports=false

import importlib
import importlib.util
import os
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent
PACKAGE = "kicad_jlcpcb_tools"


def _load_package() -> None:
    # Select the backend before the package can import a stale SWIG pcbnew.
    os.environ["KICAD_JLCPCB_TOOLS_BACKEND"] = "ipc"
    # Prefer the bundled kipy, which matches this KiCad's API, over installed ones.
    sys.path.insert(0, str(PLUGIN_DIR / "lib"))
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load JLCPCB Tools from {PLUGIN_DIR}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)


if __name__ == "__main__":
    try:
        import wx  # noqa: F401  # pylint: disable=import-error,unused-import
    except ImportError:
        sys.stderr.write(
            "JLCPCB Tools needs wxPython. Point KiCad's Python interpreter "
            "(Preferences > Plugins) at a Python that has wxPython installed.\n"
        )
        sys.exit(1)
    _load_package()
    sys.exit(importlib.import_module(f"{PACKAGE}.ipc_app").main())
