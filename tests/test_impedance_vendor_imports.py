"""Keep bundled report/calculator dependencies separate from other KiCad plugins."""

from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("entry", ["direct", "plugin"])
@pytest.mark.parametrize("conflict", ["absent", "on-path", "loaded"])
@pytest.mark.parametrize("component", ["calculator", "workbook"])
def test_runtime_uses_bundled_dependencies_without_replacing_public_modules(
    tmp_path: Path, entry: str, conflict: str, component: str
) -> None:
    """Both bootstraps must work before and after another plugin's imports."""
    public = tmp_path / "public"
    public.mkdir()
    if conflict != "absent":
        for name in ("websockets", "openpyxl", "et_xmlfile"):
            package = public / name
            package.mkdir()
            (package / "__init__.py").write_text(
                "__version__ = 'incompatible-public-package'\n", encoding="utf-8"
            )
    script = r"""
import importlib
import importlib.util
from pathlib import Path
import sys
import types

root, public = map(Path, sys.argv[1:3])
entry, conflict, component = sys.argv[3:]
sys.path[:0] = [str(public), str(root)]
names = ('websockets', 'openpyxl', 'et_xmlfile')
if conflict == 'loaded':
    for name in names:
        importlib.import_module(name)
before = {name: module for name, module in sys.modules.items()
          if name.split('.')[0] in names}
states = {name: vars(module).copy() for name, module in before.items()}
if entry == 'plugin':
    registered = []
    class ActionPlugin:
        def __init__(self) -> None:
            self.defaults()
        def register(self) -> None:
            registered.append(self)
    pcbnew = types.ModuleType('pcbnew')
    pcbnew.ActionPlugin = ActionPlugin
    sys.modules['pcbnew'] = pcbnew
    sys.modules['wx'] = types.ModuleType('wx')
    spec = importlib.util.spec_from_file_location('_installed_jlcpcb_plugin', root / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    assert len(registered) == 1
    assert registered[0].name == 'JLCPCB Tools'
    prefix = spec.name + '.impedance'
else:
    prefix = 'impedance'
if component == 'calculator':
    calculator = importlib.import_module(prefix + '.jlcpcb_calculator')
    function = calculator._websocket_connector()
else:
    workbook = importlib.import_module(prefix + '.workbook')
    function = workbook.load_workbook
assert Path(sys.modules[function.__module__].__file__).is_relative_to(root / 'lib')
after = {name: module for name, module in sys.modules.items()
         if name.split('.')[0] in names}
assert after == before, (tuple(before), tuple(after))
assert all(vars(after[name]) == state for name, state in states.items())
if conflict != 'absent':
    for name in names:
        module = importlib.import_module(name)
        assert module.__version__ == 'incompatible-public-package'
        assert Path(module.__file__).parent.parent == public
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(_ROOT),
            str(public),
            entry,
            conflict,
            component,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("dependency", ["websockets", "openpyxl"])
@pytest.mark.parametrize(
    "layout",
    ["standalone", "missing-installed", "incompatible-installed", "damaged-bundle"],
)
def test_standalone_installs_find_sources_without_trusting_public_module_cache(
    tmp_path: Path, dependency: str, layout: str
) -> None:
    """Preserve setuptools use without falling back from a damaged PCM payload."""
    install = tmp_path / "install"
    shutil.copytree(
        _ROOT / "impedance",
        install / "impedance",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    site = tmp_path / "site"
    site.mkdir()
    shutil.copytree(
        _ROOT / "lib" / "packaging",
        site / "packaging",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    public = tmp_path / "public"
    for name in ("websockets", "openpyxl", "et_xmlfile"):
        package = public / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "__version__ = 'old-already-imported-package'\n", encoding="utf-8"
        )
        if layout in {"standalone", "damaged-bundle"}:
            shutil.copytree(
                _ROOT / "lib" / name,
                site / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        elif layout == "incompatible-installed":
            shutil.copytree(package, site / name)
    if layout == "damaged-bundle":
        (install / "lib").mkdir()
    script = r"""
import importlib
from pathlib import Path
import sys

