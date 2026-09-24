"""Capture live Default assignments with explicit clear and preserve decisions."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional

from .footprint_helpers import EXCLUDE_FROM_BOM, _iter_assignment_fields
from .lcsc import is_lcsc_part, normalize_lcsc
from .part_assignments import (
    ResolvedAssignment,
    is_assignment_alias,
    resolve_assignment,
)


def _reference(value: Any) -> str:
    """Reject identities that cannot safely match a schematic reference."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Default schematic export requires a nonempty reference")
    return value


@dataclass(frozen=True)
class DefaultSchematicSnapshot:
    """Validated export decisions; empty text means a proven present-field clear.

    Factories retain unknown assignments as None. Constructing this type directly
    asserts the same provenance contract: a blank is an intentional clear, never
    a missing or unvalidated database value.
    """

    assignments: Mapping[str, Optional[str]]
    bom_parts: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate source inventory and detach every mapping from mutable inputs."""
        assignments = dict(self.assignments)
        for reference, value in assignments.items():
            _reference(reference)
            if value is not None and (
                not isinstance(value, str)
                or (
                    value
                    and (not is_lcsc_part(value) or value != normalize_lcsc(value))
                )
            ):
                raise ValueError(
                    f"Invalid Default schematic assignment for {reference}"
                )
        parts = []
        references: set[str] = set()
        for part in self.bom_parts:
            reference = _reference(part["reference"])
            if reference in references:
                raise ValueError(f"Duplicate Default schematic reference: {reference}")
            references.add(reference)
            excluded = part["exclude_from_bom"]
            if type(excluded) is not bool:
                raise ValueError(f"Invalid Default BOM state for {reference}")
            parts.append(
                MappingProxyType({"reference": reference, "exclude_from_bom": excluded})
            )
        if references != set(assignments):
            raise ValueError("Default assignment and BOM references must match")
        object.__setattr__(self, "assignments", MappingProxyType(assignments))
        object.__setattr__(self, "bom_parts", tuple(parts))
        object.__setattr__(self, "warnings", tuple(self.warnings))


def _assignment_change(
    assignment: ResolvedAssignment, lcsc: str
) -> tuple[Optional[str], str]:
    """Allow only valid assignments or recognized fields that are all raw-empty."""
    status, aliases = assignment.status, assignment.aliases
    if status not in {"missing", "empty", "valid", "invalid", "conflict"}:
        raise ValueError(f"Unknown Default assignment status: {status}")
    if not isinstance(lcsc, str):
        raise TypeError("Default LCSC assignment must be text")
    for alias in aliases:
        if not isinstance(alias.name, str) or not isinstance(alias.text, str):
            raise TypeError("Default assignment field names and values must be text")
        if not is_assignment_alias(alias.name):
            raise ValueError(f"Unrecognized Default assignment field: {alias.name}")
    if any(alias.text and not alias.text.strip() for alias in aliases):
        return None, "whitespace in an assignment field"
    if not aliases:
        return None, "missing assignment field"
    if status == "empty" and all(alias.text == "" for alias in aliases):
        return "", ""
    if status == "valid" and is_lcsc_part(lcsc):
        normalized = normalize_lcsc(lcsc)
        if {normalize_lcsc(alias.text) for alias in aliases if alias.text} == {
            normalized
        }:
            return normalized, ""
    return None, f"{status} assignment"


def _capture(
    records: Iterable[tuple[str, ResolvedAssignment, str, bool]],
) -> DefaultSchematicSnapshot:
    """Retain every component while separating assignment skips from BOM state."""
    assignments: dict[str, Optional[str]] = {}
    parts = []
    warnings = []
    for reference, assignment, lcsc, excluded in records:
        reference = _reference(reference)
        if reference in assignments:
            raise ValueError(f"Duplicate Default schematic reference: {reference}")
        value, reason = _assignment_change(assignment, lcsc)
        assignments[reference] = value
        parts.append({"reference": reference, "exclude_from_bom": excluded})
        if reason:
            warnings.append(
                f"{reference}: {reason}; schematic assignment preserved. "
                "Assign or clear the PCB assignment fields before exporting."
            )
    return DefaultSchematicSnapshot(assignments, tuple(parts), tuple(warnings))


def capture_board(board: Any) -> DefaultSchematicSnapshot:
    """Capture ordinary-mode base fields and flags without consulting SQLite."""

    def records() -> Iterable[tuple[str, ResolvedAssignment, str, bool]]:
        for footprint in board.GetFootprints():
            reference = _reference(footprint.GetReference())
            fields: dict[str, str] = {}
            for name, text in _iter_assignment_fields(footprint):
                if not isinstance(name, str) or not isinstance(text, str):
                    raise TypeError(f"Invalid PCB field on {reference}")
                if name in fields:
                    raise ValueError(f"Duplicate PCB field {name!r} on {reference}")
                fields[name] = text
            assignment, lcsc = resolve_assignment(fields, {}, "")
            excluded = bool(footprint.GetAttributes() & (1 << EXCLUDE_FROM_BOM))
            yield reference, assignment, lcsc, excluded

    try:
        return _capture(records())
    except Exception as error:
        raise ValueError(
            f"Unable to capture Default PCB assignments: {error}"
        ) from error


def capture_native(snapshot: Any) -> DefaultSchematicSnapshot:
    """Retain Default native provenance without converting through assembly rows."""

    def records() -> Iterable[tuple[str, ResolvedAssignment, str, bool]]:
        for state in snapshot.for_variant(""):
            if state.variant_name != "":
                raise ValueError("Schematic export requires Default component states")
            if type(state.bom) is not bool:
                raise ValueError(f"Invalid Default BOM state for {state.reference}")
            yield state.reference, state.assignment, state.lcsc, not state.bom

    try:
        return _capture(records())
    except Exception as error:
        raise ValueError(
            f"Unable to capture Default native assignments: {error}"
        ) from error
