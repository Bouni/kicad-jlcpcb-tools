"""Verify explicit manufacturing ZIP contents and failure-safe publication."""

from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import Mock
from zipfile import BadZipFile, ZipFile

import pytest

import fabrication_archive
from fabrication_archive import ArchiveEntry, build_archive, collect_gerber_entries


def _source(directory: Path, name: str, content: bytes = b"manufacturing data") -> Path:
    """Create a nonempty archive input in a pytest temporary directory."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _previous_archive(destination: Path) -> bytes:
    """Create a known-valid previous export and return its exact bytes."""
    with ZipFile(destination, "w") as archive:
        archive.writestr("previous.gbr", b"previous export")
    return destination.read_bytes()


def test_collects_only_exact_legacy_suffixes_in_sorted_order(tmp_path: Path) -> None:
    """Ignore images, part lists, and misleading suffixes in the Gerber tree."""
    names = (
        "nested/z.gbr",
        "b.drl",
        "a.pdf",
        "screenshot.png",
        "parts.csv",
        "notes.txt",
        "stale.xlsx",
        "template.xlsx",
        "stale.html",
        "notgbr",
        "UPPER.GBR",
        "board.gbr.backup",
    )
    for name in names:
        _source(tmp_path, name)
    entries = collect_gerber_entries(tmp_path)
    assert [entry.name for entry in entries] == ["a.pdf", "b.drl", "z.gbr"]
    assert entries[-1] == tmp_path / "nested" / "z.gbr"


def test_missing_gerber_directory_is_reported(tmp_path: Path) -> None:
    """A missing plotting directory must not look like an empty successful scan."""
    with pytest.raises(NotADirectoryError):
        collect_gerber_entries(tmp_path / "missing")


def test_archive_has_exact_sorted_manufacturing_members(
    tmp_path: Path,
) -> None:
    """Publish only current root-level data, including when replacing a prior ZIP."""
    expected = {"copper.gbr": b"copper", "holes.drl": b"drill", "map.pdf": b"drill map"}
    inputs = [
        _source(tmp_path / "gerbers", name, data) for name, data in expected.items()
    ]
    destination = tmp_path / ("a" * 246 + ".zip")
    build_archive(destination, [*inputs, _source(tmp_path, "stale.gbr")])
    with ZipFile(destination) as archive:
        assert archive.namelist() == [*expected, "stale.gbr"]
    previous = destination.read_bytes()
    assert build_archive(destination, reversed(inputs)) == destination
    inputs[-1].unlink()
    assert destination.read_bytes() != previous
    with ZipFile(destination) as archive:
        assert archive.namelist() == list(expected)
        assert {name: archive.read(name) for name in archive.namelist()} == expected
        assert archive.testzip() is None


@pytest.mark.parametrize("name", ["sub\\board.gbr", "C:board.gbr", "board\n.gbr"])
def test_rejects_nonportable_source_basenames(tmp_path: Path, name: str) -> None:
    """A real source filename cannot become an unsafe member on another platform."""
    source = _source(tmp_path, name)
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    with pytest.raises(ValueError, match="safe root-level"):
        build_archive(destination, [source])
    assert destination.read_bytes() == previous


@pytest.mark.parametrize(
    "names", [("board.gbr", "board.gbr"), ("board.gbr", "BOARD.GBR")]
)
def test_rejects_colliding_archive_names(
    tmp_path: Path, names: tuple[str, str]
) -> None:
    """Flattening directories cannot overwrite same-name or case-folded members."""
    sources = [_source(tmp_path / str(index), name) for index, name in enumerate(names)]
    with pytest.raises(ValueError, match="Duplicate archive member"):
        build_archive(tmp_path / "production.zip", sources)


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "no_entries"])
def test_rejects_unusable_inputs_preserving_previous_archive(
    tmp_path: Path, kind: str
) -> None:
    """Incomplete exports cannot replace a previous valid ZIP."""
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    source = tmp_path / "input"
    if kind == "directory":
        source.mkdir()
    elif kind == "empty":
        source.write_bytes(b"")
    entries = [] if kind == "no_entries" else [source]
    with pytest.raises((ValueError, FileNotFoundError)):
        build_archive(destination, entries)
    assert destination.read_bytes() == previous


@pytest.mark.parametrize("failure", ["read", "write", "verify", "replace", "cleanup"])
def test_archive_failures_preserve_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Every failure phase preserves the prior ZIP and attempts temporary cleanup."""
    source = _source(tmp_path, "board.gbr")
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    original_open = Path.open

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == source:
            raise PermissionError("source is unreadable")
        return original_open(path, *args, **kwargs)

    fail = Mock(side_effect=OSError("archive operation failed"))
    target, attribute, replacement = {
        "read": (Path, "open", unreadable),
        "write": (ZipFile, "write", fail),
        "verify": (ZipFile, "testzip", Mock(return_value="board.gbr")),
        "replace": (fabrication_archive.os, "replace", fail),
        "cleanup": (ZipFile, "write", fail),
    }[failure]
    monkeypatch.setattr(target, attribute, replacement)
    if failure == "cleanup":
        monkeypatch.setattr(Path, "unlink", Mock(side_effect=OSError("cleanup denied")))
    error, message = (
        (PermissionError, "source is unreadable")
        if failure == "read"
        else (BadZipFile, "board.gbr")
        if failure == "verify"
        else (OSError, "archive operation failed")
    )
    with pytest.raises(error, match=message):
        build_archive(destination, [source])
    assert destination.read_bytes() == previous
    if failure != "cleanup":
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "board.gbr",
            "production.zip",
        ]


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_consumer_failure_restores_old_files_and_removes_new_files(
    tmp_path: Path, error_type: type[BaseException]
) -> None:
    """A failure after reading published output cannot leave the new set in place."""
    old = _source(tmp_path, "BOM.csv", b"old BOM")
    fresh = tmp_path / "CPL.csv"
    sources = [
        _source(tmp_path / "staging", "BOM.csv", b"new BOM"),
        _source(tmp_path / "staging", "CPL.csv", b"new CPL"),
    ]
    failure = error_type("consumer failed")
    with (
        pytest.raises(error_type) as caught,
        fabrication_archive.artifact_publication(tuple(zip(sources, (old, fresh)))),
    ):
        assert old.read_bytes() == b"new BOM"
        assert fresh.read_bytes() == b"new CPL"
        raise failure
    assert caught.value is failure
    assert old.read_bytes() == b"old BOM"
    assert not fresh.exists()
    assert not list(tmp_path.glob(".jlcpcb-recovery-*"))


