"""Shared UI primitives must load under standalone and installed package names."""

from pathlib import Path
import subprocess
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry", ["standalone", "plugin"])
def test_shared_grid_helpers_need_no_vendor_or_application_bootstrap(
    tmp_path: Path, entry: str
) -> None:
    """Neither native wx nor database/API dependencies belong in helper imports."""
    script = r"""
import importlib
from pathlib import Path
import sys
import types

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
prefix = 'grid_helpers'
if sys.argv[2] == 'plugin':
    parent = types.ModuleType('_installed_grid_plugin')
    parent.__path__ = [str(root)]
    sys.modules[parent.__name__] = parent
    prefix = parent.__name__ + '.' + prefix
blocked = {'wx', 'pcbnew', 'common', 'requests', 'cachetools', 'ratelimit',
           'jlcpcb_grid_sizing', 'jlcpcb_grid_style'}

class RejectUnrelatedImports:
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Shared grid helper imported ' + fullname)
        return None

sys.meta_path.insert(0, RejectUnrelatedImports())
sizing = importlib.import_module(prefix + '.grid_sizing')
style = importlib.import_module(prefix + '.grid_style')
assert sizing.measure_grid_content_widths.__module__ == prefix + '.grid_sizing'
assert style.alternating_row_background((255, 255, 255), (0, 0, 0), 1) == (230, 230, 230)
assert not blocked.intersection(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(_ROOT), entry],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
