"""Plan conservative recovery from the active ordinary-mode assignment cache.

Planning never writes board fields or SQLite data and never reads archived
mappings. The caller applies the complete assignment batch through the native
board transaction before initial preferences and BOM display. Local row coverage
is separate from proving that chosen values are saved and that other boards in
the same directory no longer need active recovery data.
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from typing import Any, Optional, Union

from .lcsc import is_lcsc_part, normalize_lcsc
from .legacy_part_storage import legacy_part_info_digest, legacy_part_info_generation
from .part_assignments import ResolvedAssignment, safe_assignment_value


@dataclass(frozen=True)
class MigrationLink:
    """Distinguish authenticated schematic members from proven or unsafe absence."""

    status: str
    members: tuple[tuple[ResolvedAssignment, str], ...] = ()
    target_ids: tuple[tuple[str, str], ...] = ()
    reason: str = ""
    covered_occurrences: tuple[tuple[str, str, str], ...] = ()
    expected_occurrences: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        """Reject contradictory evidence instead of guessing an absence policy."""
        if self.status not in {"linked", "pcb_only", "no_schematic", "unresolved"}:
            raise ValueError("Unknown legacy schematic link status")
        if self.status in {"pcb_only", "no_schematic"} and (
            self.members
            or self.target_ids
            or self.covered_occurrences
            or self.expected_occurrences
        ):
            raise ValueError("Schematic absence cannot contain linked members")


SchematicLookup = Callable[
    [str], Optional[Union[MigrationLink, tuple[ResolvedAssignment, str]]]
]
TargetIdentity = Callable[[str], Optional[tuple[str, str]]]


def _as_link(
    value: Optional[Union[MigrationLink, tuple[ResolvedAssignment, str]]],
) -> MigrationLink:
    """Adapt older single-symbol callbacks without treating None as safe absence."""
    if isinstance(value, MigrationLink):
        return value
    if value is None:
        return MigrationLink(
            "unresolved", reason="linked schematic symbol is unresolved"
        )
    return MigrationLink("linked", (value,))


@dataclass(frozen=True)
class MigrationFootprint:
    """Immutable Default board values needed for exact historical row matching."""

    component_id: str
    reference: str
    value: str
    footprint: str
    bom: bool
    pos: bool
    assignment: ResolvedAssignment
    lcsc: str


@dataclass(frozen=True)
class LegacyRowCoverage:
    """Explain a historical row's recovery decision and affected native targets."""

    reference: str
    lcsc: str
    status: str
    component_ids: tuple[str, ...]
    reason: str
    disposition: str = ""
    native_value: Optional[str] = None
    identity: tuple[Any, ...] = ()


@dataclass(frozen=True)
class LegacyMigrationPlan:
    """One immutable read-only recovery plan and its table-retirement boundary."""

    active_table: bool
    assignments: tuple[tuple[str, str], ...] = ()
    rows: tuple[LegacyRowCoverage, ...] = ()
    active_digest: Optional[str] = None
    active_generation: Optional[str] = None

    @property
    def unresolved(self) -> tuple[LegacyRowCoverage, ...]:
        """Return every recoverable row that must retain its active recovery data."""
        return tuple(row for row in self.rows if row.status == "unresolved")

    @property
    def retirement_eligible(self) -> bool:
        """Report local coverage; saved-board ownership and durability are separate."""
        return not self.unresolved

    @property
    def blocked_component_ids(self) -> tuple[str, ...]:
        """Identify unsafe candidates that preference fallback must not replace."""
        return tuple(
            sorted({key for row in self.unresolved for key in row.component_ids})
        )


def _read_active_rows(dbfile: str) -> Optional[tuple[list[tuple[Any, ...]], str, str]]:
    """Read the active historical schema without creating or upgrading storage."""
    path = Path(dbfile)
    if not path.exists():
        return None
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='part_info' COLLATE NOCASE"
            ).fetchone()
            is None
        ):
            return None
        rows = connection.execute(
            "SELECT reference, value, footprint, lcsc, "
            "exclude_from_bom, exclude_from_pos FROM part_info"
        ).fetchall()
        digest = legacy_part_info_digest(connection)
        generation = legacy_part_info_generation(connection)
        if digest is None or generation is None:
            raise sqlite3.DatabaseError(
                "Active legacy table disappeared during recovery"
            )
        return rows, digest, generation


def _matches(part: MigrationFootprint, row: tuple[Any, ...]) -> bool:
    """Use the exact reference/value/item/BOM/POS tuple from historical main."""
    return (part.reference, part.value, part.footprint, not part.bom, not part.pos) == (
        row[0],
        row[1],
        row[2],
        bool(row[4]),
        bool(row[5]),
    )


