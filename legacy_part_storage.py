"""Retire the ordinary-mode mapping cache after assignments are saved."""

from pathlib import Path
import sqlite3


def retire_legacy_part_info(dbfile: str) -> None:
    """Drop only an existing legacy table after the caller completes auto-save.

    Variant and ordinary assignments now share the board as their source of
    truth. The old ordinary-mode cache must survive failed or skipped saves, so
    the caller owns that boundary. No database or project directory is created
    here; other tables (including generation counters) remain intact.

    A busy or unwritable database raises immediately so the window can offer a
    retry. Explicit transactions make a failed drop or commit leave the old
    table intact, and mode=rw prevents recreating a concurrently removed file.
    """
    path = Path(dbfile)
    if not path.exists():
        return

    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0
    )
    try:
        legacy_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'part_info' COLLATE NOCASE"
        ).fetchone()
        if legacy_table is None:
            return
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS part_info")
    finally:
        connection.close()
