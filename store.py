"""Persist plugin assembly choices independently of live PCB design data."""

from collections.abc import Iterable, Sequence
import contextlib
from functools import cmp_to_key
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional

from .bom_estimation.assembly_mode import ComponentProductType
from .footprint_helpers import (
    footprint_uuid,
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_is_dnp,
    get_lcsc_value,
    get_valid_footprints,
)
from .footprint_metadata import footprint_has_tht, get_footprint_pad_count
from .helpers import natural_sort_collation
from .part_info_migration import migrate_part_info

_PART_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS part_info ("
    "board_key TEXT NOT NULL, footprint_uuid TEXT NOT NULL, "
    "lcsc TEXT NOT NULL DEFAULT '', "
    "exclude_from_bom INTEGER NOT NULL DEFAULT 0 CHECK(exclude_from_bom IN (0,1)), "
    "exclude_from_pos INTEGER NOT NULL DEFAULT 0 CHECK(exclude_from_pos IN (0,1)), "
    "is_dnp INTEGER NOT NULL DEFAULT 0 CHECK(is_dnp IN (0,1)), "
    "PRIMARY KEY(board_key, footprint_uuid))"
)
_FIELDS = ("lcsc", "exclude_from_bom", "exclude_from_pos", "is_dnp")


class Store:
    """Join durable assembly choices with the current board and supplier catalog."""

    GENERATION_COUNT_KEY = "generation_count"

    def __init__(self, parent: Any, project_path: str, board: Any) -> None:
        self.parent, self._board = parent, board
        filename = board.GetFileName()
        if not filename:
            raise sqlite3.DatabaseError("Save the PCB before editing assembly choices.")
        self.project_path = str(Path(project_path).resolve())
        self._board_path = Path(filename).resolve()
        try:
            self.board_key = self._board_path.relative_to(self.project_path).as_posix()
        except ValueError as error:
            raise sqlite3.DatabaseError(
                "The PCB is outside this project directory."
            ) from error
        self.datadir = os.path.join(self.project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self.new_part_uuids: set[str] = set()
        parts = self._board_parts()
        Path(self.datadir).mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            migrate_part_info(
                con,
                _PART_SCHEMA,
                self.board_key,
                parts,
                Path(self.project_path),
                getattr(parent, "confirm_part_info_migration", None),
            )
            con.execute(_PART_SCHEMA)
            self._validate_schema(con)
        self.read_all()

    @property
    def board(self) -> Any:
        """Reject modeless callbacks after the board was replaced or saved elsewhere."""
        getter = getattr(self.parent, "_get_current_board", None)
        board = getter() if callable(getter) else self._board
        if board is None or Path(board.GetFileName()).resolve() != self._board_path:
            raise sqlite3.DatabaseError("The PCB changed. Reopen kicad-jlcpcb-tools.")
        return board

    @staticmethod
    def _validate_schema(con: sqlite3.Connection) -> None:
        """Refuse an unsupported table instead of falling back to native settings."""
        columns = con.execute("PRAGMA table_info(part_info)").fetchall()
        if {column[1] for column in columns} != {
            "board_key",
            "footprint_uuid",
            *_FIELDS,
        } or {column[1]: column[5] for column in columns if column[5]} != {
            "board_key": 1,
            "footprint_uuid": 2,
        }:
            raise sqlite3.DatabaseError(
                "Unsupported part_info schema; assignments were not changed."
            )

    def _board_parts(self) -> list[dict[str, Any]]:
        """Capture live design facts and initial values, checking unique identities."""
        parts = []
        refs: set[str] = set()
        uuids: set[str] = set()
        for fp in get_valid_footprints(self.board):
            ref, uuid = fp.GetReference(), footprint_uuid(fp)
            if ref in refs or uuid in uuids:
                raise sqlite3.IntegrityError(
                    "Duplicate footprint reference or UUID; assign unique references before using the plugin."
                )
            refs.add(ref)
            uuids.add(uuid)
            parts.append(
                {
                    "reference": ref,
                    "footprint_uuid": uuid,
                    "value": fp.GetValue(),
                    "footprint": str(fp.GetFPID().GetLibItemName()),
                    "lcsc": get_lcsc_value(fp),
                    "exclude_from_bom": int(bool(get_exclude_from_bom(fp))),
                    "exclude_from_pos": int(bool(get_exclude_from_pos(fp))),
                    "is_dnp": int(get_is_dnp(fp)),
                    "pad_count": get_footprint_pad_count(fp),
                    "has_tht": footprint_has_tht(fp),
                }
            )
        return parts

    def read_all(self) -> list[dict[str, Any]]:
        """Initialize missing UUIDs once, then overlay their authoritative choices."""
        parts = self._board_parts()
        inserted = set()
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.row_factory = sqlite3.Row
            saved = {
                row["footprint_uuid"]: dict(row)
                for row in con.execute(
                    "SELECT * FROM part_info WHERE board_key = ?", (self.board_key,)
                )
            }
            for part in parts:
                uuid = part["footprint_uuid"]
                if uuid not in saved:
                    cursor = con.execute(
                        "INSERT INTO part_info VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(board_key, footprint_uuid) DO NOTHING",
                        (self.board_key, uuid, *(part[key] for key in _FIELDS)),
                    )
                    if cursor.rowcount:
                        inserted.add(uuid)
                    saved[uuid] = dict(
                        con.execute(
                            "SELECT * FROM part_info WHERE board_key = ? AND footprint_uuid = ?",
                            (self.board_key, uuid),
                        ).fetchone()
                    )
                part.update({key: saved[uuid][key] for key in _FIELDS})
        self.new_part_uuids.update(inserted)
        library = getattr(self.parent, "library", None)
        getter = getattr(library, "get_lcsc_metadata", None)
        metadata = (
            getter({part["lcsc"] for part in parts if part["lcsc"]})
            if callable(getter)
            else {}
        )
        for part in parts:
            cached = metadata.get(part["lcsc"], {})
            part.update(
                assembly_process=cached.get("assembly_process") or "",
                component_product_type=cached.get("component_product_type"),
                stock=None,
            )
            part["assembly_flags"] = json.dumps(
                {key: bool(part[key]) for key in _FIELDS[1:]}, sort_keys=True
            )
        natural_key = cmp_to_key(natural_sort_collation)
        return sorted(parts, key=lambda part: natural_key(part["reference"]))

    def get_part(self, ref: str) -> Optional[dict[str, Any]]:
        """Resolve an active reference without exposing deleted footprint records."""
        return next(
            (part for part in self.read_all() if part["reference"] == ref), None
        )

    def update_from_board(self) -> None:
        """Refresh current membership without ever overwriting existing choices."""
        self.read_all()

    def update_parts(
        self,
        changes: dict[str, dict[str, Any]],
        expected_uuids: Optional[dict[str, str]] = None,
    ) -> None:
        """Commit one complete UI action after validating its current identities."""
        if not changes:
            return
        current = {part["reference"]: part for part in self._board_parts()}
        updates = []
        for ref, fields in changes.items():
            if ref not in current or (
                expected_uuids is not None
                and expected_uuids.get(ref) != current[ref]["footprint_uuid"]
            ):
                raise sqlite3.IntegrityError(
                    "Selected footprints changed. Refresh the list and try again."
                )
            if not fields or not set(fields) <= set(_FIELDS):
                raise ValueError("Only plugin assembly choices can be edited.")
            for key, value in fields.items():
                if (key == "lcsc" and not isinstance(value, str)) or (
                    key != "lcsc" and value not in (0, 1)
                ):
                    raise ValueError("Invalid assembly choice.")
            updates.append((current[ref]["footprint_uuid"], fields))
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            for uuid, fields in updates:
                columns = ", ".join(f"{key} = ?" for key in fields)
                cursor = con.execute(
                    f"UPDATE part_info SET {columns} WHERE board_key = ? AND footprint_uuid = ?",
                    (*fields.values(), self.board_key, uuid),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "An assembly record is missing. Reopen the plugin."
                    )
        self.new_part_uuids.difference_update(uuid for uuid, _fields in updates)

    def set_lcsc_assignments(
        self,
        assignments: Iterable[tuple[str, str, Optional[int]]],
        expected_uuids: Optional[dict[str, str]] = None,
    ) -> None:
        """Persist assignments atomically; stock belongs to the supplier catalog."""
        self.update_parts(
            {ref: {"lcsc": lcsc} for ref, lcsc, _stock in assignments}, expected_uuids
        )

    def read_bom_parts(
        self,
        parts: Optional[Sequence[dict[str, Any]]] = None,
        *,
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        """Group assigned value/code pairs, excluding plugin BOM/DNP decisions."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        unassigned = []
        references: set[str] = set()
        for part in sorted(
            self.read_all() if parts is None else parts,
            key=lambda row: (row["lcsc"], row["reference"]),
        ):
            if (
                part["exclude_from_bom"]
                or part.get("is_dnp", False)
                or (not include_unassigned and not part["lcsc"])
            ):
                continue
            if part["reference"] in references:
                raise ValueError(
                    "Duplicate footprint reference. Assign unique references before generating files."
                )
            references.add(part["reference"])
            row = {
                "value": part["value"],
                "refs": part["reference"],
                "footprint": part["footprint"],
                "lcsc": part["lcsc"],
            }
            if not part["lcsc"]:
                unassigned.append(row)
                continue
            key = part["value"], part["lcsc"]
            if key in groups:
                groups[key]["refs"] += "," + part["reference"]
            else:
                groups[key] = row
        return [groups[key] for key in sorted(groups)] + unassigned

    def get_assembly_enrichment_targets(
        self, references: Optional[Iterable[str]] = None
    ) -> dict[str, list[str]]:
        """Group current assigned codes missing supplier classification."""
        selected = set(references) if references is not None else None
        targets: dict[str, list[str]] = {}
        valid_types = {member.value for member in ComponentProductType}
        for part in self.read_all():
            if (
                part["lcsc"]
                and (selected is None or part["reference"] in selected)
                and (
                    not part["assembly_process"]
                    or part["component_product_type"] not in valid_types
                )
            ):
                targets.setdefault(part["lcsc"], []).append(part["reference"])
        return targets

    def get_metadata(self, key: str) -> Optional[str]:
        """Read independent project counters and per-board notice acknowledgments."""
        self.board
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            row = con.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        """Persist project metadata without touching assembly records."""
        self.board
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_generation_count(self) -> int:
        """Return the existing per-project manufacturing counter."""
        try:
            return max(0, int(self.get_metadata(self.GENERATION_COUNT_KEY) or 0))
        except ValueError:
            return 0

    def increment_generation_count(self) -> int:
        """Serialize counter increments across concurrent plugin windows."""
        self.board
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT value FROM metadata WHERE key = ?", (self.GENERATION_COUNT_KEY,)
            ).fetchone()
            current = 0
            if row:
                with contextlib.suppress(ValueError, TypeError):
                    current = max(0, int(row[0]))
            value = current + 1
            con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.GENERATION_COUNT_KEY, str(value)),
            )
        return value