@pytest.mark.parametrize("previous_cpl", [False, True])
def test_failed_restoration_after_bookkeeping_error_keeps_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_cpl: bool
) -> None:
    """A late database error still identifies the recoverable original on failure."""
    destinations = [tmp_path / name for name in ("BOM.csv", "CPL.csv", "GERBER.zip")]
    sources = []
    for destination in destinations:
        if destination.name != "CPL.csv" or previous_cpl:
            destination.write_bytes(b"previous " + destination.name.encode())
        sources.append(_source(tmp_path / "staging", destination.name, b"new export"))
    replace, unlink = fabrication_archive.os.replace, Path.unlink

    def fail_restore(source: Any, destination: Any) -> None:
        """Allow actual publication, but deny restoring CPL from its backup."""
        if Path(source).parent.name.startswith(".jlcpcb-recovery-") and (
            Path(destination) == destinations[1]
        ):
            raise PermissionError("restoration denied")
        replace(source, destination)

    def fail_remove(path: Path, *args: Any, **kwargs: Any) -> None:
        """Deny compensation for CPL when no prior CPL existed."""
        if path == destinations[1]:
            raise PermissionError("removal denied")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(fabrication_archive.os, "replace", fail_restore)
    if not previous_cpl:
        monkeypatch.setattr(Path, "unlink", fail_remove)
    failure = sqlite3.OperationalError("counter commit failed")
    with (
        pytest.raises(RuntimeError, match="Recovery copies:") as caught,
        fabrication_archive.artifact_publication(tuple(zip(sources, destinations))),
    ):
        raise failure
    assert caught.value.__cause__ is failure
    assert str(destinations[1]) in str(caught.value)
    assert destinations[0].read_bytes() == b"previous BOM.csv"
    assert destinations[1].read_bytes() == b"new export"
    assert destinations[2].read_bytes() == b"previous GERBER.zip"
    (recovery,) = tmp_path.glob(".jlcpcb-recovery-*")
    assert str(recovery) in str(caught.value)
    if previous_cpl:
        assert (recovery / "1").read_bytes() == b"previous CPL.csv"


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("previous_cpl", [False, True])
def test_interrupted_restoration_keeps_remaining_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[BaseException],
    previous_cpl: bool,
) -> None:
    """Interrupting compensation must preserve every not-yet-restored original."""
    destinations = [tmp_path / name for name in ("BOM.csv", "CPL.csv", "GERBER.zip")]
    sources = []
    for destination in destinations:
        if destination.name != "CPL.csv" or previous_cpl:
            destination.write_bytes(b"previous " + destination.name.encode())
        sources.append(_source(tmp_path / "staging", destination.name, b"new export"))
    replace, unlink = fabrication_archive.os.replace, Path.unlink
    interruption = error_type("stop restoring")

    def interrupt_restore(source: Any, destination: Any) -> None:
        """Restore GERBER, then interrupt before replacing CPL or reaching BOM."""
        if Path(source).parent.name.startswith(".jlcpcb-recovery-") and (
            Path(destination) == destinations[1]
        ):
            raise interruption
        replace(source, destination)

    def interrupt_remove(path: Path, *args: Any, **kwargs: Any) -> None:
        """Interrupt compensation of a file that did not previously exist."""
        if path == destinations[1]:
            raise interruption
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(fabrication_archive.os, "replace", interrupt_restore)
    if not previous_cpl:
        monkeypatch.setattr(Path, "unlink", interrupt_remove)
    with (
        pytest.raises(error_type) as caught,
        fabrication_archive.artifact_publication(tuple(zip(sources, destinations))),
    ):
        raise sqlite3.OperationalError("counter commit failed")
    assert caught.value is interruption
    assert destinations[0].read_bytes() == b"new export"
    assert destinations[1].read_bytes() == b"new export"
    assert destinations[2].read_bytes() == b"previous GERBER.zip"
    (recovery,) = tmp_path.glob(".jlcpcb-recovery-*")
    assert (recovery / "0").read_bytes() == b"previous BOM.csv"
    if previous_cpl:
        assert (recovery / "1").read_bytes() == b"previous CPL.csv"
    assert "restoration interrupted" in caplog.text
    assert str(recovery) in caplog.text
    assert str(destinations[1]) in caplog.text


