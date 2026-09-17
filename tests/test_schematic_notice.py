"""Saved schematic notices never modify source files or guess another project."""

import importlib.util
from pathlib import Path
import subprocess
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "schematic_notice", Path(__file__).parents[1] / "schematic_notice.py"
)
assert _SPEC is not None and _SPEC.loader is not None
notice = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(notice)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Make a saved PCB and its identified project schematic."""
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch"):
        tmp_path.joinpath("board" + suffix).write_text("original", encoding="utf-8")
    return tmp_path / "board.kicad_pcb"


@pytest.mark.parametrize(
    ("name", "value", "found"),
    [
        ("LCSC", "C123", True),
        ("jLc Part", "C456", True),
        ("LCSC", "", False),
        ("LCSC", "other", False),
        ("Unrelated", "C123", False),
    ],
)
def test_saved_field_detection_is_read_only(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, found: bool
) -> None:
    """Generic XML retains fields on components excluded from manufacturing."""
    monkeypatch.setattr(notice, "_find_cli", lambda *_: "/kicad-cli")
    outputs = []

    def export(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert command[1:6] == ["sch", "export", "netlist", "--format", "kicadxml"]
        assert command[-1] == str(project.with_suffix(".kicad_sch"))
        assert kwargs["timeout"] == 30
        output = Path(command[command.index("--output") + 1])
        outputs.append(output)
        output.write_text(
            f'<export><components><comp ref="R1"><fields>'
            f'<field name="{name}">{value}</field></fields>'
            '<property name="exclude_from_bom"/>'
            '<sheetpath names="/Child/"/></comp></components></export>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(notice.subprocess, "run", export)
    assert notice.scan_schematic_lcsc(str(project)).found is found
    assert not outputs[0].exists()
    assert all(path.read_text() == "original" for path in project.parent.iterdir())


def test_unidentified_root_does_not_scan_neighbor(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name schematic without its project is not a known project root."""
    project.with_suffix(".kicad_pro").rename(project.parent / "other.kicad_pro")
    monkeypatch.setattr(notice, "_find_cli", lambda *_: pytest.fail("guessed root"))
    assert notice.scan_schematic_lcsc(str(project)).found is None


@pytest.mark.parametrize("board_path", ["", "/missing/project.kicad_pcb"])
def test_unsaved_or_missing_board_is_unavailable(board_path: str) -> None:
    """An unsaved board has no safe schematic root to inspect."""
    assert notice.scan_schematic_lcsc(board_path).found is None


@pytest.mark.parametrize(
    "failure", ["missing_cli", "timeout", "export", "xml", "shape"]
)
def test_unavailable_is_not_a_clean_schematic(
    project: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failures are reported as unknown so the caller cannot acknowledge them."""
    monkeypatch.setattr(
        notice, "_find_cli", lambda *_: None if failure == "missing_cli" else "/cli"
    )

    def export(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if failure == "export":
            raise subprocess.CalledProcessError(1, command)
        Path(command[command.index("--output") + 1]).write_text(
            "<bad" if failure == "xml" else "<unexpected/>", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(notice.subprocess, "run", export)
    result = notice.scan_schematic_lcsc(str(project))
    assert result.found is None
    assert result.detail


def test_cli_prefers_current_installation_and_rejects_other_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH cannot silently select an incompatible KiCad installation."""
    module = tmp_path / "Contents/Frameworks/Python/pcbnew.py"
    cli = tmp_path / "Contents/MacOS/kicad-cli"
    cli.parent.mkdir(parents=True)
    cli.touch()
    monkeypatch.setattr(notice.shutil, "which", lambda _: "/other/kicad-cli")
    calls = []

    def version(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(command[0])
        return subprocess.CompletedProcess(command, 0, stdout="10.0.6\n")

    monkeypatch.setattr(notice.subprocess, "run", version)
    assert notice._find_cli(str(module), "10.0.6") == str(cli)
    assert calls == [str(cli)]
    assert notice._find_cli(str(module), "9.0.7") is None
