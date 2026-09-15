"""Exercise the configured hooks against Python 3.9 runtime annotations."""

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

HOOKS = ("ruff", "ruff-format", "pyupgrade")


def run_hook(
    repository: Path, hook: str, filename: str
) -> subprocess.CompletedProcess[str]:
    """Run the pinned hook in an isolated Git repository."""
    return subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", hook, "--files", filename],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def hook_repository(tmp_path: Path) -> Path:
    """Copy only the real hook configuration into a disposable repository."""
    pytest.importorskip(
        "pre_commit", reason="Install pre-commit to run hook integration tests"
    )
    source = Path(__file__).resolve().parents[1]
    for filename in (".pre-commit-config.yaml", "pyproject.toml"):
        shutil.copyfile(source / filename, tmp_path / filename)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize("future_annotations", [False, True])
def test_hooks_preserve_runtime_annotations(
    hook_repository: Path, future_annotations: bool
) -> None:
    """Keep optional, union, and nested callable hints usable at runtime."""
    fixture = hook_repository / "annotations_fixture.py"
    source = '''"""Representative plugin annotations with runtime evaluation."""
{future}
from typing import Callable, Optional, Union


def annotated(
    value: Optional[str],
    number: Union[int, str],
    callback: Optional[Callable[[Optional[str]], Union[int, str]]],
) -> Optional[str]:
    """Return the provided value."""
    return value
'''
    future = "from __future__ import annotations\n" if future_annotations else ""
    fixture.write_text(source.format(future=future), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=hook_repository, check=True)

    output = []
    union_rewriters = []
    union_count = 0
    for hook in HOOKS:
        result = run_hook(hook_repository, hook, fixture.name)
        output.append(result.stdout + result.stderr)
        assert result.returncode in (0, 1), output
        rewritten_count = sum(
            isinstance(node, ast.BitOr)
            for node in ast.walk(ast.parse(fixture.read_text(encoding="utf-8")))
        )
        if rewritten_count > union_count:
            union_rewriters.append(hook)
        union_count = rewritten_count

    transformed = fixture.read_text(encoding="utf-8")
    assert not union_rewriters, (
        f"Hooks {union_rewriters} introduced Python 3.10 unions into runtime annotations:\n"
        + transformed
        + "\n"
        + "\n".join(output)
    )
    # Hooks may need a newer Python than the plugin interpreter checked by CI.
    validate_hints = """
import importlib.util
import os
import sys
import typing

if "KICAD_TEST_PYTHON" in os.environ:
    assert sys.version_info[:2] == (3, 9), sys.version

spec = importlib.util.spec_from_file_location("annotations_fixture", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert typing.get_type_hints(module.annotated) == {
    "value": typing.Optional[str],
    "number": typing.Union[int, str],
    "callback": typing.Optional[
        typing.Callable[[typing.Optional[str]], typing.Union[int, str]]
    ],
    "return": typing.Optional[str],
}
"""
    runtime = os.environ.get("KICAD_TEST_PYTHON", sys.executable)
    result = subprocess.run(
        [runtime, "-c", validate_hints, str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    for hook in HOOKS:
        result = run_hook(hook_repository, hook, fixture.name)
        assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.read_text(encoding="utf-8") == transformed


@pytest.mark.parametrize("hook", ["ruff", "pyupgrade"])
def test_hooks_still_apply_safe_fixes(hook_repository: Path, hook: str) -> None:
    """Preserving annotations must not disable ordinary automatic fixes."""
    fixture = hook_repository / "safe_fix.py"
    fixture.write_text(
        '"""A Python 3 safe modernization."""\nMESSAGE = u"hello"\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=hook_repository, check=True)
    result = run_hook(hook_repository, hook, fixture.name)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'MESSAGE = "hello"' in fixture.read_text(encoding="utf-8")
    result = run_hook(hook_repository, hook, fixture.name)
    assert result.returncode == 0, result.stdout + result.stderr
