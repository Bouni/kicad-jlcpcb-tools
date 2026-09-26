"""Consolidate legacy global plugin databases into a single global.db."""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
from typing import Optional

GLOBAL_DB_NAME = "global.db"
CORRECTIONS_LEGACY_NAME = "corrections.db"
MAPPINGS_LEGACY_NAME = "mappings.db"
LEGACY_IMPORT_TABLE = "legacy_global_import"

# Migrate in a fixed order so logs and partial recovery stay deterministic.
_LEGACY_SOURCES: Sequence[tuple[str, str]] = (
    (CORRECTIONS_LEGACY_NAME, "corrections"),
    (MAPPINGS_LEGACY_NAME, "part preferences"),
)


@dataclass(frozen=True)
class LegacyDatabaseOutcome:
    """Result of attempting to migrate one legacy global database file."""

    filename: str
    label: str
    status: str  # "absent", "migrated", or "failed"
    error: Optional[str] = None  # noqa: UP045


@dataclass(frozen=True)
class MigrationResult:
    """Outcomes for every legacy global database considered during startup."""

    outcomes: tuple[LegacyDatabaseOutcome, ...]

    def uses_global(self, filename: str) -> bool:
        """Return True when that domain should be served from global.db."""
        for outcome in self.outcomes:
            if outcome.filename == filename:
                return outcome.status in {"absent", "migrated"}
        return True


def global_db_path(datadir: str) -> str:
    """Return the path of the consolidated global plugin database."""
    return os.path.join(datadir, GLOBAL_DB_NAME)


def legacy_db_path(datadir: str, filename: str) -> str:
    """Return the path of a legacy global database under datadir."""
    return os.path.join(datadir, filename)


