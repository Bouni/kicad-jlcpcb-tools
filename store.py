"""Read ordinary PCB parts and retain independent generation metadata."""

from collections.abc import Iterable, Sequence
import contextlib
from functools import cmp_to_key
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

from .bom_estimation.assembly_mode import classify_component_product_type
from .footprint_helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_is_dnp,
    get_lcsc_assignment,
    get_valid_footprints,
)
from .footprint_metadata import get_assembly_flags, get_footprint_pad_metadata
from .helpers import natural_sort_collation
from .lcsc import normalize_lcsc
from .variant.generation_counter import generation_count_transaction


class Store:
    """Adapt live board fields, with supplier facts cached by LCSC for this session.

    Legacy assignment tables and CSVs are neither read nor modified. Project
    SQLite storage is used only when the generation counter is requested.
    """

    GENERATION_COUNT_KEY = "generation_count"

    def __init__(self, parent: Any, project_path: str, board: Any) -> None:
        self.parent = parent
        self.project_path = project_path
        self._board = board
        self.datadir = os.path.join(project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self._assembly_metadata: dict[str, dict[str, Any]] = {}

    @property
    def board(self) -> Any:
        """Retrieve a validated live board, or the independently owned board."""
        getter = getattr(self.parent, "_get_current_board", None)
        return getter() if callable(getter) else self._board

    def get_generation_count(self) -> int:
        """Read the existing project counter without creating or upgrading files."""
        path = Path(self.dbfile)
        if not path.is_file():
            return 0
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone():
                return 0
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (self.GENERATION_COUNT_KEY,),
            ).fetchone()
        if row is not None:
            with contextlib.suppress(ValueError, TypeError):
                return max(0, int(row[0]))
        return 0

    def increment_generation_count(self) -> int:
        """Persist only the existing directory-wide generation counter."""
        with generation_count_transaction(self.dbfile) as count:
            pass
        return count

    def _part_row(self, footprint: Any) -> dict[str, Any]:
        """Capture one footprint's current native data and matching supplier facts."""
        assignment, lcsc = get_lcsc_assignment(footprint)
        metadata = self._assembly_metadata.get(lcsc, {})
        pad_count, has_tht = get_footprint_pad_metadata(footprint)
        return {
            "reference": footprint.GetReference(),
            "value": footprint.GetValue(),
            "footprint": str(footprint.GetFPID().GetLibItemName()),
            "lcsc": lcsc,
            "assignment_status": assignment.status,
            "stock": None,
            "exclude_from_bom": get_exclude_from_bom(footprint),
            "exclude_from_pos": get_exclude_from_pos(footprint),
            "is_dnp": get_is_dnp(footprint),
            "pad_count": pad_count,
            "has_tht": has_tht,
            "assembly_flags": get_assembly_flags(footprint),
            "assembly_process": metadata.get("assembly_process", ""),
            "component_product_type": metadata.get("component_product_type"),
        }

    def read_all(self) -> list[dict[str, Any]]:
        """Capture current native assignments, population and electrical metadata."""
        rows = [self._part_row(fp) for fp in get_valid_footprints(self.board)]
        natural_key = cmp_to_key(natural_sort_collation)
        return sorted(rows, key=lambda row: natural_key(row["reference"]))

    def get_part(self, ref: str) -> Optional[dict[str, Any]]:
        """Return the current native row for a reference, if it still exists."""
        # Preserve the same reference eligibility as get_valid_footprints without
        # crossing the native boundary for every unrelated footprint and pad.
        if not re.match(r"[\w\d-]+", ref):
            return None
        board = self.board
        finder = getattr(board, "FindFootprintByReference", None)
        if callable(finder):
            footprint = finder(ref)
        else:
            footprint = next(
                (fp for fp in get_valid_footprints(board) if fp.GetReference() == ref),
                None,
            )
        return self._part_row(footprint) if footprint is not None else None

    def read_bom_parts(
        self,
        parts: Optional[Sequence[dict[str, Any]]] = None,
        *,
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        """Group populated native value/LCSC pairs, retaining individual blank IDs."""
        source = self.read_all() if parts is None else parts
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        unassigned = []
        references: set[str] = set()
        for part in sorted(source, key=lambda row: (row["lcsc"], row["reference"])):
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
            row = {key: part[key] for key in ("value", "footprint", "lcsc")}
            row["refs"] = part["reference"]
            if not part["lcsc"]:
                unassigned.append(row)
                continue
            key = part["value"], part["lcsc"]
            if key in groups:
                groups[key]["refs"] += "," + part["reference"]
            else:
                groups[key] = row
        return [groups[key] for key in sorted(groups)] + unassigned

    def cache_lcsc_metadata(
        self,
        lcsc: str,
        assembly_process: Optional[str],
        component_product_type: object,
    ) -> None:
        """Merge supplier facts independently of any current footprint assignment."""
        code = normalize_lcsc(lcsc)
        if not code:
            return
        metadata = self._assembly_metadata.setdefault(code, {})
        if assembly_process:
            metadata["assembly_process"] = assembly_process
        classification = classify_component_product_type(component_product_type)
        if classification is not None:
            metadata["component_product_type"] = int(classification)

    def set_assembly_metadata(
        self,
        ref: str,
        assembly_process: Optional[str],
        component_product_type: object,
        expected_lcsc: Optional[str] = None,
    ) -> bool:
        """Merge facts only when the reference still carries the requested LCSC."""
        part = self.get_part(ref)
        if not part or not part["lcsc"]:
            return False
        if expected_lcsc is not None and part["lcsc"] != normalize_lcsc(expected_lcsc):
            return False
        self.cache_lcsc_metadata(part["lcsc"], assembly_process, component_product_type)
        return True

    def get_assembly_enrichment_targets(
        self, references: Optional[Iterable[str]] = None
    ) -> dict[str, list[str]]:
        """Group current assignments missing either required supplier fact."""
        selected = set(references) if references is not None else None
        targets: dict[str, list[str]] = {}
        for part in sorted(
            self.read_all(), key=lambda row: (row["lcsc"], row["reference"])
        ):
            if not part["lcsc"] or (
                selected is not None and part["reference"] not in selected
            ):
                continue
            if not part["assembly_process"] or part["component_product_type"] is None:
                targets.setdefault(part["lcsc"], []).append(part["reference"])
        return targets
