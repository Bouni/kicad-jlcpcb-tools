"""Keep the supported KiCad range consistent in both PCM metadata forms."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PCM = _ROOT / "PCM"
_WORKFLOW = _ROOT / ".github" / "workflows" / "kicad-pcm.yml"
_RANGE_STEP = "Read the KiCad version range from the archive metadata"
_PRESERVED_FILES = [
    r"plugins/[^/]+/settings\.json$",
    r"plugins/[^/]+/jlcpcb/corrections\.db$",
    r"plugins/[^/]+/jlcpcb/mappings\.db$",
    r"plugins/[^/]+/jlcpcb/global\.db$",
]


def _repository_metadata() -> dict[str, Any]:
    """Supply numeric release values so the repository template is valid JSON."""
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    for placeholder in ("DOWNLOAD_SIZE_HERE", "INSTALL_SIZE_HERE"):
        template = template.replace(placeholder, "123")
    return json.loads(template)


def _shell() -> str:
    """Find the POSIX shell the archive script and the workflow step run under."""
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("PCM metadata transformation requires a POSIX shell")
    return shell


def _write_archive_metadata(template: str, metadata_path: Path) -> dict[str, Any]:
    """Apply only create_pcm_archive.sh's metadata edits to the template."""
    metadata_path.write_text(template, encoding="utf-8")
    script = (_PCM / "create_pcm_archive.sh").read_text(encoding="utf-8")
    helper_body = script.split("sed_inplace() {\n", 1)[1].split("\n}", 1)[0]
    helper = f"sed_inplace() {{\n{helper_body}\n}}\n"
    edits = script.split('echo "Modify archive metadata.json"\n', 1)[1].split(
        'echo "Zip PCM archive"', 1
    )[0]
    subprocess.run(
        [_shell(), "-eu", "-c", helper + edits],
        env={
            **os.environ,
            "VERSION": "2026.09.15",
            "METADATA_FILE": str(metadata_path),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _dispatch_inputs() -> dict[str, str]:
    """Parse the JSON object kicad-pcm.yml hands to the repository rebuild."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^\s+inputs: '(\{.*\})'$", workflow, re.MULTILINE)
    assert match, "kicad-pcm.yml has no single-line dispatch inputs object"
    return json.loads(match.group(1))


def _step_script(name: str) -> str:
    """Return the `run: |` block of the kicad-pcm.yml step with this name."""
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    run = lines.index(f"      - name: {name}") + 1
    assert lines[run].strip() == "run: |"
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body)


def test_repository_metadata_requires_kicad7_and_preserves_user_files() -> None:
    """The new release requires KiCad 7 while keeping persisted plugin data."""
    metadata = _repository_metadata()

    assert metadata["versions"][0]["kicad_version"] == "7.0"
    assert metadata["keep_on_update"] == _PRESERVED_FILES


def test_repository_metadata_stops_at_kicad10() -> None:
    """A SWIG release is never offered to KiCad 10.99 nightlies or KiCad 11."""
    assert _repository_metadata()["versions"][0]["kicad_version_max"] == "10.0"


@pytest.mark.parametrize("minimum_version", ["7.0", "8.0"])
def test_archive_metadata_keeps_minimum_without_release_download_fields(
    tmp_path: Path, minimum_version: str
) -> None:
    """Execute only the script's metadata edits, including a future minimum."""
    current_minimum = _repository_metadata()["versions"][0]["kicad_version"]
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    template = template.replace(
        f'"kicad_version": "{current_minimum}"',
        f'"kicad_version": "{minimum_version}"',
    )
    archive = _write_archive_metadata(template, tmp_path / "metadata.json")

    assert archive["versions"] == [
        {
            "version": "2026.09.15",
            "status": "testing",
            "kicad_version": minimum_version,
            "kicad_version_max": "10.0",
        }
    ]
    assert archive["keep_on_update"] == _PRESERVED_FILES


def test_archive_metadata_without_a_maximum_stays_valid(tmp_path: Path) -> None:
    """A template with no maximum still builds valid metadata that omits it."""
    minimum = _repository_metadata()["versions"][0]["kicad_version"]
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    without_maximum = "".join(
        line
        for line in template.splitlines(keepends=True)
        if '"kicad_version_max":' not in line
    )
    assert without_maximum != template

    archive = _write_archive_metadata(without_maximum, tmp_path / "metadata.json")

    assert archive["versions"] == [
        {
            "version": "2026.09.15",
            "status": "testing",
            "kicad_version": minimum,
        }
    ]


def test_release_dispatch_passes_the_archive_kicad_range(tmp_path: Path) -> None:
    """The repository rebuild receives the range the archive's metadata declares."""
    inputs = _dispatch_inputs()

    assert inputs["KICAD_VERSION"] == "${{env.KICAD_VERSION}}"
    assert inputs["KICAD_VERSION_MAX"] == "${{env.KICAD_VERSION_MAX}}"

    if shutil.which("jq") is None:
        pytest.skip("reading the range back needs jq, which GitHub's runners provide")
    archive_dir = tmp_path / "PCM" / "archive"
    archive_dir.mkdir(parents=True)
    template = (_PCM / "metadata.template.json").read_text(encoding="utf-8")
    archive = _write_archive_metadata(template, archive_dir / "metadata.json")
    github_env = tmp_path / "github_env"
    subprocess.run(
        [_shell(), "-eu", "-c", _step_script(_RANGE_STEP)],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_ENV": str(github_env)},
        check=True,
        capture_output=True,
        text=True,
    )

    version = archive["versions"][0]
    assert github_env.read_text(encoding="utf-8").splitlines() == [
        f"KICAD_VERSION={version['kicad_version']}",
        f"KICAD_VERSION_MAX={version['kicad_version_max']}",
    ]
