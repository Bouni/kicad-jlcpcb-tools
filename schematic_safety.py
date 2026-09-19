"""Safety utilities for KiCad schematic operations.

Provides lockfile detection, automatic project schematic resolution,
hierarchical sheet discovery, and atomic file write/backup operations.
"""

from collections.abc import Callable, Iterator
import contextlib
import json
import logging
import os
import re
import shutil
from typing import IO, Any, Optional
import uuid

logger = logging.getLogger(__name__)

# Matches Sheetfile properties inside KiCad schematic files:
# e.g., (property "Sheetfile" "subsheet.kicad_sch" ...)
SHEETFILE_PROPERTY_RX = re.compile(
    r'\(property\s+"Sheetfile"\s+"([^"]+)"',
    re.IGNORECASE,
)


class SchematicLockedError(RuntimeError):
    """Raised when an operation is attempted on a locked schematic."""

    def __init__(
        self,
        schematic_path: str,
        lock_info: Optional[dict[str, Any]] = None,  # noqa: UP045
    ) -> None:
        self.schematic_path = schematic_path
        self.lock_info = lock_info or {}
        user = self.lock_info.get("username") or "unknown user"
        host = self.lock_info.get("hostname") or "unknown host"
        super().__init__(
            f"Schematic file '{os.path.basename(schematic_path)}' is locked by "
            f"{user}@{host}. It is open in the Schematic Editor, or KiCad quit "
            "without removing its lock file:\n"
            f"{get_schematic_lock_path(schematic_path)}"
        )


def get_schematic_lock_path(schematic_path: str) -> str:
    """Return the expected lockfile path for a schematic file.

    KiCad creates '~<basename>.lck' alongside the open file.
    """
    directory = os.path.dirname(os.path.abspath(schematic_path))
    basename = os.path.basename(schematic_path)
    return os.path.join(directory, f"~{basename}.lck")


