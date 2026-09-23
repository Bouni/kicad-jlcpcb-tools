"""Archive the ordinary-mode mapping cache after assignments are saved."""

from collections.abc import Callable
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Optional


def _active_table(connection: sqlite3.Connection) -> Optional[str]:
    """Return the actual historical table spelling without requiring a write lock."""
    row = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'part_info' COLLATE NOCASE"
    ).fetchone()
    return row[0] if row is not None else None


class LegacyPartInfoChanged(sqlite3.OperationalError):
    """Active recovery data changed since the caller established row coverage."""


def legacy_part_info_digest(connection: sqlite3.Connection) -> Optional[str]:
    """Fingerprint the active schema and all rows within the caller's transaction."""
    table = _active_table(connection)
    if table is None:
        return None
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE tbl_name = 'part_info' COLLATE NOCASE ORDER BY type, name"
    ).fetchall()
    rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    # SQLite yields only None, numbers, strings, and bytes. Their repr retains
    # exact types/values; sorting ignores row order without losing duplicates.
    payload = repr((schema, sorted(rows, key=repr))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def legacy_part_info_generation(connection: sqlite3.Connection) -> Optional[str]:
    """Identify an active table generation independently of ordinary row updates.

    Retained archives distinguish the normal older-plugin recreation workflow.
    Physical root pages and schema version are intentionally excluded: VACUUM,
    unrelated tables and audit metadata do not create a new generation.
    """
    active = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='part_info' COLLATE NOCASE"
    ).fetchone()
    if active is None:
        return None
    archives = [
        row
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if re.fullmatch(
            r"part_info_retired(?:_(?:[2-9]|[1-9][0-9]+))?", row[0].casefold()
        )
    ]
    return hashlib.sha256(repr((active, archives)).encode("utf-8")).hexdigest()


def retire_legacy_part_info(
    dbfile: str,
    *,
    expected_digest: Optional[str] = None,
    source_check: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Archive an active legacy table after the caller establishes full coverage.

    The caller must successfully migrate eligible missing assignments and save
    them, while retaining active recovery data when any recoverable row remains
    unaccounted for. This helper preserves every historical row and schema item
    by renaming the table. Existing archives are never merged or overwritten.

    No database or project directory is created here. Explicit transactions
    roll back a failed rename or commit, and mode=rw prevents recreating a
    concurrently removed file. A busy database raises immediately for retry.
    When supplied, expected_digest guards the recovery plan against another
    board or an older plugin changing active rows before the rename lock. The
    optional source_check revalidates saved PCB evidence under that same lock.
    """
    path = Path(dbfile)
    if not path.exists():
        return None

    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0
    )
    try:
        if _active_table(connection) is None:
            return None
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            table = _active_table(connection)
            if table is None:
                return None
            if (
                expected_digest is not None
                and legacy_part_info_digest(connection) != expected_digest
            ):
                raise LegacyPartInfoChanged(
                    "Legacy assignments changed since recovery was checked; "
                    "retain the active table and check recovery again."
                )
            names = {
                row[0].casefold()
                for row in connection.execute("SELECT name FROM sqlite_master")
            }
            archive = "part_info_retired"
            suffix = 2
            while archive.casefold() in names:
                archive = f"part_info_retired_{suffix}"
                suffix += 1
            if source_check is not None and not source_check():
                raise LegacyPartInfoChanged(
                    "Saved PCB recovery sources changed; retain the active table "
                    "and verify persisted assignments again."
                )
            # The source is constrained by _active_table; destinations are
            # generated from a fixed prefix and integers, never user input.
            connection.execute(f'ALTER TABLE "{table}" RENAME TO "{archive}"')
        return archive
    finally:
        connection.close()
