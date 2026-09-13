"""Create manufacturing archives from explicit, validated member lists."""

from collections.abc import Sequence
import logging
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile


def collect_gerber_entries(directory: Path) -> tuple[Path, ...]:
    """Collect the existing Gerber, drill, and PDF profile without extra assets."""
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Gerber directory does not exist: {directory}")
    return tuple(
        sorted(
            (
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix in {".gbr", ".drl", ".pdf"}
            ),
            key=lambda path: (path.name, str(path)),
        )
    )


def _validate_entries(entries: Sequence[Path]) -> tuple[Path, ...]:
    """Reject ambiguous names and unusable inputs before opening an output file."""
    entries = tuple(Path(entry) for entry in entries)
    if not entries:
        raise ValueError("A manufacturing archive must contain at least one file")
    names = set()
    for entry in entries:
        name = entry.name
        if (
            not name
            or name in {".", ".."}
            or any(character in name for character in "/\\:")
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ValueError(
                f"Archive member must have a safe root-level name: {name!r}"
            )
        folded_name = name.casefold()
        if folded_name in names:
            raise ValueError(f"Duplicate archive member name: {name}")
        names.add(folded_name)
        source = entry
        if not source.is_file():
            raise FileNotFoundError(f"Archive source is not an existing file: {source}")
        with source.open("rb") as stream:
            if not stream.read(1):
                raise ValueError(f"Archive source is empty: {source}")
    return tuple(sorted(entries, key=lambda entry: entry.name))


def build_archive(destination: Path, entries: Sequence[Path]) -> Path:
    """Validate and atomically publish a ZIP, preserving the previous ZIP on error.

    The destination directory must already exist. Only the supplied ``entries``
    become archive members; unrelated directory contents are never added.
    Temporary output is created beside the destination so publication stays on
    the same filesystem.
    """
    destination = Path(destination)
    members = _validate_entries(entries)
    with tempfile.NamedTemporaryFile(
        prefix=".jlcpcb-zip-",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(
            temporary_path, "w", compression=ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for entry in members:
                archive.write(entry, entry.name)
        with ZipFile(temporary_path, "r") as archive:
            invalid_member = archive.testzip()
            if invalid_member is not None:
                raise BadZipFile(f"Archive failed CRC verification: {invalid_member}")
            if archive.namelist() != [entry.name for entry in members]:
                raise BadZipFile("Archive does not match the requested member list")
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "Could not remove temporary archive %s", temporary_path, exc_info=True
            )
    return destination
