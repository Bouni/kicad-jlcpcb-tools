"""Safety utilities for KiCad schematic operations.

Provides lockfile detection, automatic project schematic resolution,
hierarchical sheet discovery, and atomic file write/backup operations.
"""

from collections.abc import Callable, Collection, Iterator
import contextlib
import json
import logging
import os
import re
import shutil
import stat
from typing import IO, Any, Optional
import uuid

logger = logging.getLogger(__name__)

# One token of a KiCad S-expression: a bracket, a quoted string or an atom.
_SEXPR_TOKEN_RX = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')

# KiCad writes "Sheetfile" and still reads the older "Sheet file".
_SHEET_FILE_PROPERTIES = ("sheetfile", "sheet file")


class SchematicLockedError(RuntimeError):
    """Raised when KiCad has a lock on schematics that are about to be written.

    `locks` holds every locked schematic path with its lockfile contents.
    """

    def __init__(self, locks: list[tuple[str, dict[str, Any]]]) -> None:
        self.locks = locks
        described = [
            (
                os.path.basename(path),
                f"{info.get('username') or 'unknown user'}@"
                f"{info.get('hostname') or 'unknown host'}",
                get_schematic_lock_path(path),
            )
            for path, info in locks
        ]
        if len(described) == 1:
            name, owner, lock_path = described[0]
            message = (
                f"Schematic file '{name}' is locked by {owner}. It is open in the "
                "Schematic Editor, or KiCad quit without removing its lock file:\n"
                f"{lock_path}"
            )
        else:
            message = (
                f"{len(described)} schematic files are locked. Each is open in a "
                "Schematic Editor, or KiCad quit without removing its lock file:\n"
                + "\n".join(
                    f"'{name}' by {owner}: {lock_path}"
                    for name, owner, lock_path in described
                )
            )
        super().__init__(message)


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

    KiCad 7.0.6 through 10 mark an open schematic with a lockfile holding only
    the owner's username and hostname. KiCad keeps no OS lock on that file and
    records no process ID, so a lockfile left behind by a crash cannot be told
    apart from a live session; any lockfile counts as a lock. KiCad 7.0.0 to
    7.0.5 kept their locks in KiCad's own lock directory, which this does not
    check.

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


def _lock_location(schematic_path: str) -> str:
    """Return the lockfile path of a schematic with its directory resolved.

    Two names for one directory hold one lock file; two names for one
    schematic in different directories, such as a symlink and its target,
    hold two.
    """
    directory, name = os.path.split(get_schematic_lock_path(schematic_path))
    return os.path.join(os.path.realpath(directory), name)


def assert_schematics_not_locked(
    schematic_paths: list[str],
    approved: Collection[str] = (),
) -> None:
    """Verify that none of the given schematics are locked.

    Args:
        schematic_paths: Schematics that are about to be written.
        approved: Paths reported by an earlier SchematicLockedError whose
            locks the user has chosen to write past. Each approves the lock
            file beside that path only: a lock beside another name of the
            same schematic, or one taken since, is still reported.

    Raises:
        SchematicLockedError: Naming every schematic with an unapproved lock.

    """
    approved_locks = {_lock_location(p) for p in approved}
    locks = []
    checked: set[str] = set()
    for path in schematic_paths:
        # KiCad names the lock after the path it opened, so a schematic reached
        # through a symlink may be locked beside the link or beside its target.
        for candidate in dict.fromkeys((path, os.path.realpath(path))):
            location = _lock_location(candidate)
            if location in checked or location in approved_locks:
                continue
            checked.add(location)
            lock_info = check_schematic_lock(candidate)
            if lock_info is not None:
                locks.append((candidate, lock_info))
    if locks:
        raise SchematicLockedError(locks)


def resolve_project_schematics(
    project_path: Optional[str],
    board_filename: Optional[str],
) -> list[str]:
    """Return the top-level schematics of the board's KiCad project.

    Pcbnew loads the project named after the board. KiCad 10 lists that
    project's top-level sheets in its .kicad_pro, and eeschema loads every
    one of them: a listed file that is missing is replaced by
    `<project>.kicad_sch` when that exists, or else skipped. Projects
    without the list have one root, named after the project. Any other
    schematic in the directory may belong to a different board, so it is
    left for the user to choose.

    Args:
        project_path: Directory containing the KiCad project.
        board_filename: Filename or full path of the PCB file (e.g. 'board.kicad_pcb').

    Returns:
        Absolute paths of the existing top-level schematics, in project
        order; empty if none is found.

    """
    if not project_path or not os.path.isdir(project_path) or not board_filename:
        return []

    project_name = os.path.splitext(os.path.basename(board_filename))[0]
    default_root = f"{project_name}.kicad_sch"
    try:
        with open(
            os.path.join(project_path, f"{project_name}.kicad_pro"), encoding="utf-8"
        ) as f:
            listed = json.load(f)["schematic"]["top_level_sheets"]
    except (OSError, ValueError, KeyError, TypeError):
        listed = []
    file_names = [
        sheet["filename"]
        for sheet in (listed if isinstance(listed, list) else [])
        if isinstance(sheet, dict) and isinstance(sheet.get("filename"), str)
    ]

    roots: list[str] = []
    for file_name in file_names or [default_root]:
        path = os.path.join(project_path, file_name)
        if not os.path.isfile(path):
            path = os.path.join(project_path, default_root)
        if not os.path.isfile(path):
            if file_names:
                logger.warning(
                    "Top-level sheet '%s' listed in %s.kicad_pro does not exist",
                    file_name,
                    project_name,
                )
            continue
        path = os.path.abspath(path)
        if path not in roots:
            roots.append(path)
    return roots


