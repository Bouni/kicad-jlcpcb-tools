"""Pure variant modules must remain usable without loading the plugin or wx."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_pure_variant_package_imports_without_gui_dependencies(tmp_path: Path) -> None:
    """Exercise filesystem imports in a fresh interpreter with GUI imports blocked."""
    script = r"""
import importlib
from pathlib import Path
import sys
import types

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
parent_name = "_variant_package_import_smoke"
parent = types.ModuleType(parent_name)
parent.__path__ = [str(root)]
sys.modules[parent_name] = parent
prefix = parent_name + ".variant"
blocked = {"wx", "pcbnew", parent_name + ".plugin", parent_name + ".mainwindow",
           prefix + ".controller", prefix + ".matrix_view"}

class RejectGuiImports:
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Pure variant imports loaded " + fullname)
        return None

assert not any(name in sys.modules for name in blocked)
sys.meta_path.insert(0, RejectGuiImports())
for name in ("native", "session", "store", "matrix_model", "text_layout"):
    imported = importlib.import_module(prefix + "." + name)
    assert Path(imported.__file__).resolve() == root / "variant" / (name + ".py")
assert not any(name in sys.modules for name in blocked)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
