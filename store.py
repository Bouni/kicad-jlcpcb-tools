"""Read ordinary PCB parts and retain independent generation metadata."""

from collections.abc import Iterable, Sequence
import contextlib
from functools import cmp_to_key
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional

from .board_parts import find_component, is_listed, part_facts
from .enrichment.cache import AssemblyMetadataCache
from .helpers import natural_sort_collation
from .kicad_backend.records import ComponentRecord
from .lcsc import normalize_lcsc
from .variant.generation_counter import generation_count_transaction


class Store:
    """Combine the board's parts with supplier facts cached by LCSC.

    The parts come from the window's board snapshots: ``parent._read_board()``
    reads the board again and ``parent._held_board()`` returns the snapshot
    the window last read, after checking the board is still the one it opened.

    This adapter does not read legacy assignments. The window performs guarded
    startup recovery and archival separately. This adapter uses project SQLite
    storage only for the generation counter and notice metadata. Supplier facts
    come from the cache the window supplies, which saves them in global.db; the
    default cache keeps them only as long as this adapter.
    """

    GENERATION_COUNT_KEY = "generation_count"
    SCHEMATIC_STORAGE_NOTICE_ACKED_KEY = "schematic_storage_notice_acked"

    def __init__(
        self,
        parent: Any,
        project_path: str,
        *,
        assembly_metadata: Optional[AssemblyMetadataCache] = None,
    ) -> None:
        self.parent = parent
        self.project_path = project_path
        self.datadir = os.path.join(project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self._assembly_metadata = (
            assembly_metadata
            if assembly_metadata is not None
            else AssemblyMetadataCache(None)
        )

    @property
    def assembly_metadata(self) -> AssemblyMetadataCache:
        """Return the cache supplier facts are read and recorded through."""
        return self._assembly_metadata

    @assembly_metadata.setter
    def assembly_metadata(self, cache: AssemblyMetadataCache) -> None:
        """Use another cache, as when Settings moves the data folder."""
        self._assembly_metadata = cache

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

    def _part_row(self, component: ComponentRecord) -> dict[str, Any]:
        """Combine one footprint's board facts with its supplier facts."""
        facts = part_facts(component)
        assembly_process, component_product_type = self._assembly_metadata.get(
            facts["lcsc"]
        )
        return {
            **facts,
            "stock": None,
            "assembly_process": assembly_process,
            "component_product_type": component_product_type,
        }

    def read_all(self) -> list[dict[str, Any]]:
        """Read the board again: assignments, population and pad facts by reference."""
        snapshot = self.parent._read_board()
        rows = [
            self._part_row(component)
            for component in snapshot.components
            if is_listed(component.reference)
        ]
        natural_key = cmp_to_key(natural_sort_collation)
        return sorted(rows, key=lambda row: natural_key(row["reference"]))

    def get_part(self, ref: str) -> Optional[dict[str, Any]]:
        """Return the row for a reference from the window's last board read.

        One part is looked up without reading the whole board again; the
        window reads it again on activation, after each edit and with each
        ``read_all``.
        """
        if not is_listed(ref):
            return None
        component = find_component(self.parent._held_board(), ref)
        return self._part_row(component) if component is not None else None

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

    def is_schematic_storage_notice_acked(self) -> bool:
        """Read the notice acknowledgment without creating or upgrading storage."""
        path = Path(self.dbfile)
        if not path.is_file():
            return False
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone():
                return False
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (self.SCHEMATIC_STORAGE_NOTICE_ACKED_KEY,),
            ).fetchone()
        return bool(row and str(row[0]) == "1")

    def set_schematic_storage_notice_acked(self) -> None:
        """Persist notice acknowledgment using only the project metadata table."""
        Path(self.datadir).mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.dbfile)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.SCHEMATIC_STORAGE_NOTICE_ACKED_KEY, "1"),
            )

    def cache_lcsc_metadata(
        self,
        lcsc: str,
        assembly_process: Optional[str],
        component_product_type: object,
    ) -> None:
        """Merge supplier facts independently of any current footprint assignment."""
        self._assembly_metadata.merge(lcsc, assembly_process, component_product_type)

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
        """Group current assignments missing a supplier fact or due a refresh."""
        selected = set(references) if references is not None else None
        targets: dict[str, list[str]] = {}
        for part in sorted(
            self.read_all(), key=lambda row: (row["lcsc"], row["reference"])
        ):
            if not part["lcsc"] or (
                selected is not None and part["reference"] not in selected
            ):
                continue
            if self._assembly_metadata.needs_fetch(part["lcsc"]):
                targets.setdefault(part["lcsc"], []).append(part["reference"])
        return targets