def _sheet_file_names(content: str) -> list[str]:
    """Return the file names of the hierarchical sheets placed in a schematic.

    Only the Sheetfile property of a `(sheet ...)` form names a sheet file;
    a symbol can carry a custom property with the same name.
    """
    # The head atom and the quoted arguments of each list still open
    heads: list[Optional[str]] = []  # noqa: UP045
    strings: list[list[str]] = []
    names: list[str] = []
    for match in _SEXPR_TOKEN_RX.finditer(content):
        token = match.group()
        if token == "(":
            heads.append(None)
            strings.append([])
        elif token == ")":
            if not heads:
                continue
            head = heads.pop()
            args = strings.pop()
            if (
                head == "property"
                and heads[-1:] == ["sheet"]
                and len(args) >= 2
                and args[0].lower() in _SHEET_FILE_PROPERTIES
            ):
                names.append(args[1])
        elif heads and heads[-1] is None:
            heads[-1] = token
        elif heads and heads[-1] == "property" and token.startswith('"'):
            strings[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]))
    return names


def collect_schematic_hierarchy(root_sch_path: str) -> list[str]:
    """Recursively collect all schematic files in a project hierarchy.

    Sheet file paths are resolved against the directory of the schematic
    that references them, as KiCad does. Symlinks are kept rather than
    resolved: KiCad names its lock file after the path it opened and looks
    for sub-sheets beside it, so a schematic opened through a link is locked
    beside the link, and its sub-sheets live beside the link. One file
    reached under two paths is walked under each, since a sub-sheet named
    with `..` can lead somewhere else from each of them.

    Args:
        root_sch_path: Absolute path to the root .kicad_sch file.

    Returns:
        List of absolute, normalized paths to all discovered schematic
        files as they are reached, ordered depth-first starting with the
        root. Two paths may name one file.

    Raises:
        FileNotFoundError: If a referenced sheet file does not exist, so an
            export can stop before it writes any sheet.
        OSError: If a schematic in the hierarchy cannot be read.

    """
    discovered: list[str] = []
    visited: set[str] = set()
    # A sheet reached again under the same real file and real directory
    # while it is still being walked is taken as a loop, through a sheet
    # that uses an ancestor or through a directory link to a parent.
    ancestors: set[tuple[str, str]] = set()

    def _traverse(current_path: str) -> None:
        opened = os.path.normpath(os.path.abspath(current_path))
        base_dir = os.path.dirname(opened)
        real = (os.path.realpath(base_dir), os.path.realpath(opened))
        if opened in visited or real in ancestors:
            return
        visited.add(opened)
        discovered.append(opened)

        with open(opened, encoding="utf-8", errors="replace") as f:
            content = f.read()

        ancestors.add(real)
        for name in _sheet_file_names(content):
            sheet_subpath = name.strip()
            if not sheet_subpath:
                continue
            child_path = os.path.normpath(os.path.join(base_dir, sheet_subpath))
            if not os.path.isfile(child_path):
                raise FileNotFoundError(
                    f"Sheet file '{sheet_subpath}' used in "
                    f"'{os.path.basename(opened)}' does not exist: {child_path}"
                )
            _traverse(child_path)
        ancestors.discard(real)

    _traverse(root_sch_path)
    return discovered


def _copy_mode_and_times(source: str, target: str) -> None:
    """Copy permission bits and access/modification times, but not file flags.

    shutil.copystat also copies flags such as macOS's immutable flag, which
    would leave a temporary file that can be neither renamed nor removed.
    """
    shutil.copymode(source, target)
    source_stat = os.stat(source)
    os.utime(target, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


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
    `path`. If anything but the metadata copy fails, `path` is untouched.

    The metadata copy is best effort: a failure is logged and the file still
    replaces `path`, because its contents are what must not be lost and a
    filesystem that refuses permission or time changes should not block an
    export that never needed them.

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
            try:
                copy_metadata(metadata_source, temp_path)
            except OSError as exc:
                logger.warning(
                    "Could not copy file metadata from '%s' to '%s': %s",
                    metadata_source,
                    path,
                    exc,
                )
        os.replace(temp_path, path)
    except BaseException:
        # A read-only mode copied onto the file would block its removal on Windows.
        with contextlib.suppress(OSError):
            os.chmod(temp_path, stat.S_IREAD | stat.S_IWRITE)
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise


def atomic_write_schematic(path: str, content: str) -> None:
    """Atomically write content to a schematic file, safely creating a backup.

    Args:
        path: Path to the target schematic file. A symlink is followed, as
            KiCad follows it when it saves: the file it points to is
            replaced and the link is kept.
        content: The text content to write.

    An existing target is first copied to `<path>_old`, beside the file
    itself rather than a link to it, and is not replaced unless that backup
    is complete.

    Raises:
        OSError: If backing up, writing or replacing fails.

    """
    abs_path = os.path.realpath(path)

    # 1. Back up the existing file; an earlier backup stays until this one is whole
    if os.path.exists(abs_path):
        with open(abs_path, "rb") as original:
            previous = original.read()
        # The backup also keeps the original's modification time
        with _replacing(
            f"{abs_path}_old", abs_path, _copy_mode_and_times, mode="wb"
        ) as backup:
            backup.write(previous)

    # 2. Write the new content beside the target and rename it into place
    with _replacing(abs_path, abs_path, mode="w", encoding="utf-8") as f:
        f.write(content)
