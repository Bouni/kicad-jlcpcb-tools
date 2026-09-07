"""Real temporary storage and reopening helpers for correction tests."""

from collections.abc import Iterable
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import types
from typing import Any, Union


def make_library(
    module: types.ModuleType,
    directory: Union[str, Path],
    rows: Iterable[tuple[Any, ...]] = (),
    *,
    local: bool = False,
) -> Any:
    """Create a real Library with all paths confined to the supplied directory."""
    directory = Path(directory)
    global_dir = directory / "global"
    project_dir = directory / "project"
    global_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "jlcpcb").mkdir(parents=True, exist_ok=True)
    library = module.Library.__new__(module.Library)
    library._migration_session_diagnostics = {}
    library._known_legacy_sources = {}
    library.logger = logging.getLogger(__name__)
    library.parent = types.SimpleNamespace(settings={}, project_path=str(project_dir))
    library.datadir = str(global_dir)
    library.partsdb_file = str(global_dir / "parts.db")
    library.rotationsdb_file = str(global_dir / "rotations.db")
    library.mappingsdb_file = str(global_dir / "mappings.db")
    library.globalcorrectionsdb_file = str(global_dir / "corrections.db")
    library.localcorrectionsdb_file = str(project_dir / "jlcpcb" / "project.db")
    library.correctionsdb_file = (
        library.localcorrectionsdb_file if local else library.globalcorrectionsdb_file
    )
    library.state = module.LibraryState.INITIALIZED
    library.create_correction_table()
    seed_raw(library, rows)
    return library


def fresh_library(library: Any) -> Any:
    """Reopen using the same paths without inheriting cached correction state."""
    fresh = type(library).__new__(type(library))
    fresh._migration_session_diagnostics = {}
    fresh._known_legacy_sources = {}
    for name in (
        "logger",
        "parent",
        "datadir",
        "partsdb_file",
        "rotationsdb_file",
        "mappingsdb_file",
        "globalcorrectionsdb_file",
        "localcorrectionsdb_file",
        "correctionsdb_file",
        "state",
    ):
        setattr(fresh, name, getattr(library, name))
    return fresh


def seed_raw(library: Any, rows: Iterable[tuple[Any, ...]]) -> None:
    """Persist historical malformed values without using the validated writer."""
    with closing(sqlite3.connect(library.correctionsdb_file)) as con, con:
        con.executemany("INSERT INTO correction VALUES (?, ?, ?, ?)", rows)


def raw_rows(library: Any) -> list[tuple[Any, ...]]:
    """Read exact persisted values through an independent SQLite connection."""
    with closing(sqlite3.connect(library.correctionsdb_file)) as con:
        return con.execute("SELECT rowid, * FROM correction ORDER BY rowid").fetchall()
