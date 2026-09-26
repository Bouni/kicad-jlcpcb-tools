"""Check smoke-result validation with synthetic archives, without native KiCad.

These tests establish what evidence the native workflow must produce. They do
not execute that workflow, plot fabrication data, or verify wx controls.
"""

from base64 import b64decode, b64encode
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.native_impedance_workflow import check_generated_archive

_WORKBOOK = "Required_impedance_control.xlsx"
_HTML = "Required_impedance_control.html"
_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_GERBER = b"G04 Synthetic validation fixture*\n%FSLAX46Y46*%\n%MOMM*%\nM02*\n"
_DRILL = b"M48\nMETRIC\nT1C0.800\n%\nT1\nX1.0Y1.0\nM30\n"


def _workbook(images: bool = True) -> bytes:
    """Provide only a ZIP media container, not a generated report workbook."""
    stream = BytesIO()
    with ZipFile(stream, "w") as workbook:
        workbook.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        if images:
            workbook.writestr("xl/media/image1.png", _PNG)
            workbook.writestr("xl/media/image2.png", _PNG)
    return stream.getvalue()


def _entries(enabled: bool = True) -> dict[str, bytes]:
    """Supply minimally marked fabrication data and optional report fixtures."""
    members = {
        "sample-F_Cu.gbr": _GERBER,
        "sample-B_Cu.gbr": _GERBER,
        "sample-PTH.drl": _DRILL,
    }
    if enabled:
        image = b64encode(_PNG).decode("ascii")
        members[_WORKBOOK] = _workbook()
        members[_HTML] = (
            f'<!doctype html><img src="data:image/png;base64,{image}">'
        ).encode()
    return members


def _archive(tmp_path: Path, entries: dict[str, bytes]) -> Path:
    """Keep every synthetic artifact inside the test's temporary directory."""
    path = tmp_path / "manufacturing.zip"
    with ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("enabled", [True, False])
def test_archive_evidence_counts_fabrication_and_enabled_report_media(
    tmp_path: Path, enabled: bool
) -> None:
    """Success reports fabrication counts and reflects the requested state."""
    result = check_generated_archive(_archive(tmp_path, _entries(enabled)), enabled)
    assert result["gerbers"] == 2
    assert result["drills"] == 1
    assert result["workbook_images"] == (2 if enabled else 0)
    assert result["enabled"] is enabled


@pytest.mark.parametrize("extension", [".gbr", ".drl"])
def test_reports_cannot_substitute_for_missing_fabrication(
    tmp_path: Path, extension: str
) -> None:
    """Both actual fabrication categories are required even with reports."""
    entries = {
        name: data for name, data in _entries().items() if not name.endswith(extension)
    }
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("member", ["sample-F_Cu.gbr", "sample-PTH.drl"])
@pytest.mark.parametrize("content", [b"", b"(kicad_pcb (version 20260101))"])
def test_fabrication_extensions_do_not_make_empty_or_pcb_data_valid(
    tmp_path: Path, member: str, content: bytes
) -> None:
    """The prior PCB-as-fabrication stand-in must never count as native evidence."""
    entries = _entries()
    entries[member] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


def test_board_file_instead_of_fabrication_is_rejected(tmp_path: Path) -> None:
    """A board plus reports cannot satisfy the Generate fabrication requirement."""
    entries = _entries()
    entries = {
        name: data for name, data in entries.items() if name in {_WORKBOOK, _HTML}
    }
    entries["sample.kicad_pcb"] = b"(kicad_pcb (version 20260101))"
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize(
    ("member", "content"),
    [
        ("sample-F_Cu.gbr", b"G04 Missing format*\nM02*\n"),
        ("sample-F_Cu.gbr", b"%FSLAX46Y46*%\nG04 Truncated output*\n"),
        ("sample-PTH.drl", b"METRIC\n%\nM30\n"),
        ("sample-PTH.drl", b"M48\nMETRIC\n%\nX1.0Y1.0\n"),
    ],
)
def test_fabrication_requires_format_header_and_completion_marker(
    tmp_path: Path, member: str, content: bytes
) -> None:
    """Incomplete plotting and drilling output cannot produce a green smoke."""
    entries = _entries()
    entries[member] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


def test_duplicate_members_are_rejected(tmp_path: Path) -> None:
    """A ZIP reader selecting one duplicate cannot conceal ambiguous output."""
    path = _archive(tmp_path, _entries())
    with (
        ZipFile(path, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr("sample-F_Cu.gbr", _GERBER)
    with pytest.raises(ValueError):
        check_generated_archive(path, True)


@pytest.mark.parametrize("missing", [_WORKBOOK, _HTML])
def test_enabled_generation_requires_both_reports(tmp_path: Path, missing: str) -> None:
    """An enabled archive must contain the workbook and its HTML companion."""
    entries = _entries()
    entries.pop(missing)
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("content", [b"", b"not an XLSX archive", _workbook(False)])
def test_workbook_requires_embedded_capture_media(
    tmp_path: Path, content: bytes
) -> None:
    """A workbook name or empty template is insufficient capture evidence."""
    entries = _entries()
    entries[_WORKBOOK] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize(
    "content", [b"", b"<html>No capture</html>", b'<img src="sample.png">']
)
def test_html_requires_self_contained_capture_media(
    tmp_path: Path, content: bytes
) -> None:
    """A stale external image reference must not pass as a portable report."""
    entries = _entries()
    entries[_HTML] = content
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), True)


@pytest.mark.parametrize("stale", [(_WORKBOOK,), (_HTML,), (_WORKBOOK, _HTML)])
def test_disabling_generation_excludes_each_stale_report(
    tmp_path: Path, stale: tuple[str, ...]
) -> None:
    """Regeneration with impedance disabled must remove both report members."""
    entries = _entries(False)
    enabled_entries = _entries()
    entries.update({name: enabled_entries[name] for name in stale})
    with pytest.raises(ValueError):
        check_generated_archive(_archive(tmp_path, entries), False)