def check_schematic_lock(
    schematic_path: str,
) -> Optional[dict[str, Any]]:  # noqa: UP045
    """Check if a schematic file is currently locked by KiCad / eeschema.

    KiCad 7 through 10 mark an open schematic with a lockfile holding only the
    owner's username and hostname. KiCad keeps no OS lock on that file and
    records no process ID, so a lockfile left behind by a crash cannot be told
    apart from a live session; any lockfile counts as a lock.

    Args:
        schematic_path: Path to the .kicad_sch file.

    Returns:
        A dict containing lock info (e.g. {'username': '...', 'hostname': '...'})
        if locked, or None if no lockfile exists.

    """
    lock_path = get_schematic_lock_path(schematic_path)
    if not os.path.exists(lock_path):
        return None

    try:
        with open(lock_path, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {"raw": str(data)}
    except (ValueError, OSError) as exc:
        logger.warning(
            "Lockfile '%s' exists but could not be parsed as JSON: %s",
            lock_path,
            exc,
        )
        return {"raw": "active_lock", "error": str(exc)}


def assert_schematics_not_locked(schematic_paths: list[str]) -> None:
    """Verify that none of the given schematics are locked.

    Raises:
        SchematicLockedError: If any schematic has an active lockfile.

    """
    for path in schematic_paths:
        lock_info = check_schematic_lock(path)
        if lock_info is not None:
            raise SchematicLockedError(path, lock_info)


def resolve_project_schematic(
    project_path: str,
    board_filename: str,
) -> Optional[str]:  # noqa: UP045
    """Auto-detect the root schematic path for a given board.

    Pcbnew loads the KiCad project named after the board, and a project's
    root schematic is named after the project, so this looks only for
    `<project_path>/<board_stem>.kicad_sch`. Any other schematic in the
    directory may belong to a different board, so it is left for the user
    to choose.

    Args:
        project_path: Directory containing the KiCad project.
        board_filename: Filename or full path of the PCB file (e.g. 'board.kicad_pcb').

    Returns:
        The absolute path to the root schematic if found, else None.

    """
    if not project_path or not os.path.isdir(project_path):
        return None

    board_stem = os.path.splitext(os.path.basename(board_filename))[0]
    expected_path = os.path.join(project_path, f"{board_stem}.kicad_sch")
    if os.path.isfile(expected_path):
        return os.path.abspath(expected_path)

    return None


def collect_schematic_hierarchy(root_sch_path: str) -> list[str]:
    """Recursively collect all schematic files in a project hierarchy.

    Sheet file paths are resolved against the directory of the schematic
    that references them, as KiCad does.

    Args:
        root_sch_path: Absolute path to the root .kicad_sch file.

    Returns:
        List of absolute paths to all discovered schematic files,
        ordered depth-first starting with the root.

    Raises:
        FileNotFoundError: If a referenced sheet file does not exist, so an
            export can stop before it writes any sheet.
        OSError: If a schematic in the hierarchy cannot be read.

    """
    discovered: list[str] = []
    visited: set[str] = set()

    def _traverse(current_path: str) -> None:
        norm = os.path.realpath(current_path)
        if norm in visited:
            return
        visited.add(norm)
        discovered.append(norm)

        with open(norm, encoding="utf-8", errors="replace") as f:
            content = f.read()

        base_dir = os.path.dirname(norm)
        for match in SHEETFILE_PROPERTY_RX.finditer(content):
            sheet_subpath = match.group(1).strip()
            if not sheet_subpath:
                continue
            child_path = os.path.normpath(os.path.join(base_dir, sheet_subpath))
            if not os.path.isfile(child_path):
                raise FileNotFoundError(
                    f"Sheet file '{sheet_subpath}' used in "
                    f"'{os.path.basename(norm)}' does not exist: {child_path}"
                )
            _traverse(child_path)

    _traverse(root_sch_path)
    return discovered


@contextlib.contextmanager
def _replacing(
    path: str,
    metadata_source: str,
    copy_metadata: Callable[[str, str], object] = shutil.copymode,
    **open_args: Any,
) -> Iterator[IO[Any]]:
    """Yield a file whose contents replace `path` once the block completes.

    The file is written beside `path` (so the final rename stays on one
    filesystem), synced, given `metadata_source`'s permissions (or whatever
    `copy_metadata` copies) when that file exists, and then renamed over
    `path`. On any failure `path` is untouched.

    It is created with mode 0o666 so the kernel applies the umask:
    tempfile.mkstemp would force 0o600, and reading the umask with os.umask
    changes it for the whole process while worker threads may create files.
    """
    directory, name = os.path.split(path)
    temp_path = os.path.join(directory, f".{name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temp_path, flags, 0o666)
    try:
        with os.fdopen(fd, **open_args) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(metadata_source):
            with contextlib.suppress(OSError):
                copy_metadata(metadata_source, temp_path)
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise


def atomic_write_schematic(
    path: str,
    content: str,
    make_backup: bool = True,
) -> None:
    """Atomically write content to a schematic file, safely creating a backup.

    Args:
        path: Path to the target schematic file.
        content: The text content to write.
        make_backup: If True and target exists, copies existing target to
            `<path>_old` first. The target is not replaced unless that
            backup is complete.

    Raises:
        OSError: If backing up, writing or replacing fails.

    """
    abs_path = os.path.abspath(path)

    # 1. Back up the existing file; an earlier backup stays until this one is whole
    if make_backup and os.path.exists(abs_path):
        with open(abs_path, "rb") as original:
            previous = original.read()
        # copystat also keeps the original's modification time on the backup
        with _replacing(
            f"{abs_path}_old", abs_path, shutil.copystat, mode="wb"
        ) as backup:
            backup.write(previous)

    # 2. Write the new content beside the target and rename it into place
    with _replacing(abs_path, abs_path, mode="w", encoding="utf-8") as f:
        f.write(content)
