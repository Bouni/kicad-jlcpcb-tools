"""Verify explicit manufacturing ZIP contents and failure-safe publication."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zipfile import BadZipFile, ZipFile

import pytest

import fabrication_archive
from fabrication_archive import build_archive, collect_gerber_entries


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