def migrate_legacy_global_databases(
    datadir: str,
    logger: Optional[logging.Logger] = None,  # noqa: UP045
) -> MigrationResult:
    """Copy each present legacy global DB into global.db once, then leave it.

    Migration is independent per legacy file. A failure leaves that file in place
    for the next startup while other files may still migrate successfully. Each
    successful import records the legacy filename in global.db so a later
    reappearance of that file never overwrites already-migrated tables. Legacy
    files are retained so an older install or a rollback can still read them.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    destination = Path(global_db_path(datadir))
    log.debug(
        "Checking for legacy global databases to migrate into %s",
        destination,
    )
    outcomes: list[LegacyDatabaseOutcome] = []
    for filename, label in _LEGACY_SOURCES:
        outcomes.append(_migrate_one_legacy_database(datadir, filename, label, log))
    migrated = [item.filename for item in outcomes if item.status == "migrated"]
    failed = [item.filename for item in outcomes if item.status == "failed"]
    if not migrated and not failed:
        log.debug(
            "No legacy global databases found; using %s for corrections and "
            "part preferences.",
            destination,
        )
    elif migrated or failed:
        log.info(
            "Global database migration finished. Migrated: %s. Failed: %s. "
            "Destination: %s",
            ", ".join(migrated) if migrated else "none",
            ", ".join(failed) if failed else "none",
            destination,
        )
    return MigrationResult(outcomes=tuple(outcomes))


def _migrate_one_legacy_database(
    datadir: str,
    filename: str,
    label: str,
    log: logging.Logger,
) -> LegacyDatabaseOutcome:
    """Migrate one legacy file into global.db or report why it remains."""
    source = Path(legacy_db_path(datadir, filename))
    destination = Path(global_db_path(datadir))
    if not source.exists():
        log.debug(
            "Legacy %s database %s not found — nothing to migrate.",
            label,
            source,
        )
        return LegacyDatabaseOutcome(filename, label, "absent")

    if _legacy_already_imported(destination, filename):
        log.debug(
            "Legacy %s database %s was already imported into %s — leaving both.",
            label,
            source,
            destination,
        )
        return LegacyDatabaseOutcome(filename, label, "migrated")

    log.info(
        "Found legacy %s database %s — migrating tables into %s",
        label,
        source,
        destination,
    )
    try:
        _copy_legacy_database(source, destination, filename, log)
    except (OSError, sqlite3.Error) as error:
        log.error(
            "Failed to migrate legacy %s database %s into %s: %s. "
            "Leaving the legacy file in place for the next startup.",
            label,
            source,
            destination,
            error,
        )
        return LegacyDatabaseOutcome(filename, label, "failed", str(error))

    log.info(
        "Migration of legacy %s database %s completed; leaving the legacy file "
        "in place for older installs and rollbacks.",
        label,
        source,
    )
    return LegacyDatabaseOutcome(filename, label, "migrated")


def _legacy_already_imported(destination: Path, filename: str) -> bool:
    """Return True when global.db already recorded importing this legacy file."""
    if not destination.is_file() or destination.stat().st_size == 0:
        return False
    try:
        with contextlib.closing(sqlite3.connect(str(destination))) as con:
            present = con.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                (LEGACY_IMPORT_TABLE,),
            ).fetchone()
            if not present:
                return False
            return (
                con.execute(
                    f"SELECT 1 FROM main.{_quote_ident(LEGACY_IMPORT_TABLE)} "
                    "WHERE filename = ?",
                    (filename,),
                ).fetchone()
                is not None
            )
    except (OSError, sqlite3.Error):
        return False


def _ensure_legacy_import_table(con: sqlite3.Connection) -> None:
    """Create the bookkeeping table that records completed legacy imports."""
    con.execute(
        f"CREATE TABLE IF NOT EXISTS main.{_quote_ident(LEGACY_IMPORT_TABLE)} ("
        "filename TEXT PRIMARY KEY NOT NULL)"
    )


def _copy_legacy_database(
    source: Path,
    destination: Path,
    filename: str,
    log: logging.Logger,
) -> None:
    """Copy source tables into destination and record the import atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(destination))) as dest:
        dest.execute("BEGIN IMMEDIATE")
        try:
            _ensure_legacy_import_table(dest)
            already = dest.execute(
                f"SELECT 1 FROM main.{_quote_ident(LEGACY_IMPORT_TABLE)} "
                "WHERE filename = ?",
                (filename,),
            ).fetchone()
            if already:
                dest.commit()
                return
            dest.execute(f"ATTACH DATABASE {_sql_literal(str(source))} AS legacy")
            tables = dest.execute(
                "SELECT name, sql FROM legacy.sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            if not tables:
                log.info(
                    "Legacy database %s has no user tables; recording the import "
                    "in %s without copying tables.",
                    source,
                    destination,
                )
            for name, create_sql in tables:
                if not create_sql:
                    raise sqlite3.DatabaseError(
                        f"legacy table {name!r} has no CREATE statement"
                    )
                existing = dest.execute(
                    "SELECT 1 FROM main.sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (name,),
                ).fetchone()
                if existing:
                    log.warning(
                        "Replacing existing table %r in %s from legacy database %s",
                        name,
                        destination,
                        source,
                    )
                quoted = _quote_ident(name)
                dest.execute(f"DROP TABLE IF EXISTS main.{quoted}")
                dest.execute(create_sql)
                dest.execute(f"INSERT INTO main.{quoted} SELECT * FROM legacy.{quoted}")
                count = dest.execute(f"SELECT COUNT(*) FROM main.{quoted}").fetchone()[
                    0
                ]
                log.info("  copied table %r (%s rows)", name, count)
                for (index_sql,) in dest.execute(
                    "SELECT sql FROM legacy.sqlite_master "
                    "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL "
                    "ORDER BY name",
                    (name,),
                ):
                    dest.execute(index_sql)
                    log.info("  copied index for table %r", name)
            dest.execute(
                f"INSERT INTO main.{_quote_ident(LEGACY_IMPORT_TABLE)} (filename) "
                "VALUES (?)",
                (filename,),
            )
            dest.commit()
        except BaseException:
            dest.rollback()
            raise
        finally:
            with contextlib.suppress(sqlite3.Error):
                dest.execute("DETACH DATABASE legacy")


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    """Quote a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"
