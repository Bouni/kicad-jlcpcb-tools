"""Build real PCM archives in disposable snapshots and exercise shipped resources."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from zipfile import ZipFile

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_VERSION = "2099.01.01"
_TEMPLATE = "impedance/resources/Required_impedance_control.xlsx"
_HOOK = "scripts/example_post_generate_git_checkpoint.sh"


@pytest.fixture(scope="module")
def pcm_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the release packager against copied sources, never the live checkout."""
    required = ("sh", "zip", "unzip", "shasum", "sed", "awk")
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        pytest.skip(f"PCM build tools unavailable: {', '.join(missing)}")
    source = tmp_path_factory.mktemp("pcm-source")
    # Also work from unpacked sources without Git; don't copy environments,
    # project databases, generated packages or other local checkout contents.
    for original in _ROOT.iterdir():
        if original.is_file() and (
            original.suffix in {".py", ".png", ".json", ".toml"}
            or original.name in {"VERSION", "LICENSE"}
        ):
            shutil.copy2(original, source / original.name)
    for directory in (
        "PCM",
        "scripts",
        "icons",
        "lib",
        "common",
        "dblib",
        "core",
        "bom_estimation",
        "enrichment",
        "variant",
        "kicad_backend",
        "impedance",
        "grid_helpers",
    ):
        shutil.copytree(
            _ROOT / directory,
            source / directory,
            ignore=shutil.ignore_patterns("archive", "*.zip", "__pycache__", "*.pyc"),
        )
    # Model a developer checkout with new tooling and caches in shipped packages.
    for relative in (
        "scripts/unshipped_developer_tool.py",
        "tests/test_pcm_unshipped.py",
        "docker/pcm-test/Dockerfile",
        "examples/pcm-test/sample.kicad_pcb",
        "test_pcm_unshipped.py",
        "impedance/test_pcm_unshipped.py",
        "impedance/conftest.py",
        "impedance/pytest.ini",
        "impedance/.pytest_cache/cache-data",
        "impedance/.ruff_cache/cache-data",
        "impedance/__pycache__/model.cpython-39.pyc",
        "impedance/model.pyc",
        "impedance/model.pyo",
        "impedance/.DS_Store",
        "lib/websockets/tests/fixture.json",
    ):
        artifact = source / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("development artifact\n", encoding="utf-8")
    environment = dict(os.environ, GITHUB_ENV=str(source / "github-env"))
    result = subprocess.run(
        ["sh", "PCM/create_pcm_archive.sh", _VERSION],
        cwd=source,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return source / "PCM" / f"KiCAD-PCM-{_VERSION}.zip"


def test_pcm_archive_excludes_development_infrastructure(pcm_archive: Path) -> None:
    """Neither current test infrastructure nor future developer helpers ship."""
    with ZipFile(pcm_archive) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
    prohibited_directories = {
        "tests",
        "test",
        "examples",
        "docker",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
    unwanted = []
    for name in names:
        path = PurePosixPath(name)
        if (
            prohibited_directories.intersection(path.parts)
            or path.name in {"conftest.py", "pytest.ini", ".DS_Store", "AGENTS.md"}
            or path.match("test_*.py")
            or path.match("*_test.py")
            or path.suffix in {".pyc", ".pyo"}
        ):
            unwanted.append(name)
    assert unwanted == []


def test_pcm_archive_ships_only_documented_hook(pcm_archive: Path) -> None:
    """Ship the user-facing hook example without capture, fixture or CI scripts."""
    with ZipFile(pcm_archive) as archive:
        scripts = {
            item.filename
            for item in archive.infolist()
            if not item.is_dir() and item.filename.startswith("plugins/scripts/")
        }
        assert scripts == {f"plugins/{_HOOK}"}
        hook_mode = archive.getinfo(f"plugins/{_HOOK}").external_attr >> 16
        assert hook_mode & stat.S_IXUSR


def test_pcm_archive_retains_runtime_resources_and_licenses(pcm_archive: Path) -> None:
    """The installable payload keeps runtime imports, artwork and notices intact."""
    required = (
        "LICENSE",
        "default_settings.json",
        "__init__.py",
        "plugin.py",
        "fabrication_archive.py",
        "kicad_backend/__init__.py",
        "kicad_backend/swig/read.py",
        "impedance/jlcpcb_calculator.py",
        "impedance/_vendor/__init__.py",
        "grid_helpers/__init__.py",
        "grid_helpers/grid_sizing.py",
        "grid_helpers/grid_style.py",
        "impedance/workbook.py",
        _TEMPLATE,
        "impedance/resources/preferred-fire.svg",
        "impedance/resources/preferred-fire-LICENSE.txt",
        "lib/websockets/asyncio/client.py",
        "lib/websockets/sync/client.py",
        "lib/websockets-15.0.1.dist-info/LICENSE",
        "lib/openpyxl/__init__.py",
        "lib/openpyxl-3.1.5.dist-info/LICENCE.rst",
        "lib/et_xmlfile/__init__.py",
        "lib/et_xmlfile-2.0.0.dist-info/LICENCE.rst",
        "lib/et_xmlfile-2.0.0.dist-info/LICENCE.python",
        "lib/packaging/__init__.py",
        "icons/mdi-layers-triple-outline.png",
        _HOOK,
    )
    with ZipFile(pcm_archive) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(set(archive.namelist()))
        assert {f"plugins/{name}" for name in required}.issubset(archive.namelist())
        for relative in required:
            assert (
                archive.read(f"plugins/{relative}") == (_ROOT / relative).read_bytes()
            )
        assert archive.read("plugins/VERSION").decode().strip() == _VERSION
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["versions"][0]["version"] == _VERSION
        assert (
            archive.read("resources/icon.png") == (_ROOT / "PCM/icon.png").read_bytes()
        )
        assert hashlib.sha256(archive.read(f"plugins/{_TEMPLATE}")).hexdigest() == (
            "22d94b15293435f103868cb62cdbee3c32db6d7d635344baafa332d630b04bd4"
        )


@pytest.mark.parametrize("entry", ["direct", "plugin"])
@pytest.mark.parametrize("conflict", ["absent", "on-path", "loaded"])
def test_extracted_pcm_uses_its_runtime_without_replacing_host_dependencies(
    pcm_archive: Path, tmp_path: Path, entry: str, conflict: str
) -> None:
    """Real bootstraps support workbook PNGs and sockets despite other packages."""
    installed = tmp_path / "installed"
    with ZipFile(pcm_archive) as archive:
        archive.extractall(installed)
    plugins = installed / "plugins"
    capture = tmp_path / "capture.png"
    shutil.copy2(plugins / "icons/mdi-layers-triple-outline.png", capture)
    output = tmp_path / "sample-impedance.xlsx"
    public = tmp_path / "public"
    public.mkdir()
    if conflict != "absent":
        for name in ("websockets", "openpyxl", "et_xmlfile"):
            package = public / name
            package.mkdir()
            (package / "__init__.py").write_text(
                "__version__ = 'incompatible-public-package'\n", encoding="utf-8"
            )
    script = """
import asyncio
import importlib
import importlib.util
from pathlib import Path
import sys
import threading
import types
from typing import Any

plugins, capture, output, public = map(Path, sys.argv[1:5])
entry, conflict = sys.argv[5:]
# Tests may expose the install root, but must leave lib resolution to the plugin.
sys.path[:0] = [str(public), str(plugins)]
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
    spec = importlib.util.spec_from_file_location('_pcm_installed_plugin', plugins / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    assert len(registered) == 1
    prefix = spec.name + '.impedance'
else:
    prefix = 'impedance'
service = importlib.import_module(prefix + '.service')
workbook = importlib.import_module(prefix + '.workbook')
calculator = importlib.import_module(prefix + '.jlcpcb_calculator')
openpyxl = importlib.import_module(prefix + '._vendor.openpyxl')
et_xmlfile = importlib.import_module(prefix + '._vendor.et_xmlfile')
websockets = importlib.import_module(prefix + '._vendor.websockets')
serve = importlib.import_module(prefix + '._vendor.websockets.sync.server').serve
async_client = importlib.import_module(prefix + '._vendor.websockets.asyncio.client')
connect = calculator._websocket_connector()
import packaging

for dependency in (et_xmlfile, openpyxl, packaging, websockets):
    assert Path(dependency.__file__).is_relative_to(plugins / "lib")
assert connect.__module__ == calculator.__name__
assert issubclass(connect, async_client.connect)
assert websockets.connect is async_client.connect
assert Path(sys.modules[async_client.connect.__module__].__file__).is_relative_to(plugins / 'lib')
def echo(connection: Any) -> None:
    connection.send(connection.recv(timeout=2))

async def exchange(port: int) -> None:
    async with connect(f'ws://127.0.0.1:{port}', proxy=None, open_timeout=2, close_timeout=0.5) as client:
        await client.send('bundled PCM runtime')
        assert await asyncio.wait_for(client.recv(), timeout=2) == 'bundled PCM runtime'

with serve(echo, '127.0.0.1', 0, close_timeout=0.5) as server:
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.socket.getsockname()[1]
        asyncio.run(exchange(port))
    finally:
        server.shutdown()
        worker.join(timeout=3)
        assert not worker.is_alive()
row = service.ReportRow(
    "PCM sample", ("RF",), "RF", ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
    "F.Cu", ("In1.Cu",), "single_ended", 254000, None, "50",
    service.CapturedImage.load(capture),
)
assert workbook.write_workbook([row], output) == output
report = openpyxl.load_workbook(output)
try:
    assert report.active["A2"].value == "L1"
    assert report.active["B2"].value == "L2"
    assert report.active["D2"].value == 10
    assert report.active["G2"].value == 50
finally:
    report.close()
after = {name: module for name, module in sys.modules.items()
         if name.split('.')[0] in names}
assert after == before
assert all(vars(after[name]) == state for name, state in states.items())
if conflict != 'absent':
    for name in names:
        assert importlib.import_module(name).__version__ == 'incompatible-public-package'
assert "pytest" not in sys.modules
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(plugins),
            str(capture),
            str(output),
            str(public),
            entry,
            conflict,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        images = [name for name in archive.namelist() if name.startswith("xl/media/")]
        assert len(images) == 1
        assert archive.read(images[0]) == capture.read_bytes()
