"""Keep pinned annotation hooks compatible with KiCad's Python 3.9."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("hook", ["ruff", "pyupgrade"])
@pytest.mark.parametrize("future_annotations", [False, True])
def test_hooks_preserve_annotations(
    tmp_path: Path, hook: str, future_annotations: bool
) -> None:
    """Leave compatible typing annotations unchanged in both evaluation modes."""
    pytest.importorskip("pre_commit", reason="Install pre-commit to test pinned hooks")
    repository = Path(__file__).resolve().parents[1]
    for filename in (".pre-commit-config.yaml", "pyproject.toml"):
        shutil.copyfile(repository / filename, tmp_path / filename)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    future = "from __future__ import annotations\n\n" if future_annotations else ""
    source = (
        '"""Representative plugin annotations."""\n'
        f"{future}"
        "from typing import Optional, Union\n"
        "\n"
        "\n"
        "def annotated(value: Optional[str], number: Union[int, str]) -> Optional[str]:\n"
        '    """Return the provided value."""\n'
        "    return value\n"
    )
    fixture = tmp_path / "annotations_fixture.py"
    fixture.write_text(source, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    result = subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", hook, "--files", fixture.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Passed" in result.stdout, result.stdout + result.stderr
    assert fixture.read_text(encoding="utf-8") == source
