"""Impedance-only settings and catalog storage in the existing project database."""

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional, Union
from uuid import uuid4

PathValue = Union[str, os.PathLike]


class BoardIdentityError(ValueError):
    """A board cannot be identified safely from its saved filename."""


class DatabaseMigrationError(RuntimeError):
    """The impedance tables cannot be opened without losing information."""


class ConfigConflictError(RuntimeError):
    """Another editor saved this board's configuration after it was loaded."""


class ImpedanceDatabase:
    """Persist impedance intent without migrating existing parts or counters."""

    CONFIG_VERSION = 1

    def __init__(self, dbfile: PathValue, *, initialize: bool = True) -> None:
        self.path = Path(dbfile).resolve()
        self.project_root = (
            self.path.parent.parent
            if self.path.parent.name == "jlcpcb"
            else self.path.parent
        )
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        """Create impedance storage when the user explicitly saves feature data."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a bounded connection and atomically commit or roll back writes."""
        connection = sqlite3.connect(
            str(self.path) if write else f"{self.path.as_uri()}?mode=ro",
            timeout=5.0,
            uri=not write,
        )
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("PRAGMA query_only = ON")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def validate_board_path(board_path: PathValue) -> Path:
        """Require a saved PCB before any board-specific database mutation."""
        if not str(board_path).strip():
            raise BoardIdentityError(
                "Save the PCB before configuring controlled impedance."
            )
        path = Path(board_path).resolve()
        if path.suffix.lower() != ".kicad_pcb" or not path.is_file():
            raise BoardIdentityError(
                "The PCB must have an existing .kicad_pcb file before its "
                "impedance settings can be opened."
            )
        return path

    def _initialize(self) -> None:
        """Create and validate only additive impedance tables, in one transaction."""
        try:
            with self.connect(write=True) as connection:
                self._create_tables(connection)
                self._validate_tables(connection)
        except sqlite3.DatabaseError as error:
            raise DatabaseMigrationError(
                f"Cannot initialize controlled-impedance storage: {error}"
            ) from error

    def _create_tables(self, connection: sqlite3.Connection) -> None:
        """Create only the board identity, impedance settings, and catalog tables."""
        connection.execute(
            "CREATE TABLE IF NOT EXISTS boards ("
            "board_id TEXT PRIMARY KEY NOT NULL, "
            "relative_path TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS board_feature_config ("
            "board_id TEXT NOT NULL REFERENCES boards(board_id), "
            "feature TEXT NOT NULL, version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL CHECK (revision > 0), "
            "enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)), "
            "payload_json TEXT NOT NULL, PRIMARY KEY(board_id, feature))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS impedance_stackup_catalog ("
            "layer_count INTEGER PRIMARY KEY NOT NULL CHECK(layer_count BETWEEN 2 AND 64), "
            "payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS impedance_calculator_config ("
            "id INTEGER PRIMARY KEY NOT NULL CHECK(id = 1), "
            "payload_json TEXT NOT NULL)"
        )

    def _validate_tables(self, connection: sqlite3.Connection) -> None:
        """Reject damaged impedance tables without modifying unrelated schemas."""
        tables = {
            "boards": ({"board_id", "relative_path", "display_name"}, ("board_id",)),
            "board_feature_config": (
                {
                    "board_id",
                    "feature",
                    "version",
                    "revision",
                    "enabled",
                    "payload_json",
                },
                ("board_id", "feature"),
            ),
            "impedance_stackup_catalog": (
                {"layer_count", "payload_json"},
                ("layer_count",),
            ),
        }
        optional = {
            "impedance_calculator_config": (
                {"id", "payload_json"},
                ("id",),
            ),
        }
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table, (required, primary_key) in {**tables, **optional}.items():
            if table in optional and table not in present:
                continue
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual_key = tuple(
                row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]
            )
            if (
                not required.issubset({row[1] for row in columns})
                or actual_key != primary_key
            ):
                raise DatabaseMigrationError(
                    f"The {table} schema requires repair before configuring impedance."
                )
            if not required.issubset({row[1] for row in columns if row[3]}):
                raise DatabaseMigrationError(
                    f"The {table} schema permits incomplete ownership records."
                )
            if table == "board_feature_config":
                foreign_keys = connection.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
                if not any(
                    row[2] == "boards" and row[3] == "board_id" and row[4] == "board_id"
                    for row in foreign_keys
                ):
                    raise DatabaseMigrationError(
                        f"The {table} board ownership constraint is missing."
                    )
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone():
                raise DatabaseMigrationError(
                    f"The {table} table contains invalid ownership records."
                )
        unique_paths = False
        for index in connection.execute("PRAGMA index_list(boards)").fetchall():
            if not index[2] or index[4]:
                continue
            # Index names come from SQLite; quote them as identifiers.
            index_name = index[1].replace('"', '""')
            columns = connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
            if [row[2] for row in columns] == ["relative_path"]:
                unique_paths = True
        if not unique_paths:
            raise DatabaseMigrationError(
                "The boards table must uniquely identify PCB paths."
            )
        if connection.execute(
            "SELECT 1 FROM boards WHERE typeof(board_id) != 'text' "
            "OR trim(board_id) = '' OR typeof(relative_path) != 'text' "
            "OR trim(relative_path) = '' LIMIT 1"
        ).fetchone():
            raise DatabaseMigrationError(
                "The boards table contains an incomplete identity."
            )

    def _relative_path(self, path: Path) -> str:
        """Keep identities relocatable with the enclosing project directory."""
        return Path(os.path.relpath(path, self.project_root)).as_posix()

    def _validate_existing_tables(self, connection: sqlite3.Connection) -> bool:
        """Distinguish untouched legacy stores from valid impedance storage."""
        owned_schema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE lower(name) IN "
            "('boards', 'board_feature_config', 'impedance_stackup_catalog', "
            "'impedance_calculator_config') LIMIT 1"
        ).fetchone()
        if owned_schema is None:
            return False
        self._validate_tables(connection)
        return True

    def find_board(self, board_path: PathValue) -> Optional[str]:
        """Read an existing board identity without initializing or registering it."""
        path = self.validate_board_path(board_path)
        if not self.path.exists():
            return None
        try:
            with self.connect() as connection:
                if not self._validate_existing_tables(connection):
                    return None
                row = connection.execute(
                    "SELECT board_id FROM boards WHERE relative_path = ?",
                    (self._relative_path(path),),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise DatabaseMigrationError(
                f"Cannot read controlled-impedance storage: {error}"
            ) from error
        return row[0] if row else None

    def resolve_board(self, board_path: PathValue) -> str:
        """Resolve only impedance identity; never claim parts or generation counters."""
        path = self.validate_board_path(board_path)
        relative_path = self._relative_path(path)
        with self.connect(write=True) as connection:
            row = connection.execute(
                "SELECT board_id FROM boards WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            if row:
                return row[0]
            board_id = str(uuid4())
            connection.execute(
                "INSERT INTO boards(board_id, relative_path, display_name) VALUES (?, ?, ?)",
                (board_id, relative_path, path.stem),
            )
            return board_id

    def ensure_current_board(self, board_id: str, board_path: PathValue) -> None:
        """Reject a stale window if its saved filename or registry identity changed."""
        path = self.validate_board_path(board_path)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT relative_path FROM boards WHERE board_id = ?", (board_id,)
            ).fetchone()
        if row is None or row[0] != self._relative_path(path):
            raise BoardIdentityError(
                "The PCB identity changed. Reopen JLCPCB Tools for the current board."
            )

    def load_config(self, board_id: str) -> Optional[dict[str, Any]]:
        """Read the controlled-impedance configuration and its edit revision."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version, revision, enabled, payload_json FROM board_feature_config "
                "WHERE board_id = ? AND feature = 'impedance'",
                (board_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != self.CONFIG_VERSION or not isinstance(row[1], int) or row[1] < 1:
            raise ValueError(
                "The saved impedance configuration version or revision is invalid."
            )
        if row[2] not in (0, 1):
            raise ValueError(
                "The saved impedance configuration enable state is invalid."
            )
        payload = json.loads(row[3])
        if not isinstance(payload, dict):
            raise TypeError("The saved impedance configuration must be an object.")
        if "enabled" in payload and payload["enabled"] != bool(row[2]):
            raise ValueError(
                "The impedance configuration enable state is inconsistent."
            )
        return {
            "version": row[0],
            "revision": row[1],
            "enabled": bool(row[2]),
            "payload": payload,
        }

    def load_stackup_catalog(self, layer_count: int) -> Optional[dict[str, Any]]:
        """Read project-shared catalog cache without touching any board revision."""
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Catalog copper-layer count must be between 2 and 64.")
        if not self.path.exists():
            return None
        try:
            with self.connect() as connection:
                if not self._validate_existing_tables(connection):
                    return None
                row = connection.execute(
                    "SELECT payload_json FROM impedance_stackup_catalog WHERE layer_count = ?",
                    (layer_count,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise DatabaseMigrationError(
                f"Cannot read controlled-impedance storage: {error}"
            ) from error
        if row is None:
            return None
        if not isinstance(row[0], str) or len(row[0].encode("utf-8")) > 32_000_000:
            raise ValueError("Cached stackup catalog is invalid or too large.")
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            raise ValueError("Cached stackup catalog must be an object.")  # noqa: TRY004 - retained validation contract
        return payload

    def save_stackup_catalog(self, layer_count: int, payload: dict[str, Any]) -> None:
        """Atomically replace public catalog rows and successful-check metadata."""
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Catalog copper-layer count must be between 2 and 64.")
        if not isinstance(payload, dict):
            raise ValueError("Cached stackup catalog must be an object.")  # noqa: TRY004 - retained validation contract
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        if len(encoded.encode("utf-8")) > 32_000_000:
            raise ValueError("Cached stackup catalog exceeds its storage limit.")
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO impedance_stackup_catalog (layer_count, payload_json) VALUES (?, ?) "
                "ON CONFLICT(layer_count) DO UPDATE SET payload_json = excluded.payload_json",
                (layer_count, encoded),
            )

    def load_calculator_config(self) -> Optional[dict[str, Any]]:
        """Read project-shared calculator metadata without touching board revisions."""
        if not self.path.exists():
            return None
        try:
            with self.connect() as connection:
                if not self._validate_existing_tables(connection):
                    return None
                present = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'impedance_calculator_config'"
                ).fetchone()
                if present is None:
                    return None
                row = connection.execute(
                    "SELECT payload_json FROM impedance_calculator_config WHERE id = 1"
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise DatabaseMigrationError(
                f"Cannot read controlled-impedance storage: {error}"
            ) from error
        if row is None:
            return None
        if not isinstance(row[0], str) or len(row[0].encode("utf-8")) > 32_000_000:
            raise ValueError("Cached calculator configuration is invalid or too large.")
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            raise ValueError("Cached calculator configuration must be an object.")  # noqa: TRY004
        return payload

    def save_calculator_config(self, payload: dict[str, Any]) -> None:
        """Atomically replace calculator metadata and successful-check timestamp."""
        if not isinstance(payload, dict):
            raise ValueError("Cached calculator configuration must be an object.")  # noqa: TRY004
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        if len(encoded.encode("utf-8")) > 32_000_000:
            raise ValueError(
                "Cached calculator configuration exceeds its storage limit."
            )
        with self.connect(write=True) as connection:
            self._create_tables(connection)
            connection.execute(
                "INSERT INTO impedance_calculator_config (id, payload_json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json",
                (encoded,),
            )

    @staticmethod
    def _raw_config_record(
        connection: sqlite3.Connection, board_id: str
    ) -> Optional[tuple[Any, ...]]:
        """Read values without attempting to parse possibly damaged feature data."""
        return connection.execute(
            "SELECT version, revision, enabled, payload_json FROM board_feature_config "
            "WHERE board_id = ? AND feature = 'impedance'",
            (board_id,),
        ).fetchone()

    @staticmethod
    def _reset_token(row: Optional[tuple[Any, ...]]) -> str:
        """Fingerprint raw SQLite scalars only for the lifetime of a reset prompt."""
        return hashlib.sha256(repr(row).encode("utf-8")).hexdigest()

    def config_reset_token(self, board_id: str) -> str:
        """Fingerprint the exact stored configuration even when it cannot be parsed."""
        with self.connect() as connection:
            self._require_board(connection, board_id)
            return self._reset_token(self._raw_config_record(connection, board_id))

    def reset_config(
        self, board_id: str, expected_token: str, empty_payload: dict[str, Any]
    ) -> int:
        """Replace explicitly confirmed, unchanged settings with disabled defaults."""
        if not isinstance(empty_payload, dict):
            raise TypeError("The replacement configuration must be a dictionary.")
        if empty_payload.get("enabled", False) is not False:
            raise ValueError("A reset configuration must start disabled.")
        encoded = json.dumps(
            empty_payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self.connect(write=True) as connection:
            self._require_board(connection, board_id)
            row = self._raw_config_record(connection, board_id)
            if self._reset_token(row) != expected_token:
                raise ConfigConflictError(
                    "The configuration changed before reset. Reload it and try again."
                )
            revision = (
                row[1] + 1
                if row and isinstance(row[1], int) and 0 < row[1] < 2**63 - 1
                else 1
            )
            connection.execute(
                "INSERT INTO board_feature_config "
                "(board_id, feature, version, revision, enabled, payload_json) "
                "VALUES (?, 'impedance', ?, ?, 0, ?) "
                "ON CONFLICT(board_id, feature) DO UPDATE SET "
                "version = excluded.version, revision = excluded.revision, "
                "enabled = excluded.enabled, payload_json = excluded.payload_json",
                (board_id, self.CONFIG_VERSION, revision, encoded),
            )
        return revision

    def save_config(
        self,
        board_id: str,
        payload: dict[str, Any],
        enabled: bool,
        expected_revision: int,
    ) -> int:
        """Save one complete document only if its loaded revision is still current."""
        if not isinstance(payload, dict):
            raise TypeError("The impedance configuration payload must be a dictionary.")
        if expected_revision < 0:
            raise ValueError("Configuration revisions cannot be negative.")
        if "enabled" in payload and payload["enabled"] != enabled:
            raise ValueError(
                "The impedance configuration enable state is inconsistent."
            )
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self.connect(write=True) as connection:
            current = self._raw_config_record(connection, board_id)
            if (current[1] if current else 0) != expected_revision:
                raise ConfigConflictError(
                    "The impedance configuration changed in another window. Reload it before saving."
                )
            revision = expected_revision + 1
            connection.execute(
                "INSERT INTO board_feature_config "
                "(board_id, feature, version, revision, enabled, payload_json) "
                "VALUES (?, 'impedance', ?, ?, ?, ?) "
                "ON CONFLICT(board_id, feature) DO UPDATE SET "
                "version = excluded.version, revision = excluded.revision, "
                "enabled = excluded.enabled, payload_json = excluded.payload_json",
                (board_id, self.CONFIG_VERSION, revision, int(enabled), encoded),
            )
        return revision

    @staticmethod
    def _require_board(connection: sqlite3.Connection, board_id: str) -> None:
        """Reject operations on an identity that is not registered here."""
        if not connection.execute(
            "SELECT 1 FROM boards WHERE board_id = ?", (board_id,)
        ).fetchone():
            raise BoardIdentityError("The requested board identity does not exist.")