def _coverage(
    row: tuple[Any, ...],
    referenced: Sequence[MigrationFootprint],
    schematic_lookup: SchematicLookup,
) -> LegacyRowCoverage:
    """Classify a row without treating absence and explicit native values alike."""
    code = normalize_lcsc(row[3])
    reference = str(row[0])
    component_ids = tuple(part.component_id for part in referenced)
    if not is_lcsc_part(code):
        return LegacyRowCoverage(
            reference,
            code,
            "ignored",
            component_ids,
            "legacy value is not a valid LCSC ID",
        )
    matching = tuple(part for part in referenced if _matches(part, row))
    if not matching:
        return LegacyRowCoverage(
            reference,
            code,
            "obsolete",
            component_ids,
            "historical tuple is obsolete for the current board",
        )
    if len(matching) != 1:
        return LegacyRowCoverage(
            reference, code, "unresolved", component_ids, "ambiguous native matches"
        )
    part = matching[0]
    component_ids = (part.component_id,)
    native_value = safe_assignment_value(part.assignment, part.lcsc)
    if native_value is not None:
        return LegacyRowCoverage(
            reference,
            code,
            "accounted",
            component_ids,
            "explicit native assignment takes precedence",
            "explicit_clear"
            if native_value == ""
            else ("already_matches" if native_value == code else "board_override"),
            native_value,
        )
    if part.assignment.status != "missing" or part.assignment.aliases:
        reason = (
            "native assignment has unsafe aliases"
            if part.assignment.status in {"valid", "empty", "missing"}
            else f"native assignment is {part.assignment.status}"
        )
        return LegacyRowCoverage(
            reference,
            code,
            "unresolved",
            component_ids,
            reason,
        )
    linked = _as_link(schematic_lookup(part.component_id))
    if linked.status in {"pcb_only", "no_schematic"}:
        reason = "recover into missing native assignment without a schematic target"
    elif linked.status != "linked" or not linked.members:
        return LegacyRowCoverage(
            reference,
            code,
            "unresolved",
            component_ids,
            linked.reason or "linked schematic members are unresolved",
        )
    else:
        for assignment, value in linked.members:
            if (assignment.status == "missing" and not assignment.aliases) or (
                safe_assignment_value(assignment, value) == code
            ):
                continue
            reason = (
                "linked schematic assignment has unsafe aliases"
                if assignment.status in {"valid", "empty"}
                and safe_assignment_value(assignment, value) is None
                else f"linked schematic assignment disagrees ({assignment.status})"
            )
            return LegacyRowCoverage(
                reference, code, "unresolved", component_ids, reason
            )
        reason = "recover into missing native assignment"
    return LegacyRowCoverage(
        reference,
        code,
        "planned",
        component_ids,
        reason,
        "pending",
        code,
    )


def _connected_target_regions(
    groups: Mapping[tuple[str, str], Sequence[MigrationFootprint]],
) -> dict[str, tuple[str, ...]]:
    """Join components that depend on any shared physical schematic member."""
    parents: dict[str, str] = {}

    def root(component_id: str) -> str:
        """Find a component's representative while compressing its parent path."""
        parents.setdefault(component_id, component_id)
        while parents[component_id] != component_id:
            parents[component_id] = parents[parents[component_id]]
            component_id = parents[component_id]
        return component_id

    for parts in groups.values():
        first = root(parts[0].component_id)
        for part in parts[1:]:
            parents[root(part.component_id)] = first
    regions: dict[str, list[str]] = {}
    for component_id in parents:
        regions.setdefault(root(component_id), []).append(component_id)
    return {
        component_id: tuple(sorted(members))
        for members in regions.values()
        for component_id in members
    }


