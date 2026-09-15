"""Keep the supported KiCad minimum consistent in both PCM metadata forms."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

_PCM = Path(__file__).resolve().parent.parent / "PCM"
_PRESERVED_FILES = [
    r"plugins/[^/]+/settings\.json$",
    r"plugins/[^/]+/jlcpcb/corrections\.db$",
    r"plugins/[^/]+/jlcpcb/mappings\.db$",
]


def _repository_metadata() -> dict[str, Any]:
    """Supply numeric release values so the repository template is valid JSON."""
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    for placeholder in ("DOWNLOAD_SIZE_HERE", "INSTALL_SIZE_HERE"):
        template = template.replace(placeholder, "123")
    return json.loads(template)


def test_repository_metadata_requires_kicad7_and_preserves_user_files() -> None:
    """The new release requires KiCad 7 while keeping persisted plugin data."""
    metadata = _repository_metadata()

    assert metadata["versions"][0]["kicad_version"] == "7.0"
    assert metadata["keep_on_update"] == _PRESERVED_FILES


@pytest.mark.parametrize("minimum_version", ["7.0", "8.0"])
def test_archive_metadata_keeps_minimum_without_release_download_fields(
    tmp_path: Path, minimum_version: str
) -> None:
    """Execute only the script's metadata edits, including a future minimum."""
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("PCM metadata transformation requires a POSIX shell")
    current_minimum = _repository_metadata()["versions"][0]["kicad_version"]
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    template = template.replace(
        f'"kicad_version": "{current_minimum}"',
        f'"kicad_version": "{minimum_version}"',
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(template, encoding="utf-8")

    script = (_PCM / "create_pcm_archive.sh").read_text(encoding="utf-8")
    helper_body = script.split("sed_inplace() {\n", 1)[1].split("\n}", 1)[0]
    helper = f"sed_inplace() {{\n{helper_body}\n}}\n"
    edits = script.split('echo "Modify archive metadata.json"\n', 1)[1].split(
        'echo "Zip PCM archive"', 1
    )[0]
    subprocess.run(
        [shell, "-eu", "-c", helper + edits],
        env={
            **os.environ,
            "VERSION": "2026.09.15",
            "METADATA_FILE": str(metadata_path),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    archive = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert archive["versions"] == [
        {
            "version": "2026.09.15",
            "status": "testing",
            "kicad_version": minimum_version,
        }
    ]
    assert archive["keep_on_update"] == _PRESERVED_FILES
