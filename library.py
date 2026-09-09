"""Handle the JLCPCB parts database."""

from collections.abc import Iterable, Iterator, Sequence
import contextlib
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path, PurePath
import re
import sqlite3
from threading import Lock, Thread
import time
from typing import Any, NamedTuple, Optional, Union

import requests  # pylint: disable=import-error
import wx  # pylint: disable=import-error

from .correction_data import (
    KIND_FOOTPRINT,
    KIND_LCSC,
    AnyCorrection,
    Correction,
    CorrectionDataError,
    CorrectionIssue,
    LcscCorrection,
    correction_kind,
    parse_corrections_csv,
    validate_correction,
    validate_lcsc_correction,
)
from .dblib import DEFAULT_LIBRARY, LIBRARY_CONFIGS
from .events import (
    DownloadCompletedEvent,
    DownloadFinishedEvent,
    DownloadProgressEvent,
    DownloadStartedEvent,
    MessageEvent,
)
from .helpers import PLUGIN_PATH, dict_factory, natural_sort_collation
from .lcsc import normalize_lcsc
from .partselector_columns import DB_FIELDS, SORTABLE_COLUMN_INDEX_TO_DB
from .search_escape import escape_fts_phrase, escape_like_term
from .unzip_parts import unzip_parts

DatabasePath = Union[str, os.PathLike[str]]


@dataclass(frozen=True)
class _RuleTable:
    """The statements that address one kind of correction by physical row."""

    name: str
    key: str
    columns: str
    probe: str
    select_all: str
    select_row: str
    insert: str
    update: str
    delete: str


# Pattern rules and part-number rules live in sibling tables of one database,
# so both are validated, repaired and copied between scopes the same way.
# rowids are only unique within a table, which is why every stored row also
# carries its kind.
_RULE_TABLES = {
    KIND_FOOTPRINT: _RuleTable(
        "correction",
        "regex",
        "PRAGMA table_xinfo(correction)",
        "SELECT rowid FROM correction LIMIT 0",
        "SELECT rowid, regex, rotation, offset_x, offset_y FROM correction ORDER BY regex ASC, rowid ASC",
        "SELECT rowid, regex, rotation, offset_x, offset_y FROM correction WHERE rowid=?",
        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES (?, ?, ?, ?)",
        "UPDATE correction SET regex=?, rotation=?, offset_x=?, offset_y=? WHERE rowid=?",
        "DELETE FROM correction WHERE rowid=?",
    ),
    KIND_LCSC: _RuleTable(
        "lcsc_correction",
        "lcsc",
        "PRAGMA table_xinfo(lcsc_correction)",
        "SELECT rowid FROM lcsc_correction LIMIT 0",
        "SELECT rowid, lcsc, rotation, offset_x, offset_y FROM lcsc_correction ORDER BY lcsc ASC, rowid ASC",
        "SELECT rowid, lcsc, rotation, offset_x, offset_y FROM lcsc_correction WHERE rowid=?",
        "INSERT INTO lcsc_correction (lcsc, rotation, offset_x, offset_y) VALUES (?, ?, ?, ?)",
        "UPDATE lcsc_correction SET lcsc=?, rotation=?, offset_x=?, offset_y=? WHERE rowid=?",
        "DELETE FROM lcsc_correction WHERE rowid=?",
    ),
}
_INITIAL_DEFAULTS_KEY = "remote:initial-defaults:v1"
_INITIAL_DOWNLOAD_LOCK = Lock()
_INITIAL_DOWNLOAD_TARGETS: set[str] = set()


def _normalize_part_preference_lcsc(value: object) -> Optional[str]:  # noqa: UP045
    """Accept a complete C-number without requiring current catalog membership."""
    if isinstance(value, str) and re.fullmatch(
        r"C[0-9]+", value.strip(), re.IGNORECASE
    ):
        return value.strip().upper()
    return None


def _sqlite_file_uri(path: PurePath) -> str:
    """Keep UNC hosts in SQLite's path while retaining pathlib's escaping."""
    uri = path.as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        return "file:////" + uri[len("file://") :]
    return uri


class PartsDatabaseInfo(NamedTuple):
    """Information about the parts database."""

    last_update: str
    size: int
    part_count: int


class LibraryState(Enum):
    """The various states of the library."""

    INITIALIZED = 0
    UPDATE_NEEDED = 1
    DOWNLOAD_RUNNING = 2


@dataclass(frozen=True)
class StoredCorrection:
    """Preserve a stored row and its validation errors for precise repair.

    ``pattern`` holds the stored key text of either kind: a regular expression
    for a footprint rule, the part number as stored for an LCSC rule.
    """

    rowid: int
    pattern: object
    rotation: object
    offset: tuple[object, object]
    issues: tuple[CorrectionIssue, ...]
    correction: Optional[AnyCorrection] = None  # noqa: UP045
    kind: str = KIND_FOOTPRINT

    @property
    def identity(self) -> tuple[tuple[type, object], ...]:
        """Identify the exact raw record, distinguishing SQLite storage types."""
        return tuple(
            (type(value), value)
            for value in (self.rowid, self.pattern, self.rotation, *self.offset)
        )


class CorrectionState(Enum):
    """Whether a complete correction set is ready, repairable, or unavailable."""

    READY = "ready"
    NEEDS_REPAIR = "needs_repair"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CorrectionSnapshot:
    """An immutable read of one explicitly identified correction database."""

    db_path: str
    scope: str
    rows: tuple[StoredCorrection, ...]
    corrections: Optional[tuple[Correction, ...]]  # noqa: UP045
    issues: tuple[CorrectionIssue, ...]
    csv_migrations: tuple[tuple[str, str], ...] = ()
    state: CorrectionState = CorrectionState.READY
    warnings: tuple[CorrectionIssue, ...] = ()


@dataclass(frozen=True)
class CorrectionBatchResult:
    """Describe a committed operation, including input records intentionally skipped."""

    inserted: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def changed(self) -> int:
        """Report how many stored records were inserted or updated."""
        return self.inserted + self.updated