def test_archive_has_exact_sorted_members_and_embedded_workbook(tmp_path: Path) -> None:
    """The explicit workbook and legacy manufacturing files are embedded once."""
    gerbers = tmp_path / "gerbers"
    copper = _source(gerbers, "copper.gbr", b"copper")
    drill = _source(gerbers, "holes.drl", b"drill")
    workbook = _source(tmp_path, "report.xlsx", b"workbook content")
    destination = tmp_path / "manufacturing.zip"
    result = build_archive(
        destination,
        [
            ArchiveEntry(drill, drill.name),
            ArchiveEntry(workbook, "Required_impedance_control.xlsx"),
            ArchiveEntry(copper, copper.name),
        ],
    )
    workbook.unlink()
    assert result == destination
    with ZipFile(destination) as archive:
        assert archive.namelist() == [
            "Required_impedance_control.xlsx",
            "copper.gbr",
            "holes.drl",
        ]
        assert archive.read("Required_impedance_control.xlsx") == b"workbook content"
        assert archive.read("copper.gbr") == b"copper"
        assert archive.testzip() is None


def test_enabled_then_disabled_does_not_reuse_stale_workbook(tmp_path: Path) -> None:
    """Rebuilding without an extra entry drops all previous impedance assets."""
    gerbers = tmp_path / "gerbers"
    _source(gerbers, "board.gbr")
    _source(gerbers, "stale.xlsx")
    _source(gerbers, "snippet.png")
    workbook = _source(tmp_path, "impedance.xlsx")
    destination = tmp_path / "production.zip"
    gerber_entries = collect_gerber_entries(gerbers)
    build_archive(destination, (*gerber_entries, ArchiveEntry(workbook, workbook.name)))
    with ZipFile(destination) as archive:
        assert "impedance.xlsx" in archive.namelist()
    build_archive(destination, gerber_entries)
    with ZipFile(destination) as archive:
        assert archive.namelist() == ["board.gbr"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../board.gbr",
        "/board.gbr",
        "sub/board.gbr",
        "sub\\board.gbr",
        "C:board.gbr",
        "C:\\board.gbr",
        "board\x00.gbr",
        "board\n.gbr",
    ],
)
def test_rejects_unsafe_member_names_before_output(tmp_path: Path, name: str) -> None:
    """Portable archive names cannot escape the root or be silently truncated."""
    source = _source(tmp_path, "board.gbr")
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    with pytest.raises(ValueError, match="safe root-level"):
        build_archive(destination, [ArchiveEntry(source, name)])
    assert destination.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "board.gbr",
        "production.zip",
    ]


