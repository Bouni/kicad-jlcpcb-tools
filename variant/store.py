"""Supplier facts, explicit assembly rows and existing plugin preferences."""

from collections.abc import Iterator
import contextlib
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from ..bom_estimation.assembly_mode import ComponentProductType
from ..core.settings_persistence import (
    VariantPreferencePatch,
    changed_variant_preferences,
)
from .generation_counter import generation_count_transaction, publication_guard

if TYPE_CHECKING:
    from .native import BoardVariantSnapshot


@dataclass(frozen=True)
class _AssemblyMetadata:
    """Supplier facts retained only for the current plugin dialog lifetime."""

    assembly_process: str = ""
    component_product_type: Optional[int] = None


class VariantSnapshotError(ValueError):
    """An operation cannot be tied to a complete current native snapshot."""


class VariantStore:
    """Own supplier facts and plugin preferences without caching native state.

    Session snapshots are passed explicitly when adapting rows. SQLite is used
    only for the existing generation counter.
    Supplier facts are shared by LCSC and refetched after reopening.
    """

    GENERATION_COUNT_KEY = "generation_count"
    OUTPUT_VARIANT_KEY = "output_variant"
    DISPLAY_PREFERENCES_KEY = "display_preferences"

    def __init__(
        self,
        parent: Any,
        project_path: str,
        board: Any,
    ) -> None:
        filename = str(board.GetFileName())
        if not filename.strip():
            raise VariantSnapshotError("Save the PCB before opening the JLCPCB plugin.")
        self.board_path = Path(filename).resolve()
        if (
            self.board_path.suffix.lower() != ".kicad_pcb"
            or not self.board_path.is_file()
        ):
            raise VariantSnapshotError("Save the PCB before opening the JLCPCB plugin.")
        self.parent = parent
        self.project_path = project_path
        self.board = board
        self.datadir = os.path.join(project_path, "jlcpcb")
        self.dbfile = os.path.join(self.datadir, "project.db")
        self.board_id = str(uuid4())
        self._assembly_metadata: dict[str, _AssemblyMetadata] = {}

    def ensure_current_board(self) -> None:
        """Require reopening after Save As without persisting a board registry."""
        filename = str(self.board.GetFileName())
        if not filename or Path(filename).resolve() != self.board_path:
            raise VariantSnapshotError(
                "The PCB filename changed; reopen the JLCPCB plugin."
            )

    @contextlib.contextmanager
    def _read_database(self) -> Iterator[Optional[sqlite3.Connection]]:
        """Read an existing database without creating or upgrading any tables."""
        self.ensure_current_board()
        path = Path(self.dbfile)
        if not path.is_file():
            yield None
            return
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection

    def get_generation_count(self) -> int:
        """Read ordinary mode's existing directory-wide counter without creation."""
        with self._read_database() as connection:
            if (
                connection is None
                or not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
                ).fetchone()
            ):
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
        """Increment the existing counter using only main's metadata schema/key."""
        with self.generation_counter_transaction() as next_count:
            pass
        return next_count

    @contextlib.contextmanager
    def generation_publication_lock(self) -> Iterator[None]:
        """Serialize publication until every failed export has restored its files."""
        self.ensure_current_board()
        with publication_guard(self.dbfile):
            yield

    @contextlib.contextmanager
    def generation_counter_transaction(
        self, expected_count: Optional[int] = None
    ) -> Iterator[int]:
        """Reserve the counter only while prepared artifact files are published."""
        self.ensure_current_board()
        with generation_count_transaction(self.dbfile, expected_count) as count:
            yield count

    @staticmethod
    def _reference_key(reference: str) -> tuple[Any, ...]:
        """Keep the existing natural reference order without a GUI dependency."""
        return tuple(
            int(item) if item.isdigit() else item.lower()
            for item in re.split("([0-9]+)", reference)
        )

    def assembly_rows(
        self, snapshot: "BoardVariantSnapshot", variant_name: str
    ) -> list[dict[str, Any]]:
        """Join captured native parts to supplier facts for estimates and details."""
        self.ensure_current_board()
        if snapshot.board_id != self.board_id:
            raise VariantSnapshotError("The variant snapshot belongs to another PCB.")
        rows = []
        for part in sorted(
            snapshot.for_variant(variant_name),
            key=lambda part: self._reference_key(part.reference),
        ):
            metadata = self._assembly_metadata.get(part.lcsc, _AssemblyMetadata())
            rows.append(
                {
                    "reference": part.reference,
                    "value": part.value,
                    "footprint": part.footprint,
                    "lcsc": part.lcsc,
                    "assignment_status": part.assignment.status,
                    "variant_name": part.variant_name,
                    "pad_count": part.pad_count,
                    "has_tht": part.has_tht,
                    "assembly_process": metadata.assembly_process,
                    "component_product_type": metadata.component_product_type,
                    "exclude_from_bom": not part.bom,
                    "exclude_from_pos": not part.pos,
                    "is_dnp": not part.pop,
                }
            )
        return rows

    def get_missing_metadata(self, snapshot: "BoardVariantSnapshot") -> set[str]:
        """Return unique assigned supplier IDs still missing assembly facts."""
        valid_types = {item.value for item in ComponentProductType}
        return {
            part.lcsc
            for part in snapshot.components
            if part.lcsc
            and (
                not (facts := self._assembly_metadata.get(part.lcsc))
                or not facts.assembly_process
                or facts.component_product_type not in valid_types
            )
        }

    def set_assembly_metadata(
        self,
        lcsc: str,
        assembly_process: Optional[str],
        component_product_type: Optional[int],
    ) -> None:
        """Merge supplier facts by the requested identifier, independent of edits."""
        existing = self._assembly_metadata.get(lcsc, _AssemblyMetadata())
        self._assembly_metadata[lcsc] = _AssemblyMetadata(
            assembly_process or existing.assembly_process,
            component_product_type
            if component_product_type is not None
            else existing.component_product_type,
        )

    def _preferences(self) -> dict[str, Any]:
        """Read the current board's UI choices from the existing settings document."""
        self.ensure_current_board()
        variants = self.parent.settings.get("variants", {})
        if not isinstance(variants, dict):
            raise VariantSnapshotError("Stored variant settings are invalid.")
        boards = variants.get("boards", {})
        if not isinstance(boards, dict):
            raise VariantSnapshotError("Stored board display preferences are invalid.")
        preferences = boards.get(str(self.board_path), {})
        if not isinstance(preferences, dict):
            raise VariantSnapshotError("Stored board display preferences are invalid.")
        return preferences

    def _set_preference(self, key: str, value: Any) -> None:
        """Save one explicit board choice against the latest persisted settings."""
        self.ensure_current_board()
        patch = VariantPreferencePatch({("boards", str(self.board_path), key): value})
        self.parent.save_settings(variant_patch=patch)

    def get_output_variant(self) -> Optional[str]:
        """Retain canonical names, including a previously selected removed variant."""
        value = self._preferences().get(self.OUTPUT_VARIANT_KEY)
        if value is not None and not isinstance(value, str):
            raise VariantSnapshotError("Stored output variant is invalid.")
        return value

    def set_output_variant(self, variant_name: str) -> None:
        """Remember a valid output choice independently of matrix focus."""
        self._set_preference(self.OUTPUT_VARIANT_KEY, variant_name)

    def get_display_preferences(self) -> dict[str, Any]:
        """Return an independent copy of the current board's matrix settings."""
        value = self._preferences().get(self.DISPLAY_PREFERENCES_KEY, {})
        if not isinstance(value, dict):
            raise VariantSnapshotError("Stored matrix display preferences are invalid.")
        return deepcopy(value)

    def set_display_preferences(
        self,
        preferences: dict[str, Any],
        *,
        before: Optional[dict[str, Any]] = None,
    ) -> None:
        """Save only changed display fields, preserving other windows' updates."""
        self.ensure_current_board()
        if not isinstance(preferences, dict):
            raise TypeError("Matrix display preferences must be a dictionary.")
        value = json.loads(json.dumps(preferences, ensure_ascii=False, allow_nan=False))
        patch = changed_variant_preferences(
            self.get_display_preferences() if before is None else before,
            value,
            ("boards", str(self.board_path), self.DISPLAY_PREFERENCES_KEY),
        )
        if patch.updates or patch.removals:
            self.parent.save_settings(variant_patch=patch)