class Library:
    """A storage class to get data from a sqlite database and write it back."""

    def __init__(self, parent: Any) -> None:
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.order_by = "LCSC Part"
        self.order_dir = "ASC"
        self.datadir = ""
        self.selected_library = DEFAULT_LIBRARY
        self.partsdb_file = ""
        self.rotationsdb_file = ""
        self.localcorrectionsdb_file = ""
        self.globalcorrectionsdb_file = ""
        self.correctionsdb_file = ""
        self.part_preferences_db_file = ""
        self.state = None
        self.download_lock = Lock()
        self._download_running = False
        self._download_attempt = 0
        self.category_map = {}
        self._migration_session_diagnostics = {}
        self._known_legacy_sources = {}

        self.refresh_library_config()

        self.logger.debug("partsdb_file %s", self.partsdb_file)
        self.logger.debug("sqlite.sqlite_version %s", sqlite3.sqlite_version)

    def _resolve_data_directory(self):
        """Resolve the directory where global database files are stored."""
        configured = self.parent.settings.get("library", {}).get("data_path", "")
        if isinstance(configured, str) and configured.strip():
            return os.path.abspath(os.path.expanduser(configured.strip()))
        return os.path.join(PLUGIN_PATH, "jlcpcb")

    def refresh_library_config(self) -> bool:
        """Apply settings only when no worker still uses the current catalog paths."""
        with self.download_lock:
            if self._download_running:
                return False
            try:
                self._apply_library_config()
            except (sqlite3.Error, OSError, ValueError):
                self.state = LibraryState.UPDATE_NEEDED
                raise
            return True

    def _apply_library_config(self) -> None:
        """Refresh catalog paths while the download lock protects their identity."""
        self.state = LibraryState.UPDATE_NEEDED
        self.datadir = self._resolve_data_directory()

        # Get selected library from settings, default to all-parts
        selected_library = self.parent.settings.get("library", {}).get(
            "selected_library", DEFAULT_LIBRARY
        )
        if selected_library not in LIBRARY_CONFIGS:
            selected_library = DEFAULT_LIBRARY

        self.selected_library = selected_library
        library_config = LIBRARY_CONFIGS[selected_library]
        self.partsdb_file = os.path.join(self.datadir, library_config.name)
        self.rotationsdb_file = os.path.join(self.datadir, "rotations.db")
        self.localcorrectionsdb_file = os.path.join(
            self.parent.project_path, "jlcpcb", "project.db"
        )
        self.globalcorrectionsdb_file = os.path.join(self.datadir, "corrections.db")
        self.correctionsdb_file = (
            self.globalcorrectionsdb_file
            if self.uses_global_correction_database()
            else self.localcorrectionsdb_file
        )
        # Retain the legacy filename so existing shared part preferences stay available.
        self.part_preferences_db_file = os.path.join(self.datadir, "mappings.db")
        self.category_map = {}

        self.setup()
        self.check_library()

        self.logger.debug(
            "Library configuration refreshed. Selected: %s, Data directory: %s, Database: %s",
            self.selected_library,
            self.datadir,
            self.partsdb_file,
        )

    def setup(self):
        """Check if folders and database exist, setup if not."""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory '%s' does not exist and will be created.", self.datadir
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        else:
            self.logger.info("Data directory '%s' exists, not creating", self.datadir)

    def check_library(self) -> None:
        """Check if the database files exists, if not trigger update / create database."""
        if (
            not os.path.isfile(self.partsdb_file)
            or os.path.getsize(self.partsdb_file) == 0
        ):
            self.state = LibraryState.UPDATE_NEEDED
        else:
            self.state = LibraryState.INITIALIZED
        try:
            if (
                not os.path.isfile(self.correctionsdb_file)
                or os.path.getsize(self.correctionsdb_file) == 0
            ):
                with self._correction_transaction(
                    self.correctionsdb_file, create=True
                ) as con:
                    if (
                        self.correctionsdb_file == self.globalcorrectionsdb_file
                        and _INITIAL_DEFAULTS_KEY
                        not in self._correction_metadata(con)[0]
                    ):
                        con.execute(
                            "INSERT OR IGNORE INTO correction_migration_state VALUES (?, ?, ?, ?)",
                            (
                                _INITIAL_DEFAULTS_KEY,
                                self.globalcorrectionsdb_file,
                                "seed-pending",
                                json.dumps(self._legacy_correction_sources()),
                            ),
                        )
            self.retry_correction_migrations()
        except (CorrectionDataError, OSError) as error:
            # Recovery reads report these errors while keeping the manager usable.
            self.logger.warning("Correction storage is unavailable: %s", error)
        try:
            new_preferences = (
                not os.path.isfile(self.part_preferences_db_file)
                or os.path.getsize(self.part_preferences_db_file) == 0
            )
            self.create_part_preferences_table()
            if new_preferences:
                self.migrate_legacy_part_preferences()
        except (sqlite3.Error, OSError) as error:
            # Preferences are optional; keep the catalog and Settings accessible.
            self.logger.warning("Part preference storage is unavailable: %s", error)

    def uses_global_correction_database(self):
        """Check for a project correction table without creating a project database."""
        if not Path(self.localcorrectionsdb_file).exists():
            return True
        try:
            with contextlib.closing(
                self._read_database(self.localcorrectionsdb_file)
            ) as con:
                return not con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='correction'"
                ).fetchone()
        except (sqlite3.Error, OSError):
            # Expose an unreadable project database for repair rather than
            # silently changing which correction set a board uses.
            return False

    def _validate_local_destination(self) -> None:
        """Validate an existing correction table while allowing unrelated project data."""
        target = self.localcorrectionsdb_file
        if not Path(target).exists():
            return
        try:
            with contextlib.closing(self._read_database(target)) as con:
                exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='correction'"
                ).fetchone()
            if exists:
                snapshot = self.read_correction_data(target)
                if snapshot.corrections is None:
                    raise CorrectionDataError(snapshot.issues)
        except (sqlite3.Error, OSError) as error:
            raise self._storage_error(target, error) from error

    def switch_to_global_correction_database(self, use_global: bool) -> None:
        """Switch only after validation and a complete successful storage transaction."""
        currently_using_global = (
            self.correctionsdb_file == self.globalcorrectionsdb_file
        )
        if currently_using_global == use_global:
            return
        if use_global:
            if (
                not Path(self.globalcorrectionsdb_file).exists()
                or Path(self.globalcorrectionsdb_file).stat().st_size == 0
            ):
                self.create_correction_table(self.globalcorrectionsdb_file)
            migration_issues = self.migrate_corrections()
            if migration_issues:
                raise CorrectionDataError(migration_issues)
            destination = self.read_correction_data(self.globalcorrectionsdb_file)
            if destination.corrections is None:
                raise CorrectionDataError(destination.issues)
            with self._correction_transaction(self.localcorrectionsdb_file) as con:
                con.execute("DROP TABLE correction")
                # uses_global_correction_database() decides by looking for
                # 'correction', so a surviving part-number table would strand
                # local overrides in a database that nothing reads.
                con.execute("DROP TABLE IF EXISTS lcsc_correction")
                # Keep automatic CSV provenance when leaving local scope, so
                # an unarchived source cannot replay into the global database.
            self.correctionsdb_file = self.globalcorrectionsdb_file
            self._start_initial_remote_corrections(self.globalcorrectionsdb_file)
        else:
            source_snapshot = self.read_correction_data()
            source = source_snapshot.corrections
            if source is None:
                raise CorrectionDataError(source_snapshot.issues)
            self._validate_local_destination()
            with self._correction_transaction(
                self.localcorrectionsdb_file, create=True
            ) as con:
                con.execute("DELETE FROM correction")
                con.execute("DELETE FROM lcsc_correction")
                for kind, table in _RULE_TABLES.items():
                    con.executemany(
                        table.insert,
                        [
                            correction.db_row()
                            for correction in source
                            if correction_kind(correction) == kind
                        ],
                    )
                con.executemany(
                    "INSERT OR IGNORE INTO correction_migrations VALUES (?, ?)",
                    source_snapshot.csv_migrations,
                )
            self.correctionsdb_file = self.localcorrectionsdb_file

    def set_order_by(self, n):
        """Set which value we want to order by when getting data from the database."""
        column = SORTABLE_COLUMN_INDEX_TO_DB.get(n)
        if column is None:
            return
        if self.order_by == column and self.order_dir == "ASC":
            self.order_dir = "DESC"
        else:
            self.order_by = column
            self.order_dir = "ASC"

    def search(self, parameters):
        """Search the database for parts that meet the given parameters."""

        # skip searching if there are no keywords and the part number
        # field is empty as there are too many parts for the search
        # to reasonbly show the desired part
        if parameters["keyword"] == "" and (
            "part_no" not in parameters or parameters["part_no"] == ""
        ):
            return []

        # Note: must match the shared part selector column definitions.
        s = ",".join(f'"{c}"' for c in DB_FIELDS)
        query = f"SELECT {s} FROM parts WHERE "

        match_chunks = []
        like_chunks = []

        query_chunks = []

        # Build 'match_chunks' and 'like_chunks' arrays
        #
        # FTS5 (https://www.sqlite.org/fts5.html) has a substring limit of
        # at least 3 characters.
        # 'Substrings consisting of fewer than 3 unicode characters do not
        #  match any rows when used with a full-text query'
        #
        # However, they will still match with a LIKE.
        #
        # So extract out the <3 character strings and add a 'LIKE' term
        # for each of those.
        if parameters["keyword"] != "":
            keywords = parameters["keyword"].split(" ")
            match_keywords_intermediate = []
            for w in keywords:
                # skip over empty keywords
                if w != "":
                    if len(w) < 3:  # LIKE entry
                        escaped = escape_like_term(w)
                        kw = f"description LIKE '%{escaped}%' ESCAPE '\\'"
                        like_chunks.append(kw)
                    else:  # MATCH entry
                        escaped = escape_fts_phrase(w)
                        kw = f'"{escaped}"'
                        match_keywords_intermediate.append(kw)
            if match_keywords_intermediate:
                match_entry = " AND ".join(match_keywords_intermediate)
                match_chunks.append(f"{match_entry}")

        if "manufacturer" in parameters and parameters["manufacturer"] != "":
            p = escape_fts_phrase(parameters["manufacturer"])
            match_chunks.append(f'"Manufacturer":"{p}"')
        if "package" in parameters and parameters["package"] != "":
            p = escape_fts_phrase(parameters["package"])
            match_chunks.append(f'"Package":"{p}"')
        if (
            "category" in parameters
            and parameters["category"] != ""
            and parameters["category"] != "All"
        ):
            p = escape_fts_phrase(parameters["category"])
            match_chunks.append(f'"First Category":"{p}"')
        if "subcategory" in parameters and parameters["subcategory"] != "":
            p = escape_fts_phrase(parameters["subcategory"])
            match_chunks.append(f'"Second Category":"{p}"')
        if "part_no" in parameters and parameters["part_no"] != "":
            p = escape_fts_phrase(parameters["part_no"])
            match_chunks.append(f'"MFR.Part":"{p}"')
        if "solder_joints" in parameters and parameters["solder_joints"] != "":
            p = escape_fts_phrase(parameters["solder_joints"])
            match_chunks.append(f'"Solder Joint":"{p}"')

        library_types = []
        if parameters["basic"]:
            library_types.append('"Basic"')
        if parameters["extended"]:
            library_types.append('"Extended"')
        if parameters["preferred"]:
            library_types.append('"Preferred"')
        if library_types:
            query_chunks.append(f'"Library Type" IN ({",".join(library_types)})')

        if parameters["stock"]:
            query_chunks.append('"Stock" > "0"')

        if not match_chunks and not like_chunks and not query_chunks:
            return []

        if match_chunks:
            query += "parts MATCH '"
            query += " AND ".join(match_chunks)
            query += "'"

        if like_chunks:
            if match_chunks:
                query += " AND "
            query += " AND ".join(like_chunks)

        if query_chunks:
            if match_chunks or like_chunks:
                query += " AND "
            query += " AND ".join(query_chunks)

        query += f' ORDER BY "{self.order_by}" COLLATE naturalsort {self.order_dir}'
        query += " LIMIT 1000"

        self.logger.debug("query '%s'", query)

        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con:
            con.create_collation("naturalsort", natural_sort_collation)
            with con as cur:
                return cur.execute(query).fetchall()

    def delete_parts_table(self):
        """Delete the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute("DROP TABLE IF EXISTS parts")
            cur.commit()

    def create_meta_table(self):
        """Create the meta table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta ('filename', 'size', 'partcount', 'date', 'last_update')"
            )
            cur.commit()

    @staticmethod
    def _correction_schema(con: sqlite3.Connection) -> None:
        """Create additive correction tables inside the caller's transaction."""
        con.execute(
            "CREATE TABLE IF NOT EXISTS correction ('regex', 'rotation', 'offset_x', 'offset_y')"
        )
        Library._lcsc_correction_schema(con)
        con.execute(
            "CREATE TABLE IF NOT EXISTS correction_migrations "
            "(migration_key TEXT PRIMARY KEY, source TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS correction_migration_state "
            "(migration_key TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "status TEXT NOT NULL, message TEXT NOT NULL)"
        )

    @staticmethod
    def _sqlite_affinity(declaration: str) -> str:
        """Determine column affinity using SQLite's ordered declaration rules."""
        declared_type = declaration.upper()
        if "INT" in declared_type:
            return "INTEGER"
        if any(name in declared_type for name in ("CHAR", "CLOB", "TEXT")):
            return "TEXT"
        if not declared_type or "BLOB" in declared_type:
            return "BLOB"
        if any(name in declared_type for name in ("REAL", "FLOA", "DOUB")):
            return "REAL"
        return "NUMERIC"

    @staticmethod
    def _lcsc_correction_schema(con: sqlite3.Connection) -> None:
        """Create the part-number table; databases from earlier releases lack it."""
        con.execute(
            "CREATE TABLE IF NOT EXISTS lcsc_correction "
            "('lcsc', 'rotation', 'offset_x', 'offset_y')"
        )

    @staticmethod
    def _has_table(con: sqlite3.Connection, name: str) -> bool:
        """Report whether an ordinary table of that name exists."""
        return (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    @classmethod
    def _validate_correction_schema(cls, con: sqlite3.Connection) -> None:
        """Require named correction fields and unambiguous physical row identities."""
        cls._validate_rule_table(con, _RULE_TABLES[KIND_FOOTPRINT], required=True)
        # The part-number table is additive: a database from an earlier release
        # is valid without it and gains it on its next write transaction.
        cls._validate_rule_table(con, _RULE_TABLES[KIND_LCSC], required=False)

    @classmethod
    def _validate_rule_table(
        cls, con: sqlite3.Connection, table: _RuleTable, *, required: bool
    ) -> None:
        """Check one rule table's columns, affinities and row identities."""
        found = con.execute(
            "SELECT type FROM sqlite_master WHERE name=?", (table.name,)
        ).fetchone()
        if found is None and not required:
            return
        if found is None or found[0] != "table":
            raise sqlite3.DatabaseError(
                f"{table.name} storage must be an ordinary table"
            )
        columns = con.execute(table.columns).fetchall()
        expected = {table.key, "rotation", "offset_x", "offset_y"}
        if (
            len(columns) != len(expected)
            or {column[1].casefold() for column in columns} != expected
            or any(column[6] for column in columns)
        ):
            raise sqlite3.DatabaseError(
                f"{table.name} table must contain exactly {table.key}, rotation, "
                "offset_x, and offset_y; unsupported schemas cannot be repaired "
                "automatically"
            )
        affinities = {
            column[1].casefold(): cls._sqlite_affinity(column[2]) for column in columns
        }
        if affinities[table.key] not in {"TEXT", "BLOB"}:
            raise sqlite3.DatabaseError(
                f"unsupported correction schema: {table.key} must have TEXT or BLOB "
                "affinity to preserve the stored key text; use a supported "
                "correction database schema"
            )
        if affinities["rotation"] == "REAL":
            raise sqlite3.DatabaseError(
                "unsupported correction schema: rotation must not have REAL affinity "
                "because it can round whole degrees; use a supported correction database schema"
            )
        if any(affinities[name] == "TEXT" for name in ("offset_x", "offset_y")):
            raise sqlite3.DatabaseError(
                "unsupported correction schema: offsets must not have TEXT affinity "
                "because it can round their numeric values; use a supported correction database schema"
            )
        # The exact-column check rules out aliases that shadow SQLite's rowid.
        # This query also rejects WITHOUT ROWID tables before exposing repair IDs.
        try:
            con.execute(table.probe)
        except sqlite3.Error as error:
            raise sqlite3.DatabaseError(
                f"{table.name} table requires SQLite row identities for safe repair"
            ) from error

    @staticmethod
    def _read_database(target: DatabasePath) -> sqlite3.Connection:
        """Open an existing database without creating missing files."""
        return sqlite3.connect(
            _sqlite_file_uri(Path(target).resolve()) + "?mode=ro", uri=True
        )

    @staticmethod
    def _storage_error(
        target: DatabasePath, error: Exception, field: str = "database"
    ) -> CorrectionDataError:
        """Wrap storage failures in actionable correction diagnostics."""
        return CorrectionDataError(
            (CorrectionIssue(field, None, str(error), source=str(target)),)
        )

    @contextlib.contextmanager
    def _correction_transaction(
        self, target: DatabasePath, *, create: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Capture one target and roll back the entire operation on any failure."""
        try:
            if create:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            elif not Path(target).is_file():
                raise OSError("correction database does not exist")
            connection = (
                sqlite3.connect(target)
                if create
                else sqlite3.connect(
                    _sqlite_file_uri(Path(target).resolve()) + "?mode=rw", uri=True
                )
            )
            with contextlib.closing(connection) as con, con:
                con.execute("BEGIN IMMEDIATE")
                if create:
                    self._correction_schema(con)
                self._validate_correction_schema(con)
                # Every write transaction brings an older database up to date.
                self._lcsc_correction_schema(con)
                yield con
        except (sqlite3.Error, OSError) as error:
            raise self._storage_error(target, error) from error

    def create_correction_table(
        self,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> None:
        """Initialize correction storage without rewriting existing user records."""
        target = db_path if db_path is not None else self.correctionsdb_file
        with self._correction_transaction(target, create=True):
            pass

    @staticmethod
    def correction_csv_migration_key(
        path: DatabasePath, contents: Union[bytes, str]
    ) -> str:
        """Identify an automatic CSV import by canonical path and exact contents."""
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        digest = hashlib.sha256(contents).hexdigest()
        return f"csv:{Path(path).resolve()}:{digest}"

    @staticmethod
    def _correction_metadata(
        con: sqlite3.Connection,
    ) -> tuple[dict[object, str], list[tuple[object, str, str, str]]]:
        """Read optional metadata once, including databases from earlier releases."""
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        completed = (
            dict(con.execute("SELECT migration_key, source FROM correction_migrations"))
            if "correction_migrations" in tables
            else {}
        )
        states = (
            con.execute(
                "SELECT migration_key, source, status, message FROM correction_migration_state ORDER BY rowid"
            ).fetchall()
            if "correction_migration_state" in tables
            else []
        )
        return completed, states

    @staticmethod
    def _complete_correction_migration(
        con: sqlite3.Connection, key: str, source: str
    ) -> None:
        """Commit completion and remove superseded pending state in the caller's transaction."""
        con.execute("INSERT INTO correction_migrations VALUES (?, ?)", (key, source))
        con.execute(
            "DELETE FROM correction_migration_state WHERE migration_key=?", (key,)
        )

    def has_correction_migration(
        self,
        key: str,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> bool:
        """Check completion, including the current project's archived CSV provenance."""
        target = db_path if db_path is not None else self.correctionsdb_file
        candidates = [target]
        if key.startswith("csv:"):
            candidates.extend(
                path
                for path in (
                    self.localcorrectionsdb_file,
                    self.globalcorrectionsdb_file,
                )
                if Path(path).resolve() != Path(target).resolve()
                and Path(path).exists()
            )
        try:
            for candidate in candidates:
                with contextlib.closing(self._read_database(candidate)) as con:
                    if key in self._correction_metadata(con)[0]:
                        return True
            return False
        except (sqlite3.Error, OSError) as error:
            raise self._storage_error(candidate, error, "migration") from error

    def _validated_corrections(
        self, records: Iterable[object], target: DatabasePath
    ) -> list[Correction]:
        """Parse raw records once; retain already validated immutable corrections."""
        validated = []
        issues = []
        for record in records:
            try:
                if isinstance(record, Correction):
                    validated.append(record)
                    continue
                if not isinstance(record, (tuple, list)) or len(record) != 3:
                    raise CorrectionDataError(
                        (
                            CorrectionIssue(
                                "record",
                                record,
                                "expected pattern, rotation, and offsets",
                                str(target),
                            ),
                        )
                    )
                validated.append(validate_correction(*record, source=str(target)))
            except CorrectionDataError as error:
                issues.extend(error.issues)
        if issues:
            raise CorrectionDataError(issues)
        return validated

    def apply_corrections(
        self,
        records: Iterable[object],
        *,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
        overwrite: bool = True,
        migration_key: Optional[str] = None,  # noqa: UP045
    ) -> CorrectionBatchResult:
        """Validate a complete batch, then commit its writes and marker atomically."""
        target = db_path if db_path is not None else self.correctionsdb_file
        validated = self._validated_corrections(records, target)
        if migration_key and self.has_correction_migration(migration_key, target):
            return CorrectionBatchResult(skipped=len(validated))
        selected = {}
        for correction in validated:
            if overwrite or correction.pattern not in selected:
                selected[correction.pattern] = correction
        inserted = updated = 0
        skipped = len(validated) - len(selected)
        with self._correction_transaction(target) as con:
            if (
                migration_key
                and migration_key != _INITIAL_DEFAULTS_KEY
                and migration_key in self._correction_metadata(con)[0]
            ):
                return CorrectionBatchResult(skipped=len(validated))
            if (
                migration_key == _INITIAL_DEFAULTS_KEY
                and not self._initial_seed_is_eligible(con, str(target))
            ):
                # A concurrent retry discovered a higher-priority source while
                # the HTTP request was in flight. Leave initial seeding pending.
                return CorrectionBatchResult(skipped=len(validated))
            for correction in selected.values():
                exists = con.execute(
                    "SELECT 1 FROM correction WHERE regex = ?", (correction.pattern,)
                ).fetchone()
                if exists and not overwrite:
                    skipped += 1
                elif exists:
                    result = con.execute(
                        "UPDATE correction SET rotation=?, offset_x=?, offset_y=? WHERE regex=?",
                        (*correction.db_row()[1:], correction.pattern),
                    )
                    updated += result.rowcount
                else:
                    con.execute(
                        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES (?, ?, ?, ?)",
                        correction.db_row(),
                    )
                    inserted += 1
            if migration_key:
                self._correction_schema(con)
                self._complete_correction_migration(con, migration_key, migration_key)
        return CorrectionBatchResult(inserted, updated, skipped)

    def get_correction_data(
        self,
        regex: str,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> Optional[tuple[object, object, object, object]]:  # noqa: UP045
        """Get original stored values for a pattern using a parameterized lookup."""
        target = db_path if db_path is not None else self.correctionsdb_file
        try:
            with contextlib.closing(self._read_database(target)) as con:
                self._validate_correction_schema(con)
                return con.execute(
                    "SELECT regex, rotation, offset_x, offset_y FROM correction WHERE regex = ?",
                    (regex,),
                ).fetchone()
        except (sqlite3.Error, OSError) as error:
            raise self._storage_error(target, error) from error

    def delete_correction_data(self, regex: str) -> None:
        """Delete a pattern without interpreting its contents as SQL."""
        target = self.correctionsdb_file
        with self._correction_transaction(target) as con:
            con.execute("DELETE FROM correction WHERE regex = ?", (regex,))

    def _check_expected_corrections(
        self,
        actual: Sequence[tuple[object, ...]],
        expected: Optional[Sequence[StoredCorrection]],  # noqa: UP045
        target: DatabasePath,
    ) -> None:
        """Check existence or exact raw identities under the write lock."""
        if expected is None:
            if actual:
                return
        elif [tuple((type(value), value) for value in row) for row in actual] == [
            row.identity for row in sorted(expected, key=lambda row: row.rowid)
        ]:
            return
        raise self._storage_error(
            target,
            ValueError("stored correction changed; refresh and select it again"),
            "row",
        )

    def delete_correction_row(
        self,
        rowid: int,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
        *,
        expected_record: Optional[StoredCorrection] = None,  # noqa: UP045
        kind: str = KIND_FOOTPRINT,
    ) -> None:
        """Delete the selected original row, rejecting a concurrent edit or reused ID.

        The record's own kind names the table; ``kind`` applies without one.
        """
        target = db_path if db_path is not None else self.correctionsdb_file
        table = _RULE_TABLES[expected_record.kind if expected_record else kind]
        with self._correction_transaction(target) as con:
            if expected_record is not None:
                self._check_expected_corrections(
                    con.execute(table.select_row, (rowid,)).fetchall(),
                    (expected_record,),
                    target,
                )
            con.execute(table.delete, (rowid,))

    def update_correction_data(
        self, regex: object, rotation: object, offset: object
    ) -> None:
        """Validate and update an existing pattern in one transaction."""
        target = self.correctionsdb_file
        correction = validate_correction(regex, rotation, offset, source=str(target))
        with self._correction_transaction(target) as con:
            con.execute(
                "UPDATE correction SET rotation=?, offset_x=?, offset_y=? WHERE regex=?",
                (*correction.db_row()[1:], correction.pattern),
            )

    def insert_correction_data(
        self,
        regex: object,
        rotation: object,
        offset: object,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> int:
        """Validate and insert a correction, refusing accidental duplicate patterns."""
        return self.save_correction_data(regex, rotation, offset, db_path=db_path)

    def insert_lcsc_correction_data(
        self,
        lcsc: object,
        rotation: object,
        offset: object,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> int:
        """Validate and insert a part-number correction, refusing a duplicate key."""
        return self.save_correction_data(
            lcsc, rotation, offset, db_path=db_path, kind=KIND_LCSC
        )

    def save_correction_data(
        self,
        pattern: object,
        rotation: object = None,
        offset: object = None,
        *,
        rowid: Optional[int] = None,  # noqa: UP045
        replace: bool = False,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
        expected_record: Optional[StoredCorrection] = None,  # noqa: UP045
        expected_conflicts: Optional[Sequence[StoredCorrection]] = None,  # noqa: UP045
        kind: str = KIND_FOOTPRINT,
    ) -> int:
        """Save or repair one row, with optional collision replacement in the same transaction.

        A validated Correction or LcscCorrection names its own table; raw
        values are validated as the given kind first.
        """
        target = db_path if db_path is not None else self.correctionsdb_file
        if isinstance(pattern, (Correction, LcscCorrection)):
            correction = pattern
        elif kind == KIND_LCSC:
            correction = validate_lcsc_correction(
                pattern, rotation, offset, source=str(target), rowid=rowid
            )
        else:
            correction = validate_correction(
                pattern, rotation, offset, source=str(target), rowid=rowid
            )
        table = _RULE_TABLES[correction_kind(correction)]
        if expected_record is not None and expected_record.kind != correction_kind(
            correction
        ):
            raise self._storage_error(
                target,
                ValueError("the selected row is a different kind of rule"),
                "row",
            )
        with self._correction_transaction(target) as con:
            if rowid is not None or expected_record is not None:
                self._check_expected_corrections(
                    con.execute(table.select_row, (rowid,)).fetchall(),
                    (expected_record,) if expected_record is not None else None,
                    target,
                )
            conflicts = self._conflicting_rows(con, correction, rowid)
            if expected_conflicts is not None:
                self._check_expected_corrections(conflicts, expected_conflicts, target)
            if conflicts and not replace:
                raise CorrectionDataError(
                    (self._duplicate_key_issue(correction, target, rowid),)
                )
            if conflicts:
                con.executemany(table.delete, [(row[0],) for row in conflicts])
            if rowid is None:
                return con.execute(table.insert, correction.db_row()).lastrowid
            con.execute(table.update, (*correction.db_row(), rowid))
        return rowid

    @staticmethod
    def _conflicting_rows(
        con: sqlite3.Connection,
        correction: AnyCorrection,
        rowid: Optional[int],  # noqa: UP045
    ) -> list[tuple[object, ...]]:
        """Find the other rows of the same kind stored under the same key."""
        if isinstance(correction, LcscCorrection):
            # Part numbers compare in canonical form, so a row that reached the
            # table in another spelling still counts as the same rule.
            return [
                row
                for row in con.execute(
                    "SELECT rowid, lcsc, rotation, offset_x, offset_y "
                    "FROM lcsc_correction ORDER BY rowid"
                ).fetchall()
                if row[0] != rowid
                and isinstance(row[1], str)
                and normalize_lcsc(row[1]) == correction.lcsc
            ]
        return con.execute(
            "SELECT rowid, regex, rotation, offset_x, offset_y FROM correction "
            "WHERE regex=? AND (? IS NULL OR rowid != ?) ORDER BY rowid",
            (correction.pattern, rowid, rowid),
        ).fetchall()

    @staticmethod
    def _duplicate_key_issue(
        correction: AnyCorrection,
        target: DatabasePath,
        rowid: Optional[int],  # noqa: UP045
    ) -> CorrectionIssue:
        """Describe a refused save whose key another stored row already uses."""
        if isinstance(correction, LcscCorrection):
            return CorrectionIssue(
                "lcsc",
                correction.lcsc,
                "another correction already uses this part number",
                str(target),
                rowid=rowid,
            )
        return CorrectionIssue(
            "pattern",
            correction.pattern,
            "another correction already uses this pattern",
            str(target),
            pattern=correction.pattern,
            rowid=rowid,
        )

    def read_correction_data(
        self,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> CorrectionSnapshot:
        """Read recoverable original rows and a validated snapshot without creating storage."""
        target = str(db_path if db_path is not None else self.correctionsdb_file)
        scope = (
            "global"
            if Path(target).resolve() == Path(self.globalcorrectionsdb_file).resolve()
            else "local"
        )
        rows = []
        issues = []
        corrections = {}
        csv_migrations = ()
        raw_rows = {kind: [] for kind in _RULE_TABLES}
        unavailable = False
        warnings = []
        try:
            with contextlib.closing(self._read_database(target)) as con:
                con.execute("BEGIN")
                self._validate_correction_schema(con)
                for kind, table in _RULE_TABLES.items():
                    if kind == KIND_FOOTPRINT or self._has_table(con, table.name):
                        raw_rows[kind] = con.execute(table.select_all).fetchall()
                completed, states = self._correction_metadata(con)
                csv_migrations = tuple(
                    (key, source)
                    for key, source in completed.items()
                    if isinstance(key, str) and key.startswith("csv:")
                )
                if scope == "global":
                    for _, source, status, message in states:
                        if status in {"pending", "deferred"}:
                            (issues if status == "pending" else warnings).append(
                                CorrectionIssue(
                                    "migration", None, message, source=source
                                )
                            )
                unavailable = bool(issues)
        except (sqlite3.Error, OSError) as error:
            issues.extend(self._storage_error(target, error).issues)
            unavailable = True
        session_issues, session_warnings = self._migration_session_diagnostics.get(
            str(Path(target).resolve()), ((), ())
        )
        issues.extend(session_issues)
        warnings.extend(session_warnings)
        unavailable = unavailable or bool(session_issues)
        for kind, kind_rows in raw_rows.items():
            stored, kind_issues, kind_corrections = self._stored_rows(
                kind, kind_rows, target
            )
            rows.extend(stored)
            issues.extend(kind_issues)
            corrections.update(kind_corrections)
        rows, conflicts = self._mark_conflicting_corrections(rows, target)
        issues.extend(conflicts)
        state = (
            CorrectionState.UNAVAILABLE
            if unavailable
            else CorrectionState.NEEDS_REPAIR
            if issues
            else CorrectionState.READY
        )
        return CorrectionSnapshot(
            target,
            scope,
            tuple(rows),
            None if issues else tuple(corrections.values()),
            tuple(issues),
            csv_migrations,
            state,
            tuple(warnings),
        )

    @staticmethod
    def _stored_rows(
        kind: str, raw_rows: Sequence[tuple[object, ...]], target: str
    ) -> tuple[
        list[StoredCorrection],
        list[CorrectionIssue],
        dict[tuple[str, str], AnyCorrection],
    ]:
        """Validate one table's rows, keeping every original for precise repair."""
        validate = (
            validate_lcsc_correction if kind == KIND_LCSC else validate_correction
        )
        rows = []
        issues = []
        corrections = {}
        for rowid, key, rotation, offset_x, offset_y in raw_rows:
            correction = None
            row_issues = ()
            try:
                correction = validate(
                    key, rotation, (offset_x, offset_y), source=target, rowid=rowid
                )
            except CorrectionDataError as error:
                row_issues = error.issues
                issues.extend(row_issues)
            rows.append(
                StoredCorrection(
                    rowid,
                    key,
                    rotation,
                    (offset_x, offset_y),
                    row_issues,
                    correction,
                    kind,
                )
            )
            if correction is not None:
                corrections.setdefault((kind, correction.key), correction)
        return rows, issues, corrections

    @staticmethod
    def _conflict_group(row: StoredCorrection) -> tuple[str, str]:
        """Key a stored row the way its kind compares keys."""
        key = str(row.pattern)
        return row.kind, normalize_lcsc(key) if row.kind == KIND_LCSC else key

    @staticmethod
    def _mark_conflicting_corrections(
        rows: Sequence[StoredCorrection], target: str
    ) -> tuple[list[StoredCorrection], list[CorrectionIssue]]:
        """Identify every member of a conflicting key group for explicit repair."""
        groups = {}
        for row in rows:
            if isinstance(row.pattern, str):
                groups.setdefault(Library._conflict_group(row), []).append(row)
        conflicting = {
            pattern
            for pattern, group in groups.items()
            if len(group) > 1
            and (
                any(row.correction is None for row in group)
                or len({row.correction for row in group}) > 1
            )
        }
        issues = []
        result = []
        for row in rows:
            if (
                isinstance(row.pattern, str)
                and Library._conflict_group(row) in conflicting
            ):
                if row.kind == KIND_LCSC:
                    issue = CorrectionIssue(
                        "lcsc",
                        row.pattern,
                        "conflicting stored corrections use this part number; repair or delete the duplicate rows",
                        target,
                        rowid=row.rowid,
                    )
                else:
                    issue = CorrectionIssue(
                        "pattern",
                        row.pattern,
                        "conflicting stored corrections use this pattern; repair or delete the duplicate rows",
                        target,
                        pattern=row.pattern,
                        rowid=row.rowid,
                    )
                issues.append(issue)
                row = replace(row, issues=(*row.issues, issue))
            result.append(row)
        return result, issues

    def get_all_correction_data(
        self,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
    ) -> Optional[tuple[Correction, ...]]:  # noqa: UP045
        """Return a complete typed set, or None when storage is not ready."""
        return self.read_correction_data(db_path).corrections

    def create_part_preferences_table(self) -> None:
        """Create part preference storage using the existing table name.

        Keep the legacy ``mapping`` schema compatible with installed databases.
        """
        with (
            contextlib.closing(sqlite3.connect(self.part_preferences_db_file)) as con,
            con as cur,
        ):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mapping ('footprint', 'value', 'LCSC')"
            )
            cur.commit()

    def get_part_preference(self, footprint: str, value: str) -> Optional[str]:  # noqa: UP045
        """Validate the first stored preference without rewriting installed rows."""
        with (
            contextlib.closing(sqlite3.connect(self.part_preferences_db_file)) as con,
            con as cur,
        ):
            row = cur.execute(
                "SELECT LCSC FROM mapping WHERE footprint = ? AND value = ? "
                "ORDER BY rowid LIMIT 1",
                (footprint, value),
            ).fetchone()
        if row is None:
            return None
        lcsc = _normalize_part_preference_lcsc(row[0])
        if lcsc is None:
            self.logger.warning(
                "Skipping invalid stored part preference for %r / %r: %r",
                footprint,
                value,
                row[0],
            )
        return lcsc

    def delete_part_preference(self, footprint: str, value: str) -> None:
        """Delete the part preference for a footprint and value."""
        with (
            contextlib.closing(sqlite3.connect(self.part_preferences_db_file)) as con,
            con as cur,
        ):
            cur.execute(
                "DELETE FROM mapping WHERE footprint = ? AND value = ?",
                (footprint, value),
            )
            cur.commit()

    def save_part_preferences(
        self, preferences: Iterable[tuple[str, str, object]]
    ) -> int:
        """Commit an action's preferences together and count distinct changed keys.

        The last valid C-number for each exact key wins. Invalid choices leave
        preferences intact; legacy duplicate rows are updated together. Any
        database failure rolls back every accepted choice in the action.
        """
        latest = {}
        for footprint, value, identifier in preferences:
            lcsc = _normalize_part_preference_lcsc(identifier)
            if lcsc is None:
                self.logger.warning(
                    "Skipping invalid part preference for %r / %r: %r",
                    footprint,
                    value,
                    identifier,
                )
            else:
                latest[footprint, value] = lcsc
        if not latest:
            return 0

        with (
            contextlib.closing(sqlite3.connect(self.part_preferences_db_file)) as con,
            con as cur,
        ):
            # Serialize the lookup and write without requiring a new unique index.
            cur.execute("BEGIN IMMEDIATE")
            changed = 0
            for (footprint, value), lcsc in latest.items():
                existing = cur.execute(
                    "SELECT LCSC FROM mapping WHERE footprint = ? AND value = ?",
                    (footprint, value),
                ).fetchall()
                if not existing:
                    cur.execute(
                        "INSERT INTO mapping VALUES (?, ?, ?)",
                        (footprint, value, lcsc),
                    )
                elif any(row[0] != lcsc for row in existing):
                    cur.execute(
                        "UPDATE mapping SET LCSC = ? WHERE footprint = ? AND value = ?",
                        (lcsc, footprint, value),
                    )
                else:
                    continue
                changed += 1
            return changed

    def get_all_part_preferences(self) -> list[list[object]]:
        """Get all shared part preferences ordered by footprint."""
        with (
            contextlib.closing(sqlite3.connect(self.part_preferences_db_file)) as con,
            con as cur,
        ):
            return [
                list(c)
                for c in cur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
            ]

    def create_parts_table(self, columns):
        """Create the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cols = ",".join([f" '{c}'" for c in columns])
            cur.execute(f"CREATE TABLE IF NOT EXISTS parts ({cols})")
            cur.commit()

    def get_part_details(self, number: str) -> dict:
        """Get the part details for a LCSC number using optimized FTS5 querying."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con:
            con.row_factory = dict_factory
            cur = con.cursor()
            query = """SELECT "LCSC Part" AS lcsc, "Stock" AS stock, "Library Type" AS type,
                "MFR.Part" as part_no, "Description" as description, "Package" as package,
                "First Category" as category, "Price" as price
                FROM parts WHERE parts MATCH :number"""
            cur.execute(query, {"number": number})
            return next((n for n in cur.fetchall() if n["lcsc"] == number), {})

    def is_download_running(self) -> bool:
        """Report the live worker claim independently of catalog readiness."""
        with self.download_lock:
            return self._download_running

    def has_usable_parts_catalog(self, *, check_integrity: bool = True) -> bool:
        """Check queryable catalog tables, optionally scanning database integrity."""
        try:
            with contextlib.closing(self._read_database(self.partsdb_file)) as con:
                if check_integrity and con.execute(
                    "PRAGMA quick_check(1)"
                ).fetchone() != ("ok",):
                    return False
                fields = ", ".join(
                    f'parts."{field}"'
                    for field in [*DB_FIELDS, "Second Category", "Solder Joint"]
                )
                first = con.execute(f"SELECT {fields} FROM parts LIMIT 1").fetchone()
                number = str(first[0]) if first else "C1"
                phrase = '"' + number.replace('"', '""') + '"'
                con.execute(
                    f"SELECT {fields} FROM parts WHERE parts MATCH ? LIMIT 1",
                    (phrase,),
                ).fetchone()
                con.execute(
                    'SELECT categories."First Category", categories."Second Category" '
                    "FROM categories LIMIT 1"
                ).fetchone()
                # Metadata is optional: the window title has a no-metadata fallback.
            return True
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Parts catalog is unavailable: %s", error)
            return False

    @property
    def download_attempt(self) -> int:
        """Identify the latest accepted worker, including failed attempts."""
        with self.download_lock:
            return self._download_attempt

    def update(self) -> None:
        """Update the sqlite parts database from the JLCPCB CSV."""
        with self.download_lock:
            if self._download_running:
                self.logger.info(
                    "Download already running, ignoring duplicate request."
                )
                return
            self._download_running = True
            self._download_attempt += 1
            attempt = self._download_attempt
            self.state = LibraryState.DOWNLOAD_RUNNING
            source = (self.selected_library, self.partsdb_file)

        def run_download() -> None:
            """Retain the accepted attempt identity until its worker terminates."""
            self._download_wrapper(source, attempt)

        try:
            Thread(target=run_download).start()
        except Exception:
            self._finish_download(source, attempt, False, check_integrity=False)
            raise

    def _finish_download(
        self,
        source: tuple[str, str],
        attempt: int,
        succeeded: bool,
        *,
        check_integrity: bool = True,
    ) -> None:
        """Release the worker claim and notify queued catalog changes on every exit."""
        # Keep the worker claim while checking the remaining file, but do not hold
        # the lock across the integrity scan: UI settings must still be deferrable.
        usable = succeeded or self.has_usable_parts_catalog(
            check_integrity=check_integrity
        )
        with self.download_lock:
            self.state = (
                LibraryState.INITIALIZED if usable else LibraryState.UPDATE_NEEDED
            )
            self._download_running = False
        if succeeded:
            wx.PostEvent(
                self.parent,
                DownloadCompletedEvent(library=self, source=source, attempt=attempt),
            )
        wx.PostEvent(
            self.parent,
            DownloadFinishedEvent(
                library=self, source=source, attempt=attempt, succeeded=succeeded
            ),
        )

    def _download_wrapper(self, source: tuple[str, str], attempt: int) -> None:
        """Run the download worker with guaranteed state cleanup."""
        succeeded = False
        try:
            wx.PostEvent(
                self.parent,
                DownloadStartedEvent(library=self, source=source, attempt=attempt),
            )
            succeeded = self.download()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.exception("Unexpected error while downloading parts database")
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Unexpected error while downloading parts database: {exc}",
                    style="error",
                ),
            )
        finally:
            self._finish_download(source, attempt, succeeded)

    def download(self) -> bool:
        """Actual worker thread that downloads and imports the parts data."""
        start = time.time()

        # Get library configuration for selected library
        library_config = LIBRARY_CONFIGS[self.selected_library]

        # Define basic variables
        url_stub = "https://bouni.github.io/kicad-jlcpcb-tools/"
        cnt_file = library_config.chunk_file_name
        progress_file = os.path.join(self.datadir, f"{library_config.name}.progress")
        chunk_file_stub = library_config.name.replace(".db", ".db.zip.")
        completed_chunks = set()

        self.logger.debug("Starting download of JLCPCB parts database...")
        self.logger.debug(
            "Using library: %s (basefile %s)",
            self.selected_library,
            chunk_file_stub,
        )

        # Check if there is a progress file
        if os.path.exists(progress_file):
            with open(progress_file) as f:
                # Read completed chunk indices from the progress file
                completed_chunks = {int(line.strip()) for line in f.readlines()}

        # Get the total number of chunks to download
        try:
            r = requests.get(
                url_stub + cnt_file, allow_redirects=True, stream=True, timeout=300
            )
            if r.status_code != requests.codes.ok:
                wx.PostEvent(
                    self.parent,
                    MessageEvent(
                        title="HTTP GET Error",
                        text=f"Failed to fetch count of database parts, error code {r.status_code}\n"
                        + "URL was:\n"
                        f"'{url_stub + cnt_file}'",
                        style="error",
                    ),
                )
                return False

            total_chunks = int(r.text)
        except Exception as e:
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Failed to fetch database chunk count, {e}",
                    style="error",
                ),
            )
            return False

        # Re-download incomplete or missing chunks
        for i in range(total_chunks):
            chunk_index = i + 1
            chunk_file = chunk_file_stub + f"{chunk_index:03}"
            chunk_path = os.path.join(self.datadir, chunk_file)

            # Check if the chunk is logged as completed but the file might be incomplete
            if chunk_index in completed_chunks:
                if os.path.exists(chunk_path):
                    # Validate the size of the chunk file
                    try:
                        expected_size = int(
                            requests.head(
                                url_stub + chunk_file, timeout=300
                            ).headers.get("Content-Length", 0)
                        )
                        actual_size = os.path.getsize(chunk_path)
                        if actual_size == expected_size:
                            self.logger.debug(
                                "Skipping already downloaded and validated chunk %d.",
                                chunk_index,
                            )
                            continue
                        else:
                            self.logger.warning(
                                "Chunk %d is incomplete, re-downloading.", chunk_index
                            )
                    except Exception as e:
                        self.logger.warning(
                            "Unable to validate chunk %d, re-downloading. Error: %s",
                            chunk_index,
                            e,
                        )
                else:
                    self.logger.warning(
                        "Chunk %d marked as completed but file is missing, re-downloading.",
                        chunk_index,
                    )

            # Download the chunk
            try:
                with open(chunk_path, "wb") as f:
                    r = requests.get(
                        url_stub + chunk_file,
                        allow_redirects=True,
                        stream=True,
                        timeout=300,
                    )
                    if r.status_code != requests.codes.ok:
                        wx.PostEvent(
                            self.parent,
                            MessageEvent(
                                title="Download Error",
                                text=f"Failed to download chunk {chunk_index}, error code {r.status_code}\n"
                                + "URL was:\n"
                                f"'{url_stub + chunk_file}'",
                                style="error",
                            ),
                        )
                        return False

                    size = int(r.headers.get("Content-Length", 0))
                    self.logger.debug(
                        "Downloading chunk %d/%d (%.2f MB)",
                        chunk_index,
                        total_chunks,
                        size / 1024 / 1024,
                    )
                    for data in r.iter_content(chunk_size=4096):
                        f.write(data)
                        progress = f.tell() / size * 100
                        wx.PostEvent(self.parent, DownloadProgressEvent(value=progress))
                    self.logger.debug("Chunk %d downloaded successfully.", chunk_index)

                # Update progress file after successful download
                with open(progress_file, "a") as f:
                    f.write(f"{chunk_index}\n")

            except Exception as e:
                wx.PostEvent(
                    self.parent,
                    MessageEvent(
                        title="Download Error",
                        text=f"Failed to download chunk {chunk_index}, {e}",
                        style="error",
                    ),
                )
                return False

        # Delete progress file to indicate the download is complete
        if os.path.exists(progress_file):
            os.remove(progress_file)

        # Combine and extract downloaded files
        self.logger.debug("Combining and extracting zip part files...")
        try:
            unzip_parts(self.parent, self.datadir, library_config.name + ".zip")
        except Exception as e:
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Extract Error",
                    text=f"Failed to combine and extract the JLCPCB database, {e}",
                    style="error",
                ),
            )
            return False

        # Extraction can leave a nonempty but incomplete or unreadable database.
        if not self.has_usable_parts_catalog():
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text="Failed to extract the database file from the downloaded zip.",
                    style="error",
                ),
            )
            return False

        end = time.time()
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Successfully downloaded and imported the JLCPCB database in {end - start:.2f} seconds!",
                style="info",
            ),
        )
        return True

    def create_tables(self, headers: Iterable[str]) -> None:
        """Create all tables."""
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
        self.create_correction_table()
        self.create_part_preferences_table()

    @property
    def categories(self):
        """The primary categories in the database.

        Caching the relatively small set of category and subcategory maps
        gives a noticeable speed improvement over repeatedly reading the
        information from the on-disk database.
        """
        if not self.category_map:
            self.category_map.setdefault("", [])

            # Populate the cache.
            with (
                contextlib.closing(sqlite3.connect(self.partsdb_file)) as con,
                con as cur,
            ):
                for row in cur.execute(
                    'SELECT * from categories ORDER BY UPPER("First Category"), UPPER("Second Category")'
                ):
                    self.category_map.setdefault(row[0], []).append(row[1])
        tmp = list(self.category_map.keys())
        tmp.insert(0, "All")
        return tmp

    def get_subcategories(self, category):
        """Get the subcategories associated with the given category."""
        return self.category_map[category]

    def _legacy_correction_sources(self) -> tuple[str, str]:
        """Associate global legacy archives with stable completion identities."""
        return self.rotationsdb_file, self.partsdb_file

    @staticmethod
    def _legacy_migration_key(source: str) -> str:
        """Keep completed legacy archives from replaying over repaired records."""
        return f"rotation:{Path(source).resolve()}:rotation"

    def _legacy_rotation_rows(
        self, source: str
    ) -> Optional[list[tuple[object, object]]]:  # noqa: UP045
        """Read a legacy archive without creating or removing source storage."""
        if not Path(source).exists():
            return None
        with contextlib.closing(self._read_database(source)) as con:
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rotation'"
            ).fetchone()
            if not table:
                return None
            # Legacy releases named the second column differently; preserve its
            # original value by position while requiring the original two fields.
            cursor = con.execute("SELECT * FROM rotation")
            if len(cursor.description) != 2:
                raise sqlite3.DatabaseError(
                    "legacy rotation table must have exactly two columns"
                )
            return cursor.fetchall()

    def _migrate_correction_sources(
        self, sources: Iterable[str]
    ) -> CorrectionBatchResult:
        """Inspect once, retain unresolved work, and atomically copy eligible archives."""
        target = self.globalcorrectionsdb_file
        if not Path(target).exists() or Path(target).stat().st_size == 0:
            self.create_correction_table(target)
        try:
            with contextlib.closing(self._read_database(target)) as con:
                con.execute("BEGIN")
                self._validate_correction_schema(con)
                completed, metadata = self._correction_metadata(con)
                states = {key: status for key, _, status, _ in metadata}
                candidates = {}
                for source in sources:
                    key = self._legacy_migration_key(source)
                    if key not in completed and states.get(key) != "empty":
                        candidates.setdefault(key, source)
        except (sqlite3.Error, OSError) as error:
            raise self._storage_error(target, error, "migration") from error
        if not candidates:
            return CorrectionBatchResult()

        identity = str(Path(target).resolve())
        known_sources = self._known_legacy_sources.setdefault(identity, set())
        outcomes = []
        pending = []
        deferred_by = None
        for key, source in candidates.items():
            known = states.get(key) == "pending" or key in known_sources
            status = "pending" if known else "deferred"
            if deferred_by is not None:
                message = f"legacy transfer is waiting for higher-priority archive {deferred_by}; restore that archive and reopen Corrections Manager to retry"
            else:
                try:
                    rows = self._legacy_rotation_rows(source)
                    if rows is None or not rows:
                        if known:
                            message = "previously identified legacy corrections are no longer present; restore the source archive and reopen Corrections Manager to retry"
                        else:
                            outcomes.append((key, source, "empty", ""))
                            continue
                    else:
                        known_sources.add(key)
                        outcomes.append(
                            (
                                key,
                                source,
                                "pending",
                                "legacy corrections have not been transferred; reopen Corrections Manager to retry before generating fabrication files",
                            )
                        )
                        pending.append((source, key, rows))
                        continue
                except (sqlite3.Error, OSError) as error:
                    message = f"cannot read legacy corrections: {error}; restore the source archive and reopen Corrections Manager to retry"
            outcomes.append((key, source, status, message))
            deferred_by = deferred_by or source

        # Positive knowledge must survive a later transfer rollback or process
        # interruption. Completion, in contrast, commits together with the rows.
        try:
            with self._correction_transaction(target, create=True) as con:
                completed, metadata = self._correction_metadata(con)
                current_states = {key: status for key, _, status, _ in metadata}
                for outcome in outcomes:
                    if outcome[0] in completed:
                        continue
                    current = current_states.get(outcome[0])
                    if current == "pending" and outcome[2] != "pending":
                        outcome = (
                            outcome[0],
                            outcome[1],
                            "pending",
                            outcome[3]
                            or "previously identified legacy corrections still require transfer; restore the archive and retry",
                        )
                    if outcome[2] == "empty":
                        self._complete_correction_migration(con, *outcome[:2])
                    else:
                        con.execute(
                            "INSERT OR REPLACE INTO correction_migration_state VALUES (?, ?, ?, ?)",
                            outcome,
                        )
        except CorrectionDataError as error:
            if any(outcome[2] == "pending" for outcome in outcomes):
                raise
            # Failure to record absence or an unclassified archive is not
            # evidence that valid active corrections are incomplete.
            warnings = tuple(
                CorrectionIssue("migration", None, message, source=source)
                for _, source, status, message in outcomes
                if status == "deferred"
            )
            self._migration_session_diagnostics[identity] = (
                (),
                (*warnings, *error.issues),
            )
            return CorrectionBatchResult()
        if not pending:
            return CorrectionBatchResult()

        inserted = skipped = 0
        with self._correction_transaction(target, create=True) as con:
            established = {
                pattern
                for (pattern,) in con.execute("SELECT regex FROM correction")
                if isinstance(pattern, str) and pattern.strip()
            }
            completed, metadata = self._correction_metadata(con)
            current_states = {key: status for key, _, status, _ in metadata}
            earlier = list(candidates)
            for source, key, rows in pending:
                if key in completed:
                    continue
                if any(
                    current_states.get(earlier_key) in {"pending", "deferred"}
                    for earlier_key in earlier[: earlier.index(key)]
                ):
                    # Another coordinator may have discovered a higher-priority
                    # problem after our preflight. Keep lower-priority work pending.
                    break
                novel = [
                    row
                    for row in rows
                    if not (isinstance(row[0], str) and row[0] in established)
                ]
                for pattern, rotation in novel:
                    rowid = con.execute(
                        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES (?, ?, 0, 0)",
                        (pattern, rotation),
                    ).lastrowid
                    stored = con.execute(
                        "SELECT regex, rotation, offset_x, offset_y FROM correction WHERE rowid=?",
                        (rowid,),
                    ).fetchone()
                    original = Correction.parse(pattern, rotation, (0, 0))
                    if original is None:
                        # Malformed input must remain verbatim for repair. In
                        # particular, TEXT affinity must not turn numeric legacy
                        # patterns into silently accepted regular expressions.
                        preserved = (
                            stored is not None
                            and all(
                                type(before) is type(after) and before == after
                                for before, after in zip(
                                    (pattern, rotation), stored[:2]
                                )
                            )
                            and stored[2:] == (0, 0)
                        )
                    else:
                        # Safe representation changes such as '90' to INTEGER 90
                        # are allowed only when the complete normalized value agrees.
                        preserved = (
                            stored is not None
                            and Correction.parse(stored[0], stored[1], stored[2:])
                            == original
                        )
                    if not preserved:
                        raise CorrectionDataError(
                            (
                                CorrectionIssue(
                                    "migration",
                                    (pattern, rotation),
                                    f"destination schema at {target} cannot preserve this legacy correction; "
                                    "restore a compatible correction schema before retrying; the source archive is unchanged",
                                    source=source,
                                ),
                            )
                        )
                self._complete_correction_migration(
                    con, key, str(Path(source).resolve())
                )
                current_states.pop(key, None)
                # Preserve contradictions within this source before allowing it
                # to take precedence over lower-priority historical archives.
                established.update(
                    pattern
                    for pattern, _ in rows
                    if isinstance(pattern, str) and pattern.strip()
                )
                inserted += len(novel)
                skipped += len(rows) - len(novel)
        self.logger.info(
            "Transferred %d legacy corrections to %s; preserved %d established patterns.",
            inserted,
            target,
            skipped,
        )
        return CorrectionBatchResult(inserted=inserted, skipped=skipped)

    def migrate_corrections_from_rotation(self) -> CorrectionBatchResult:
        """Transfer missing rotations.db patterns to global storage, retaining the archive."""
        return self._migrate_correction_sources((self.rotationsdb_file,))

    def migrate_corrections_from_parts(self) -> CorrectionBatchResult:
        """Transfer missing legacy parts patterns without dropping their source."""
        return self._migrate_correction_sources((self.partsdb_file,))

    def migrate_corrections(self) -> tuple[CorrectionIssue, ...]:
        """Retry incomplete global transfers while retaining failures for recovery."""
        target = self.globalcorrectionsdb_file
        identity = str(Path(target).resolve())
        diagnostics = self._migration_session_diagnostics
        diagnostics.pop(identity, None)
        try:
            current_sources = list(self._legacy_correction_sources())
            sources = current_sources[:1]
            if Path(self.globalcorrectionsdb_file).exists():
                with contextlib.closing(
                    self._read_database(self.globalcorrectionsdb_file)
                ) as con:
                    completed, states = self._correction_metadata(con)
                    if _INITIAL_DEFAULTS_KEY not in completed:
                        # Initial archives retain priority even if configuration
                        # changes after storage creation but before examination.
                        sources.extend(
                            source
                            for source in self._initial_seed_sources(states)
                            if source not in sources
                        )
                    # Changing the selected parts library must not strand known
                    # pending work for the same global correction database.
                    sources.extend(
                        source
                        for key, source, status, _ in states
                        if isinstance(key, str)
                        and key.startswith("rotation:")
                        and status in {"pending", "deferred"}
                        and source not in sources
                    )
            sources.extend(
                source for source in current_sources[1:] if source not in sources
            )
            self._migrate_correction_sources(sources)
            with contextlib.closing(
                self._read_database(self.globalcorrectionsdb_file)
            ) as con:
                return tuple(
                    CorrectionIssue("migration", None, message, source=source)
                    for _, source, status, message in self._correction_metadata(con)[1]
                    if status == "pending"
                )
        except (sqlite3.Error, OSError, CorrectionDataError) as error:
            self.logger.warning("Correction migration remains unresolved: %s", error)
            issues = (
                error.issues
                if isinstance(error, CorrectionDataError)
                else self._storage_error(target, error, "migration").issues
            )
            diagnostics[identity] = (issues, ())
            return issues

    def retry_correction_migrations(self) -> tuple[CorrectionIssue, ...]:
        """Retry active global archives once and resume eligible initial defaults."""
        if self.correctionsdb_file != self.globalcorrectionsdb_file:
            return ()
        issues = self.migrate_corrections()
        if not issues:
            self._start_initial_remote_corrections(self.globalcorrectionsdb_file)
        return issues

    def _start_initial_remote_corrections(self, target: str) -> None:
        """Schedule optional defaults; failures warn without invalidating stored data."""
        identity = str(Path(target).resolve())
        claimed = False
        try:
            with contextlib.closing(self._read_database(target)) as con:
                con.execute("BEGIN")
                if not self._initial_seed_is_eligible(con, target):
                    return
            with _INITIAL_DOWNLOAD_LOCK:
                if identity in _INITIAL_DOWNLOAD_TARGETS:
                    return
                _INITIAL_DOWNLOAD_TARGETS.add(identity)
                claimed = True
            Thread(
                target=self._fetch_initial_remote_corrections,
                args=(target,),
                daemon=True,
            ).start()
        except (sqlite3.Error, OSError, RuntimeError) as error:
            if claimed:
                with _INITIAL_DOWNLOAD_LOCK:
                    _INITIAL_DOWNLOAD_TARGETS.discard(identity)
            issues, warnings = self._migration_session_diagnostics.get(
                identity, ((), ())
            )
            self._migration_session_diagnostics[identity] = (
                issues,
                (
                    *warnings,
                    *self._storage_error(target, error, "initial download").issues,
                ),
            )

    def _initial_seed_is_eligible(self, con: sqlite3.Connection, target: str) -> bool:
        """Require durable classification of captured sources before seeding defaults."""
        completed, states = self._correction_metadata(con)
        if _INITIAL_DEFAULTS_KEY in completed:
            return False
        session = self._migration_session_diagnostics.get(
            str(Path(target).resolve()), ((), ())
        )
        if any(session):
            return False
        if any(status in {"pending", "deferred"} for _, _, status, _ in states):
            return False
        sources = self._initial_seed_sources(states)
        if not sources:
            return False
        terminal = {key for key, _, status, _ in states if status == "empty"}
        return all(
            self._legacy_migration_key(source) in terminal
            or self._legacy_migration_key(source) in completed
            for source in sources
        )

    @staticmethod
    def _initial_seed_sources(
        states: Sequence[tuple[object, str, str, str]],
    ) -> list[str]:
        """Decode the original source identities retained across interrupted startup."""
        seed = next(
            (
                message
                for key, _, status, message in states
                if key == _INITIAL_DEFAULTS_KEY and status == "seed-pending"
            ),
            None,
        )
        if seed is None:
            return []
        try:
            sources = json.loads(seed)
        except (TypeError, ValueError) as error:
            raise sqlite3.DatabaseError(
                "initial correction source metadata is unreadable"
            ) from error
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(source, str) for source in sources)
        ):
            raise sqlite3.DatabaseError(
                "initial correction source metadata must name its archives"
            )
        return sources

    def _fetch_initial_remote_corrections(self, target: str) -> None:
        """Release the in-process download claim even after an unsuccessful seed."""
        try:
            self.fetch_remote_corrections(target, initial=True)
        finally:
            with _INITIAL_DOWNLOAD_LOCK:
                _INITIAL_DOWNLOAD_TARGETS.discard(str(Path(target).resolve()))

    def fetch_remote_corrections(
        self,
        db_path: Optional[DatabasePath] = None,  # noqa: UP045
        *,
        initial: bool = False,
    ) -> Optional[CorrectionBatchResult]:  # noqa: UP045
        """Validate the entire remote CSV and add missing patterns in one transaction."""
        target = db_path if db_path is not None else self.correctionsdb_file
        url = "https://raw.githubusercontent.com/matthewlai/JLCKicadTools/master/jlc_kicad_tools/cpl_rotations_db.csv"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            corrections = parse_corrections_csv(response.text, source=url)
            result = self.apply_corrections(
                corrections,
                db_path=target,
                overwrite=False,
                migration_key=_INITIAL_DEFAULTS_KEY if initial else None,
            )
            self.logger.info(
                "Downloaded %d corrections to %s.", result.inserted, target
            )
            return result
        except (requests.RequestException, CorrectionDataError, UnicodeError) as error:
            self.logger.warning(
                "Failed to download corrections to %s: %s", target, error
            )
            wx.PostEvent(
                self.parent,
                MessageEvent(
                    title="Correction Download Error",
                    text=f"Corrections were not imported into {target}.\n\n{error}",
                    style="error",
                ),
            )
            return None

    def migrate_legacy_part_preferences(self) -> None:
        """Move legacy part preferences out of the parts catalog database."""
        with (
            contextlib.closing(sqlite3.connect(self.partsdb_file)) as pdb,
            contextlib.closing(
                sqlite3.connect(self.part_preferences_db_file)
            ) as preferences_db,
            pdb as pcur,
            preferences_db as preferences_cursor,
        ):
            try:
                result = pcur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    preferences_cursor.execute(
                        "INSERT INTO mapping VALUES (?, ?)",
                        (r[0], r[1]),
                    )
                    preferences_cursor.commit()
                self.logger.debug(
                    "Migrated %d part preferences to a separate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS mapping")
                pcur.commit()
                self.logger.debug(
                    "Removed legacy part preferences from the parts database."
                )
            except sqlite3.OperationalError:
                return

    def get_parts_db_info(self) -> Optional[PartsDatabaseInfo]:  # noqa: UP045
        """Retrieve the database information."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            try:
                meta = cur.execute(
                    "SELECT last_update, size, partcount FROM meta"
                ).fetchone()
                if meta:
                    return PartsDatabaseInfo(meta[0], meta[1], meta[2])
                return None
            except sqlite3.OperationalError:
                return None