@pytest.mark.parametrize(
    "names", [("board.gbr", "board.gbr"), ("board.gbr", "BOARD.GBR")]
)
def test_rejects_colliding_archive_names_with_named_entries(
    tmp_path: Path, names: tuple[str, str]
) -> None:
    """Flattening directories cannot overwrite same-name or case-folded members."""
    source = _source(tmp_path, "board.gbr")
    with pytest.raises(ValueError, match="Duplicate archive member"):
        build_archive(
            tmp_path / "production.zip", [ArchiveEntry(source, name) for name in names]
        )


def test_nested_collector_collision_is_rejected(tmp_path: Path) -> None:
    """Two accepted Gerber files with identical basenames must fail explicitly."""
    _source(tmp_path, "first/board.gbr")
    _source(tmp_path, "second/board.gbr")
    with pytest.raises(ValueError, match="Duplicate"):
        build_archive(tmp_path / "production.zip", collect_gerber_entries(tmp_path))


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "no_entries"])
def test_rejects_unusable_inputs_preserving_previous_archive_with_named_entries(
    tmp_path: Path, kind: str
) -> None:
    """Incomplete exports cannot replace a previous valid ZIP."""
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    source = tmp_path / "input"
    if kind == "directory":
        source.mkdir()
    elif kind == "empty":
        source.write_bytes(b"")
    entries = [] if kind == "no_entries" else [ArchiveEntry(source, "board.gbr")]
    with pytest.raises((ValueError, FileNotFoundError)):
        build_archive(destination, entries)
    assert destination.read_bytes() == previous


def test_unreadable_input_preserves_previous_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read permission errors propagate without creating a replacement ZIP."""
    source = _source(tmp_path, "board.gbr")
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)
    original_open = Path.open

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> Any:
        """Simulate a source read denial without depending on operating system ACLs."""
        if path == source:
            raise PermissionError("source is unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(PermissionError):
        build_archive(destination, [ArchiveEntry(source, source.name)])
    assert destination.read_bytes() == previous


@pytest.mark.parametrize("failure", ["write", "verify", "replace"])
def test_mid_export_failures_preserve_old_zip_and_remove_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failures at each publication phase leave only the prior valid artifact."""
    source = _source(tmp_path, "board.gbr")
    destination = tmp_path / "production.zip"
    previous = _previous_archive(destination)

    def fail(*args: Any, **kwargs: Any) -> None:
        """Inject an I/O failure during archive construction or publication."""
        raise OSError("injected archive failure")

    def invalid_member(archive: ZipFile) -> str:
        """Report a simulated member checksum mismatch."""
        return "board.gbr"

    if failure == "write":
        monkeypatch.setattr(ZipFile, "write", fail)
    elif failure == "verify":
        monkeypatch.setattr(ZipFile, "testzip", invalid_member)
    else:
        monkeypatch.setattr(fabrication_archive.os, "replace", fail)
    with pytest.raises((OSError, BadZipFile)):
        build_archive(destination, [ArchiveEntry(source, source.name)])
    assert destination.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "board.gbr",
        "production.zip",
    ]


def test_mixed_paths_and_named_impedance_documents(tmp_path: Path) -> None:
    """Keep path-based Gerbers compatible with explicitly named workbook and HTML."""
    gerbers = tmp_path / "gerbers"
    _source(gerbers, "board.gbr", b"copper")
    workbook = _source(tmp_path, "temporary.xlsx", b"workbook")
    report = _source(tmp_path, "temporary.html", b"report")
    destination = tmp_path / "manufacturing.zip"
    collected = collect_gerber_entries(gerbers)
    build_archive(
        destination,
        (
            *collected,
            ArchiveEntry(workbook, "Required_impedance_control.xlsx"),
            ArchiveEntry(report, "Controlled_impedance.html"),
        ),
    )
    with ZipFile(destination) as archive:
        assert archive.namelist() == [
            "Controlled_impedance.html",
            "Required_impedance_control.xlsx",
            "board.gbr",
        ]
        assert archive.read("Controlled_impedance.html") == b"report"
        assert archive.read("Required_impedance_control.xlsx") == b"workbook"
        assert archive.read("board.gbr") == b"copper"
    build_archive(destination, collected)
    with ZipFile(destination) as archive:
        assert archive.namelist() == ["board.gbr"]


def test_mixed_entries_reject_colliding_names(tmp_path: Path) -> None:
    """Implicit path names share the same portable namespace as explicit names."""
    source = _source(tmp_path, "board.gbr")
    destination = tmp_path / "manufacturing.zip"
    previous = _previous_archive(destination)
    with pytest.raises(ValueError, match="Duplicate archive member"):
        build_archive(destination, [source, ArchiveEntry(source, "BOARD.GBR")])
    assert destination.read_bytes() == previous
