"""Reserve the existing project counter while prepared outputs are published."""

from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
import sqlite3
from typing import Optional, Union

from ..core.file_lock import file_lock


class GenerationCountConflict(ValueError):
    """Another generation changed the count used to prepare these outputs."""


@contextmanager
def publication_guard(dbfile: Union[str, Path], timeout: float = 5.0) -> Iterator[None]:
    """Exclude competing publication until counter and file recovery completes.

    This must enclose both the counter and artifact contexts. SQLite can release
    its writer lock before artifact compensation, including automatic rollback
    after an I/O error; the sidecar lock prevents that recovery overwriting a
    different process's newly successful output.
    """
    path = Path(dbfile).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(
        str(path) + ".generation.lock",
        timeout=timeout,
        timeout_message="Another KiCad window or process is publishing fabrication files. Try again.",
    ):
        yield


@contextmanager
def generation_count_transaction(
    dbfile: Union[str, Path], expected_count: Optional[int] = None
) -> Iterator[int]:
    """Commit one reserved increment only after the publication body succeeds.

    Enter after expensive preparation and hooks, keeping the writer lock short.
    The connection context rolls back both body errors and failed commits before
    closing. Publication callers must hold publication_guard() around both this
    context and the artifact context until all compensation completes.
    """
    path = Path(dbfile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata ("
            "key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", ("generation_count",)
        ).fetchone()
        current = 0
        if row is not None:
            with suppress(ValueError, TypeError):
                current = max(0, int(row[0]))
        if expected_count is not None and current != expected_count:
            raise GenerationCountConflict(
                f"The generation count changed from {expected_count} to {current}; "
                "generate again."
            )
        next_count = current + 1
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("generation_count", str(next_count)),
        )
        yield next_count
