"""Read current board parts and persist project generation metadata."""

from collections.abc import Iterable, Sequence
import contextlib
from functools import cmp_to_key
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional

from .bom_estimation.assembly_mode import ComponentProductType
from .footprint_helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_is_dnp,
    get_lcsc_value,
    get_valid_footprints,
)
from .footprint_metadata import (
    footprint_has_tht,
    get_assembly_flags,
    get_footprint_pad_count,
)
from .helpers import natural_sort_collation


class Store:
    """Expose board-derived parts and the independent project generation counter."""

    GENERATION_COUNT_KEY = "generation_count"

    def __init__(self, parent: Any, project_path: str, board: Any) -> None:
        self.parent = parent
        self.project_path = project_path
        self._board = board
        self.datadir = os.path.join(self.project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")

    @property
    def board(self) -> Any:
        """Retrieve the validated live board, retaining direct standalone ownership."""
        getter = getattr(self.parent, "_get_current_board", None)
        return getter() if callable(getter) else self._board

    def create_db(self) -> None:
        """Initialize generation metadata only when project persistence is needed."""
        Path(self.datadir).mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
            )

    def get_generation_count(self) -> int:
        """Return the per-project generation counter."""
        self.create_db()
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con:
            row = con.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (self.GENERATION_COUNT_KEY,),
            ).fetchone()
        if row:
            with contextlib.suppress(ValueError, TypeError):
                return max(0, int(row[0]))
        return 0

    def increment_generation_count(self) -> int:
        """Increment and persist the per-project generation counter."""
        self.create_db()
        with contextlib.closing(sqlite3.connect(self.dbfile)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (self.GENERATION_COUNT_KEY,),
            ).fetchone()
            current = 0
            if row:
                with contextlib.suppress(ValueError, TypeError):
                    current = max(0, int(row[0]))
            next_count = current + 1
            con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.GENERATION_COUNT_KEY, str(next_count)),
            )
        return next_count

    def read_all(self) -> list[dict[str, Any]]:
        """Read current board fields and join supplier metadata by assigned code."""
        parts = [
            {
                "reference": fp.GetReference(),
                "value": fp.GetValue(),
                "footprint": str(fp.GetFPID().GetLibItemName()),
                "lcsc": get_lcsc_value(fp),
                "stock": None,
                "exclude_from_bom": get_exclude_from_bom(fp),
                "exclude_from_pos": get_exclude_from_pos(fp),
                "is_dnp": get_is_dnp(fp),
                "pad_count": get_footprint_pad_count(fp),
                "has_tht": footprint_has_tht(fp),
                "assembly_flags": get_assembly_flags(fp),
            }
            for fp in get_valid_footprints(self.board)
        ]
        library = getattr(self.parent, "library", None)
        get_metadata = getattr(library, "get_lcsc_metadata", None)
        codes = {part["lcsc"] for part in parts if part["lcsc"]}
        metadata = get_metadata(codes) if callable(get_metadata) else {}
        for part in parts:
            cached = metadata.get(part["lcsc"], {})
            part["assembly_process"] = cached.get("assembly_process") or ""
            part["component_product_type"] = cached.get("component_product_type")
        natural_key = cmp_to_key(natural_sort_collation)
        return sorted(parts, key=lambda part: natural_key(part["reference"]))

    def read_bom_parts(
        self,
        parts: Optional[Sequence[dict[str, Any]]] = None,
        *,
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        """Group assigned value/code pairs and keep unassigned footprints separate."""
        rows = self.read_all() if parts is None else parts
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        unassigned = []
        references: set[str] = set()
        # Keep the historical lexical reference order within grouped BOM rows.
        for part in sorted(rows, key=lambda row: (row["lcsc"], row["reference"])):
            if (
                part["exclude_from_bom"]
                or part.get("is_dnp", False)
                or (not include_unassigned and not part["lcsc"])
            ):
                continue
            if part["reference"] in references:
                raise ValueError(
                    f"Duplicate footprint reference {part['reference']}. "
                    "Assign unique references before generating fabrication files."
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
        """Group current assignments whose supplier metadata is incomplete."""
        selected = set(references) if references is not None else None
        valid_types = {member.value for member in ComponentProductType}
        targets: dict[str, list[str]] = {}
        rows = sorted(
            self.read_all(), key=lambda part: (part["lcsc"], part["reference"])
        )
        for part in rows:
            if not part["lcsc"] or (
                selected is not None and part["reference"] not in selected
            ):
                continue
            if (
                not part["assembly_process"]
                or part["component_product_type"] not in valid_types
            ):
                targets.setdefault(part["lcsc"], []).append(part["reference"])
        return targets
