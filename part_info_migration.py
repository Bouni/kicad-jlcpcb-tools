"""Isolated conversion of reference-keyed project assignments; removable later."""

from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional


def migrate_part_info(
    connection: sqlite3.Connection,
    schema: str,
    board_key: str,
    parts: list[dict[str, Any]],
    project_path: Path,
    confirm: Optional[Callable[[str, list[str]], bool]],
) -> None:
    """Replace legacy storage inside the caller's explicit transaction."""
    connection.execute("DROP TABLE IF EXISTS part_info_legacy")
    connection.execute("DELETE FROM metadata WHERE key = 'part_info_legacy_owner'")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(part_info)")}
    if not columns or "board_key" in columns:
        return
    if not {"reference", "value", "footprint", "lcsc"} <= columns:
        raise sqlite3.DatabaseError("Unrecognized legacy part_info schema.")
    cursor = connection.execute("SELECT * FROM part_info")
    names = [column[0] for column in cursor.description]
    legacy = [dict(zip(names, row)) for row in cursor]
    by_reference = {part["reference"]: part for part in parts}
    boards = sorted(path.name for path in project_path.glob("*.kicad_pcb"))
    reasons = []
    if legacy and (len(boards) != 1 or boards[0] != board_key):
        reasons.append(
            "Existing assignments do not identify their PCB. Use them for "
            + board_key
            + "? Boards in this directory: "
            + ", ".join(boards)
        )
    mismatches = [
        row["reference"]
        for row in legacy
        if row["reference"] in by_reference
        and any(
            row[key] != by_reference[row["reference"]][key]
            for key in ("value", "footprint")
        )
    ]
    if mismatches:
        reasons.append(
            "These references have changed value or footprint: "
            + ", ".join(mismatches)
            + ". Keep their existing plugin assignments?"
        )
    if reasons and (confirm is None or not confirm(board_key, reasons)):
        raise sqlite3.DatabaseError(
            "Assignment upgrade cancelled; existing data was preserved."
        )
    connection.execute("DROP TABLE part_info")
    connection.execute(schema)
    for row in legacy:
        part = by_reference.get(row["reference"])
        if part is None:
            continue
        try:
            flags = json.loads(row.get("assembly_flags") or "{}")
        except (TypeError, ValueError):
            flags = {}
        dnp = (
            flags.get("is_dnp", part["is_dnp"])
            if isinstance(flags, dict)
            else part["is_dnp"]
        )
        connection.execute(
            "INSERT INTO part_info VALUES (?, ?, ?, ?, ?, ?)",
            (
                board_key,
                part["footprint_uuid"],
                row["lcsc"] or "",
                _legacy_flag(row.get("exclude_from_bom", 0)),
                _legacy_flag(row.get("exclude_from_pos", 0)),
                _legacy_flag(dnp),
            ),
        )


def _legacy_flag(value: Any) -> int:
    """Preserve meaningful legacy booleans while rejecting corrupt decisions."""
    if value is None:
        return 0
    if value not in (0, 1, "0", "1"):
        raise sqlite3.DatabaseError(
            "An existing assembly flag is invalid; no data was changed."
        )
    return int(value)