install, site, public = map(Path, sys.argv[1:4])
dependency, layout = sys.argv[4:]
sys.path.insert(0, str(public))
for name in ('websockets', 'openpyxl', 'et_xmlfile'):
    importlib.import_module(name)
before = {name: module for name, module in sys.modules.items()
          if name.split('.')[0] in ('websockets', 'openpyxl', 'et_xmlfile')}
states = {name: vars(module).copy() for name, module in before.items()}
sys.path[0:1] = [str(site), str(install)]
try:
    if dependency == 'websockets':
        calculator = importlib.import_module('impedance.jlcpcb_calculator')
        function = calculator._websocket_connector()
    else:
        function = importlib.import_module('impedance.workbook').load_workbook
except Exception as error:
    assert layout != 'standalone', error
    assert 'Install or update the complete plugin package' in str(error), error
else:
    assert layout == 'standalone', 'Broken installation unexpectedly used public dependencies'
    assert Path(sys.modules[function.__module__].__file__).is_relative_to(site)
after = {name: module for name, module in sys.modules.items()
         if name.split('.')[0] in ('websockets', 'openpyxl', 'et_xmlfile')}
assert after == before
assert all(vars(after[name]) == state for name, state in states.items())
assert all(module.__version__ == 'old-already-imported-package' for module in before.values())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(install),
            str(site),
            str(public),
            dependency,
            layout,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "version", "supported"),
    [
        ("websockets", "15.1", True),
        ("websockets", "15.0.1.post1", True),
        ("websockets", "15.0.1+vendor", True),
        ("websockets", "15.0.0", False),
        ("websockets", "16.0", False),
        ("websockets", "unknown", False),
        ("openpyxl", "3.1.5+vendor", True),
        ("openpyxl", "3.1.6", False),
        ("et_xmlfile", "2.0.0+vendor", True),
        ("et_xmlfile", "2.0.1", False),
    ],
)
def test_standalone_version_validation_matches_project_requirements(
    name: str, version: str, supported: bool
) -> None:
    """Accepted PEP 440 release spellings must match the declared requirements."""
    from impedance._vendor import _check_installed_version

    module = ModuleType(name)
    module.__version__ = version
    if supported:
        _check_installed_version(module, name)
    else:
        with pytest.raises(ImportError, match="Install or update"):
            _check_installed_version(module, name)


def test_private_vendor_first_import_is_thread_safe_and_keeps_lazy_imports_private(
    tmp_path: Path,
) -> None:
    """Worker first use must retain standard import locking and nested imports."""
    script = r"""
import builtins
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import sys
import threading

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import impedance
original_import = builtins.__import__
original_path = list(sys.path)
barrier = threading.Barrier(8)
def load(_index: int) -> tuple[object, object, object]:
    barrier.wait(timeout=5)
    spreadsheet = importlib.import_module('impedance._vendor.openpyxl')
    # This is a module-level __getattr__ import, not an eager import statement.
    sockets = importlib.import_module('impedance._vendor.websockets')
    lazy_connect = sockets.connect
    client = importlib.import_module('impedance._vendor.websockets.sync.client')
    writer = importlib.import_module('impedance._vendor.et_xmlfile')
    book = spreadsheet.Workbook()
    book.active['A1'] = 'worker import'
    assert book.active['A1'].value == 'worker import'
    assert lazy_connect.__module__.startswith('impedance._vendor.')
    return spreadsheet, client.connect, writer
with ThreadPoolExecutor(max_workers=8) as executor:
    results = list(executor.map(load, range(8)))
assert all(all(a is b for a, b in zip(result, results[0])) for result in results)
assert builtins.__import__ is original_import
assert sys.path == original_path
vendor = importlib.import_module('impedance._vendor')
for _ in range(2):
    importlib.reload(vendor)
assert sum(getattr(finder, '_jlcpcb_vendor_prefix', None) == vendor.__name__ + '.'
           for finder in sys.meta_path) == 1
assert importlib.import_module('impedance._vendor.websockets.sync.client').connect is results[0][1]
assert not any(name.split('.')[0] in {'websockets', 'openpyxl', 'et_xmlfile'}
               for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(_ROOT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
