"""Only the action plugin may import pcbnew at runtime; it hands it to the backend.

Ruff's TID251 rule bans ``import pcbnew`` statements. It cannot see an import
by name, such as ``importlib.import_module("pcbnew")``, so this scan does.
"""

import ast
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The plugin's entry point imports pcbnew for good.
_PERMANENT = ("plugin.py",)
# Its entry in pyproject.toml's TID251 exemptions, with the tests' and the
# development tool that builds a gate board in KiCad's own Python, which is never
# shipped.
_PERMANENT_EXEMPTIONS = {
    "plugin.py",
    "tests/**",
    "tools/jlcfootprint/make_corner_case_board.py",
}
# Imports by name not yet behind the backend, by module, with their count.
# Fabrication's three go when gerber and drill output move to the backend.
_ALLOWED = {"fabrication.py": 3}


def pcbnew_imports_by_name(source: str) -> int:
    """Count ``import_module``/``__import__`` calls naming pcbnew or a submodule.

    The module name may be passed by position or as ``name=``.
    """
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else ""
        )
        argument = (
            node.args[0]
            if node.args
            else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        )
        if (
            name in {"import_module", "__import__"}
            and isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and (argument.value == "pcbnew" or argument.value.startswith("pcbnew."))
        ):
            count += 1
    return count


def pcbnew_import_statements(source: str) -> int:
    """Count ``import pcbnew`` and ``from pcbnew import`` statements, nested too."""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            count += sum(
                alias.name == "pcbnew" or alias.name.startswith("pcbnew.")
                for alias in node.names
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and (node.module == "pcbnew" or node.module.startswith("pcbnew."))
        ):
            count += 1
    return count


def _ban_patterns() -> list[str]:
    """Return pyproject.toml's patterns exempt from the pcbnew import ban, as written."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.ruff.lint.per-file-ignores]", 1)[1].split("\n[", 1)[0]
    return re.findall(r'^"([^"]+)"\s*=\s*\[[^\]]*"TID251"', section, flags=re.M)


def _ban_exemptions() -> set[str]:
    """Return the paths exempt from the pcbnew import ban, relative to the root."""
    return {pattern.removeprefix("./") for pattern in _ban_patterns()}


def _runtime_modules(root: Path = ROOT) -> Iterator[tuple[str, Path]]:
    """Yield every plugin module that runs inside KiCad, by relative path.

    The plugin's runtime is what the PCM archive ships: the top-level modules
    and the directories its script copies, without the tests it prunes and
    the root conftest.py, which ships but only pytest runs. Anything else in
    a checkout, such as a local archive build or a virtual environment, is
    not.
    """
    script = (root / "PCM" / "create_pcm_archive.sh").read_text(encoding="utf-8")
    files = script.split("for file in ", 1)[1].split(";", 1)[0].split()
    directories = script.split("for dir in ", 1)[1].split(";", 1)[0].split()
    paths = [
        *(
            path
            for pattern in files
            if pattern.endswith(".py")
            for path in root.glob(pattern)
        ),
        *(path for name in directories for path in (root / name).rglob("*.py")),
    ]
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if (
            relative.startswith(_PERMANENT)
            or path.name.startswith("test_")
            or path.name == "conftest.py"
            or "__pycache__" in path.parts
        ):
            continue
        yield relative, path


def test_only_allowed_modules_import_pcbnew_by_name() -> None:
    """A new import by name fails here; removing one means shrinking the list."""
    found = Counter(
        {
            relative: count
            for relative, path in _runtime_modules()
            if (count := pcbnew_imports_by_name(path.read_text(encoding="utf-8")))
        }
    )
    assert dict(found) == _ALLOWED


def test_only_modules_that_import_pcbnew_are_exempt_from_the_ban() -> None:
    """An exemption for a module that imports nothing would let a new import in.

    Ruff exempts the permanent importers and, until each moves behind the
    backend, the modules that still import pcbnew; no other module.
    """
    importers = {
        relative
        for relative, path in _runtime_modules()
        if pcbnew_import_statements(path.read_text(encoding="utf-8"))
    }
    exemptions = _ban_exemptions()

    assert exemptions >= _PERMANENT_EXEMPTIONS
    assert exemptions - _PERMANENT_EXEMPTIONS == importers


@pytest.mark.parametrize(
    "source, count",
    [
        ("import pcbnew", 1),
        ("import pcbnew as kicad_pcbnew", 1),
        ("from pcbnew import ToMM, FromMM", 1),
        ("def f():\n    import pcbnew.foo", 1),
        ("from .pcbnew import x", 0),
        ("import pcbnew_tools", 0),
    ],
)
def test_the_scan_finds_import_statements(source: str, count: int) -> None:
    """Statements importing pcbnew count, at any depth; look-alikes do not."""
    assert pcbnew_import_statements(source) == count


@pytest.mark.parametrize(
    "source, count",
    [
        ('import_module("pcbnew")', 1),
        ('importlib.import_module("pcbnew")', 1),
        ('importlib.import_module("pcbnew.something")', 1),
        ('importlib.import_module(name="pcbnew")', 1),
        ('__import__(name="pcbnew")', 1),
        ('import_module(package="pcbnew", name="wx")', 0),
        ('__import__("pcbnew")', 1),
        ('getattr(import_module("pcbnew"), "F_Cu")', 1),
        ('import_module("wx")', 0),
        ('import_module("pcbnew_tools")', 0),
        ("import_module(name)", 0),
        ('print("pcbnew")', 0),
    ],
)
def test_the_scan_finds_imports_by_name(source: str, count: int) -> None:
    """Calls naming pcbnew count; other modules, names and strings do not."""
    assert pcbnew_imports_by_name(source) == count


def test_the_scan_covers_the_plugins_runtime() -> None:
    """The scan reads plugin code, the SWIG backend included, not tests or tooling."""
    modules = {relative for relative, _path in _runtime_modules()}
    assert {"mainwindow.py", "fabrication.py", "variant/native.py"} <= modules
    assert {"kicad_backend/records.py", "kicad_backend/swig/read.py"} <= modules
    assert not any(
        module.startswith((*_PERMANENT, "tests/", "db_build/")) for module in modules
    )
    assert not any(module.rsplit("/", 1)[-1].startswith("test_") for module in modules)


def test_the_scan_reads_only_what_the_archive_ships(tmp_path: Path) -> None:
    """A local archive build or a virtual environment is not the plugin's runtime."""
    (tmp_path / "PCM").mkdir()
    (tmp_path / "PCM" / "create_pcm_archive.sh").write_bytes(
        (ROOT / "PCM" / "create_pcm_archive.sh").read_bytes()
    )
    for relative in (
        "PCM/archive/plugins/plugin.py",
        "PCM/archive/plugins/mainwindow.py",
        "venv/lib/site.py",
        "env/lib/site.py",
        "mainwindow.py",
        "plugin.py",
        "kicad_backend/swig/read.py",
        "common/test_partsdb.py",
        "conftest.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import pcbnew\n", encoding="utf-8")

    modules = {relative for relative, _path in _runtime_modules(tmp_path)}

    assert modules == {"mainwindow.py", "kicad_backend/swig/read.py"}


def test_each_exemption_is_anchored_at_the_root() -> None:
    """Ruff matches a bare file name in any directory; "./" keeps it to the root."""
    assert all(pattern.startswith("./") for pattern in _ban_patterns())