def _shared_target_coverage(
    footprints: Sequence[MigrationFootprint],
    coverage: list[LegacyRowCoverage],
    schematic_lookup: Callable[[str], MigrationLink],
    target_identity: Optional[TargetIdentity],
) -> list[LegacyRowCoverage]:
    """Require every shared physical member's assignment, BOM and coverage to agree."""
    planned = {
        row.component_ids[0]: row.lcsc for row in coverage if row.status == "planned"
    }
    groups: dict[tuple[str, str], list[MigrationFootprint]] = {}
    links: dict[str, MigrationLink] = {}
    blocked: dict[str, tuple[str, ...]] = {}
    for part in footprints:
        link = schematic_lookup(part.component_id)
        links[part.component_id] = link
        if link.status in {"pcb_only", "no_schematic"}:
            continue
        keys = link.target_ids
        if not keys and target_identity is not None:
            target = target_identity(part.component_id)
            keys = (target,) if target is not None else ()
        for key in set(keys):
            groups.setdefault(key, []).append(part)
        if not keys and part.component_id in planned:
            blocked[part.component_id] = (part.component_id,)
    regions = _connected_target_regions(groups)
    for key, parts in groups.items():
        identities = regions[parts[0].component_id]
        candidates = [
            component_id for component_id in identities if component_id in planned
        ]
        if not candidates:
            continue
        values = {
            (
                planned[part.component_id]
                if part.component_id in planned
                else safe_assignment_value(part.assignment, part.lcsc),
                part.bom,
            )
            for part in parts
        }
        covered = [
            item
            for part in parts
            for item in links[part.component_id].covered_occurrences
            if item[:2] == key
        ]
        expected = {
            item
            for part in parts
            for item in links[part.component_id].expected_occurrences
            if item[:2] == key
        }
        complete = not expected or (
            set(covered) == expected and len(covered) == len(set(covered))
        )
        if not complete or len(values) != 1 or next(iter(values))[0] is None:
            blocked.update((component_id, identities) for component_id in candidates)
    return [
        replace(
            row,
            status="unresolved",
            disposition="unresolved",
            native_value=None,
            component_ids=blocked[row.component_ids[0]],
            reason="shared schematic target has incomplete coverage or native instances disagree",
        )
        if row.status == "planned" and row.component_ids[0] in blocked
        else row
        for row in coverage
    ]


def plan_legacy_migration(
    dbfile: str,
    footprints: Sequence[MigrationFootprint],
    schematic_lookup: SchematicLookup,
    *,
    target_identity: Optional[TargetIdentity] = None,
    imported_assignments: Optional[Mapping[str, str]] = None,
) -> LegacyMigrationPlan:
    """Plan normalized missing-field imports and report every unaccounted row.

    Valid and explicitly empty native fields supersede historical data. Invalid
    or conflicting native fields, unresolved schematic links, and schematic
    disagreement are preserved for user resolution. Obsolete rows do not block
    local coverage; callers separately verify saved current and sibling boards
    before archiving the directory-wide table.

    The lookup returns authenticated member provenance or explicit absence;
    None remains an unsafe unresolved link for older callers. Stock, flags,
    catalog metadata, and preferences are never imported or consulted. A supplied
    target_identity callback additionally requires every native instance of a
    reused schematic symbol to agree with proposed imports and shared BOM state.
    """
    active = _read_active_rows(dbfile)
    if active is None:
        return LegacyMigrationPlan(False)
    rows, digest, generation = active
    by_reference: dict[str, list[MigrationFootprint]] = {}
    for part in footprints:
        by_reference.setdefault(part.reference, []).append(part)
    links: dict[str, MigrationLink] = {}

    def lookup(component_id: str) -> MigrationLink:
        """Read each callback once so per-member evidence stays consistent."""
        if component_id not in links:
            links[component_id] = _as_link(schematic_lookup(component_id))
        return links[component_id]

    coverage = []
    imported = imported_assignments or {}
    for row in rows:
        decision = _coverage(row, by_reference.get(row[0], ()), lookup)
        disposition = (
            decision.disposition
            or {
                "obsolete": "obsolete",
                "unresolved": "unresolved",
                "ignored": "invalid_legacy",
            }[decision.status]
        )
        if decision.status == "accounted" and len(decision.component_ids) == 1:
            previous = imported.get(decision.component_ids[0])
            if (
                previous is not None
                and normalize_lcsc(previous) == decision.native_value == decision.lcsc
            ):
                disposition = "imported"
        coverage.append(
            replace(
                decision,
                disposition=disposition,
                identity=(row[0], row[1], row[2], bool(row[4]), bool(row[5])),
            )
        )
    candidates: dict[str, set[str]] = {}
    for row in coverage:
        if row.status == "planned":
            candidates.setdefault(row.component_ids[0], set()).add(row.lcsc)
    conflicting = {key for key, codes in candidates.items() if len(codes) != 1}
    coverage = [
        replace(
            row,
            status="unresolved",
            disposition="unresolved",
            native_value=None,
            reason="conflicting legacy assignment rows",
        )
        if row.status == "planned" and row.component_ids[0] in conflicting
        else row
        for row in coverage
    ]
    if target_identity is not None or any(link.target_ids for link in links.values()):
        coverage = _shared_target_coverage(
            footprints, coverage, lookup, target_identity
        )
    assignments = tuple(
        sorted(
            {
                (row.component_ids[0], row.lcsc)
                for row in coverage
                if row.status == "planned"
            }
        )
    )
    return LegacyMigrationPlan(True, assignments, tuple(coverage), digest, generation)
