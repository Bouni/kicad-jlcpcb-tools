"""Capture immutable native Default assignments and schematic links once."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .board_parts import default_assignment
from .kicad_backend.records import BoardSnapshot
from .lcsc import is_lcsc_part
from .part_assignments import (
    AssignmentAlias,
    ResolvedAssignment,
    resolve_assignment,
    safe_assignment_value,
)


@dataclass(frozen=True)
class FootprintAssignment:
    """One PCB identity, its schematic path, and Default assignment provenance."""

    component_id: str
    reference: str
    schematic_path: str
    assignment: ResolvedAssignment
    lcsc: str
    exclude_from_bom: bool

    @property
    def value(self) -> Optional[str]:
        """Return a proven assignment or clear; None preserves schematic fields."""
        return safe_assignment_value(self.assignment, self.lcsc)

    @property
    def label(self) -> str:
        """Retain the UUID so duplicate or blank references stay distinguishable."""
        return f"{self.reference or '(unannotated)'} [{self.component_id}]"


@dataclass(frozen=True)
class DefaultSchematicSnapshot:
    """Detached source records; duplicate references are harmless labels."""

    parts: tuple[FootprintAssignment, ...]

    def __post_init__(self) -> None:
        """Reject duplicate native identities without rejecting duplicate labels."""
        parts = tuple(self.parts)
        identities: set[str] = set()
        for part in parts:
            if not part.component_id or part.component_id in identities:
                raise ValueError("Default snapshot requires unique footprint UUIDs")
            if type(part.exclude_from_bom) is not bool:
                raise ValueError("Default snapshot requires boolean BOM states")
            identities.add(part.component_id)
        object.__setattr__(self, "parts", parts)


def capture_board(snapshot: BoardSnapshot) -> DefaultSchematicSnapshot:
    """Take each footprint's Default assignment, UUID, link and BOM flag.

    A footprint holding two fields of one name is refused: which of them
    the schematic should receive is ambiguous.
    """
    parts = []
    for component in snapshot.components:
        repeated = component.default.duplicate_names
        if repeated:
            raise ValueError(f"Duplicate Default PCB field {repeated[0]!r}")
        parts.append(
            FootprintAssignment(
                component.uuid,
                component.reference,
                component.schematic_path,
                *default_assignment(component),
                component.default.exclude_from_bom,
            )
        )
    return DefaultSchematicSnapshot(tuple(parts))


capture_default_snapshot = capture_board


def capture_rows(parts: Iterable[Mapping[str, Any]]) -> DefaultSchematicSnapshot:
    """Validate explicitly supplied Default records with identity and provenance.

    This adapter is for callers that already captured native records. Ordinary
    and matrix windows use capture_board directly and never export store rows.
    """
    records = []
    for part in parts:
        required = {
            "component_id",
            "schematic_path",
            "reference",
            "lcsc",
            "exclude_from_bom",
            "assignment_status",
        }
        if not required.issubset(part) or part.get("variant_name", "") != "":
            raise ValueError(
                "Default export requires UUID, path and assignment provenance"
            )
        status, lcsc = part["assignment_status"], part["lcsc"]
        if status not in {"valid", "empty", "missing", "invalid", "conflict"}:
            raise ValueError("Unknown Default assignment status")
        if not all(
            isinstance(part[name], str)
            for name in ("component_id", "schematic_path", "reference", "lcsc")
        ):
            raise ValueError("Default identity and assignment values must be text")
        if "fields" in part:
            assignment, resolved = resolve_assignment(dict(part["fields"]), {}, "")
            if (assignment.status, resolved) != (status, lcsc):
                raise ValueError("Default assignment provenance disagrees with fields")
        else:
            aliases = (
                () if status == "missing" else (AssignmentAlias("LCSC", lcsc, False),)
            )
            assignment = ResolvedAssignment(status, False, aliases)
        if status == "valid" and not is_lcsc_part(lcsc):
            raise ValueError("Default valid assignments require an LCSC identifier")
        records.append(
            FootprintAssignment(
                part["component_id"],
                part["reference"],
                part["schematic_path"],
                assignment,
                lcsc,
                part["exclude_from_bom"],
            )
        )
    return DefaultSchematicSnapshot(tuple(records))
