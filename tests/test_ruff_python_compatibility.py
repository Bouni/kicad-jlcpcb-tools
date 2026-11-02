"""Keep Ruff fixes compatible with KiCad's Python 3.9 annotations."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("future_annotations", [False, True])
@pytest.mark.parametrize("include_union_rule", [False, True])
def test_ruff_preserves_annotations(
    tmp_path: Path, future_annotations: bool, include_union_rule: bool
) -> None:
    """Preserve eager and postponed annotations with the installed Ruff."""
    repository = Path(__file__).resolve().parents[1]
    shutil.copyfile(repository / "pyproject.toml", tmp_path / "pyproject.toml")
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
    command = [sys.executable, "-m", "ruff", "check", "--no-cache", "--fix"]
    if include_union_rule:
        # Ruff versions before UP045 still need to exercise both settings.
        command.extend(["--extend-select", "UP007"])
    result = subprocess.run(
        [*command, fixture.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.read_text(encoding="utf-8") == source
