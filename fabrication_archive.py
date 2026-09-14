"""Create manufacturing archives from explicit, validated member lists."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import shutil
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


def publish_artifact_set(pairs: Sequence[tuple[Path, Path]]) -> None:
    """Publish a set immediately, restoring previous files if replacement fails."""
    with artifact_publication(pairs):
        pass


@contextmanager
def artifact_publication(pairs: Sequence[tuple[Path, Path]]) -> Iterator[None]:
    """Keep published files recoverable until the caller completes bookkeeping.

    Filesystem APIs cannot atomically replace several filenames. Keep recovery
    copies through the context body and compensate replacement or body failures.
    If compensation itself fails, retain the recovery directory and report it;
    callers must not label that generation successful. This is not a promise of
    atomic publication with a database across process termination or power loss.
    """
    if not pairs:
        raise ValueError("No fabrication artifacts were prepared")
    paths = tuple((Path(source), Path(destination)) for source, destination in pairs)
    destinations = [str(destination.absolute()).casefold() for _, destination in paths]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Fabrication artifacts have conflicting destinations")
    for source, destination in paths:
        if not source.is_file():
            raise FileNotFoundError(f"Fabrication artifact is missing: {source}")
        if source.stat().st_size == 0:
            raise ValueError(f"Fabrication artifact is empty: {source}")
        if not destination.parent.is_dir():
            raise NotADirectoryError(destination.parent)
        if destination.exists() and not destination.is_file():
            raise ValueError(f"Artifact destination is not a file: {destination}")
    recovery = Path(
        tempfile.mkdtemp(prefix=".jlcpcb-recovery-", dir=paths[0][1].parent)
    )
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    preserve_recovery = False
    try:
        for index, (_, destination) in enumerate(paths):
            if destination.exists():
                backup = recovery / str(index)
                shutil.copy2(destination, backup)
                backups[destination] = backup
        try:
            for source, destination in paths:
                os.replace(source, destination)
                published.append(destination)
            yield
        except BaseException as error:
            # Until compensation finishes, these may be the only remaining
            # originals. An interruption must never make finally discard them.
            preserve_recovery = True
            failed_restore = []
            try:
                for destination in reversed(published):
                    try:
                        if destination in backups:
                            os.replace(backups[destination], destination)
                        else:
                            destination.unlink(missing_ok=True)
                    except OSError:
                        failed_restore.append(str(destination))
            except BaseException:
                logging.getLogger(__name__).exception(
                    "Fabrication restoration interrupted. Check these output files: "
                    "%s. Recovery copies: %s",
                    ", ".join(str(path) for path in published),
                    recovery,
                )
                raise
            if failed_restore:
                raise RuntimeError(
                    "Fabrication publication failed and these files could not be "
                    f"restored: {', '.join(failed_restore)}. Recovery copies: {recovery}"
                ) from error
            preserve_recovery = False
            raise
    finally:
        if not preserve_recovery:
            try:
                shutil.rmtree(recovery)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Could not remove fabrication recovery directory %s",
                    recovery,
                    exc_info=True,
                )
