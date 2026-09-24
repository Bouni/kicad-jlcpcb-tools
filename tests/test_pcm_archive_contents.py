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
        "impedance",
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
        "impedance/workbook.py",
        _TEMPLATE,
        "impedance/resources/preferred-fire.svg",
        "impedance/resources/preferred-fire-LICENSE.txt",
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


def test_extracted_pcm_generates_workbook_without_host_dependencies(
    pcm_archive: Path, tmp_path: Path
) -> None:
    """A fresh interpreter can use only the extracted vendor modules and template."""
    installed = tmp_path / "installed"
    with ZipFile(pcm_archive) as archive:
        archive.extractall(installed)
    plugins = installed / "plugins"
    capture = tmp_path / "capture.png"
    shutil.copy2(plugins / "icons/mdi-layers-triple-outline.png", capture)
    output = tmp_path / "sample-impedance.xlsx"
    script = """
from pathlib import Path
import sys

plugins, capture, output = map(Path, sys.argv[1:])
sys.path[:0] = [str(plugins), str(plugins / "lib")]
import et_xmlfile
import openpyxl
import packaging
from impedance.service import CapturedImage, ReportRow
from impedance.workbook import write_workbook

for dependency in (et_xmlfile, openpyxl, packaging):
    assert Path(dependency.__file__).is_relative_to(plugins / "lib")
row = ReportRow(
    "PCM sample", ("RF",), "RF", ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
    "F.Cu", ("In1.Cu",), "single_ended", 254000, None, "50",
    CapturedImage.load(capture),
)
assert write_workbook([row], output) == output
report = openpyxl.load_workbook(output)
try:
    assert report.active["A2"].value == "L1"
    assert report.active["B2"].value == "L2"
    assert report.active["D2"].value == 10
    assert report.active["G2"].value == 50
finally:
    report.close()
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
