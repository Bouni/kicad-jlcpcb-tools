"""Export Default PCB assignments through authenticated schematic UUID paths."""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
import logging
import os
from typing import Any, Optional

from pcbnew import GetBuildVersion  # pylint: disable=import-error

from .core.version import is_version7
from .part_assignments import safe_assignment_value
from .schematic_fields import update_symbol_fields
from .schematic_links import SchematicIndex
from .schematic_safety import (
    SchematicLockedError,
    assert_schematics_not_locked,
    assert_schematics_writable,
    atomic_write_schematic,
    matching_project_names,
    project_schematic_path,
)
from .schematic_snapshot import (
    DefaultSchematicSnapshot,
    FootprintAssignment,
    capture_board,
    capture_rows,
)

__all__ = [
    "ExportOutcome",
    "SchematicExport",
    "SchematicLockedError",
    "SchematicVariantExportError",
]


class SchematicVariantExportError(ValueError):
    """Reject named-variant data before the base schematic writer touches files."""


def _file_identity(path: str) -> Optional[tuple[int, int]]:
    """Identify aliases, falling back to path names on inode-less filesystems."""
    file_stat = os.stat(path)
    if file_stat.st_ino == 0:
        return None
    return file_stat.st_dev, file_stat.st_ino


@dataclass(frozen=True)
class ExportOutcome:
    """Completed writes and assignment preservation, separate from recovery retirement.

    IDs name native footprints, never display references. A successful partial
    save still returns normally; filesystem, parse and lock failures raise.
    """

    saved: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    bom_saved: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    retirement_eligible: bool = False
    advisory: tuple[str, ...] = ()
    information: tuple[str, ...] = ()


class SchematicExport:
    """Prepare every sheet from one native snapshot before writing any file."""

    def __init__(self, parent: Any) -> None:
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    @cached_property
    def _project_name(self) -> Optional[str]:  # noqa: UP045
        """Return the open board's project name, or None if it cannot be authenticated."""
        fallback = os.path.splitext(self.parent.board_name)[0]
        pcbnew = getattr(self.parent, "pcbnew", None)
        get_manager = getattr(pcbnew, "GetSettingsManager", None)
        if get_manager is None:
            return fallback

        board = pcbnew.GetBoard()
        get_project = getattr(board, "GetProject", None)
        if get_project is None:
            return fallback
        board_project = get_project()
        if board_project is None:
            self.logger.warning(
                "Using board-name schematic lock lookup for %s; "
                "open board has no authenticated project identity",
                self.parent.board_name,
            )
            return None

        matches = matching_project_names(
            get_manager(), self.parent.project_path, board_project
        )
        if len(matches) == 1:
            return matches[0]
        self.logger.warning(
            "Using board-name schematic lock lookup for %s; "
            "expected one matching .kicad_pro file, found %d",
            self.parent.board_name,
            len(matches),
        )
        return None

    @staticmethod
    def _require_default(variant_name: str) -> None:
        """Require the canonical empty Default name, never a display label."""
        if not isinstance(variant_name, str):
            raise TypeError("Schematic export requires a canonical variant name.")
        if variant_name:
            raise SchematicVariantExportError(
                "Schematic export supports Default only. "
                f"Variant {variant_name!r} cannot be written into base fields."
            )

    def _snapshot(
        self,
        parts: Optional[Iterable[Mapping[str, Any]]],
        snapshot: Optional[DefaultSchematicSnapshot],
    ) -> DefaultSchematicSnapshot:
        """Guard the live board, then capture Default fields without a store view."""
        get_board = getattr(self.parent, "_get_current_board", None)
        board = get_board() if callable(get_board) else None
        if parts is not None and snapshot is not None:
            raise ValueError("Supply one Default snapshot source")
        if snapshot is not None:
            if not isinstance(snapshot, DefaultSchematicSnapshot):
                raise TypeError("Expected a Default schematic snapshot")
            return snapshot
        if parts is not None:
            return capture_rows(parts)
        if board is None:
            board = getattr(self.parent, "board", None)
        if board is None:
            pcbnew = getattr(self.parent, "pcbnew", None)
            getter = getattr(pcbnew, "GetBoard", None)
            if callable(getter):
                board = getter()
        if board is None:
            raise ValueError("Default schematic export requires the live PCB")
        return capture_board(board)

    def load_schematic(
        self,
        paths: Iterable[str],
        approved_locks: Collection[str] = (),
        *,
        variant_name: str = "",
        parts: Optional[Iterable[Mapping[str, Any]]] = None,
        snapshot: Optional[DefaultSchematicSnapshot] = None,
        shared_project: bool = False,
        root_uuids: Optional[Mapping[str, str]] = None,
    ) -> ExportOutcome:
        """Write safe linked assignments and report every preserved PCB assignment.

        Links contain the real root UUID, sheet UUIDs, and placed-symbol UUID.
        All instances of a physical symbol must agree, independently for LCSC
        and BOM. Missing fields preserve; only explicit empty fields clear.
        Parsing, locks and permissions are checked for the whole hierarchy
        before the first write. Existing atomic backups remain in force.
        """
        self._require_default(variant_name)
        captured = self._snapshot(parts, snapshot)
        index = SchematicIndex.from_paths(
            paths,
            shared_project=shared_project,
            root_uuids=root_uuids,
        )
        encountered = list(index.encountered_paths)
        project_schematic = project_schematic_path(
            getattr(self.parent, "project_path", None),
            getattr(self.parent, "board_name", None),
            self._project_name,
        )
        lock_paths = list(encountered)
        if project_schematic and project_schematic not in lock_paths:
            lock_paths.append(project_schematic)
        assert_schematics_not_locked(lock_paths, approved_locks)
        assert_schematics_writable(encountered)

        outcome, assignments, bom_states = self._decisions(index, captured)
        rendered = {
            path: update_symbol_fields(
                text,
                assignments.get(path, {}),
                bom_states.get(path, {}),
                version7=is_version7(GetBuildVersion()),
            )
            for path, text in index.texts.items()
        }
        # Preserve every independently named hardlink, but write symlink aliases
        # only once so a second write cannot replace its original backup.
        written: set[tuple[int, int]] = set()
        for path in encountered:
            physical = index.file_paths[path]
            identity = _file_identity(path)
            if identity is not None and identity in written:
                continue
            atomic_write_schematic(path, rendered[physical])
            identity = _file_identity(path)
            if identity is not None:
                written.add(identity)
        for diagnostic in outcome.diagnostics:
            self.logger.warning("%s", diagnostic)
        for message in (*outcome.advisory, *outcome.information):
            self.logger.info("%s", message)
        return outcome

    @staticmethod
    def _decisions(
        index: SchematicIndex,
        snapshot: DefaultSchematicSnapshot,
    ) -> tuple[ExportOutcome, dict[str, dict[str, str]], dict[str, dict[str, bool]]]:
        """Require complete connected components before planning physical writes.

        A reused unit can connect otherwise separate packages. Consensus must
        then extend to all of their units, so an exclusive sibling cannot be
        partially updated when a shared member disagrees or lacks a PCB peer.
        """
        groups: dict[tuple[str, str], list[FootprintAssignment]] = {}
        instance_paths: dict[tuple[str, str], list[str]] = {}
        neighbors: dict[tuple[str, str], set[tuple[str, str]]] = {}
        diagnostics = list(index.issues)
        blocked = bool(index.issues)
        advisory: list[str] = []
        information: list[str] = []
        saved: list[str] = []
        preserved: list[str] = []
        skipped: list[str] = []
        unresolved: list[str] = []
        bom_saved: list[str] = []
        assignments: dict[str, dict[str, str]] = {}
        bom_states: dict[str, dict[str, bool]] = {}
        for part in snapshot.parts:
            if part.schematic_path in {"", "/"}:
                preserved.append(part.component_id)
                if part.value is None and (
                    part.assignment.status != "missing" or part.assignment.aliases
                ):
                    blocked = True
                    diagnostics.append(
                        f"{part.label}: unsafe PCB-only assignment; native fields preserved."
                    )
                else:
                    information.append(
                        f"{part.label}: PCB-only footprint has no schematic assignment to save."
                    )
                continue
            component = index.resolve_component(part.schematic_path)
            if not component.resolved:
                blocked = True
                unresolved.append(part.component_id)
                diagnostics.append(
                    f"{part.label}: schematic link {part.schematic_path!r} is "
                    f"unresolved ({'; '.join(component.issues)}); "
                    "assignment and BOM preserved."
                )
            else:
                keys = {member.target.key for member in component.members}
                for member in component.members:
                    key = member.target.key
                    groups.setdefault(key, []).append(part)
                    instance_paths.setdefault(key, []).append(member.path)
                    neighbors.setdefault(key, set()).update(keys)

        pending = dict.fromkeys(groups)
        while pending:
            first = next(iter(pending))
            region: set[tuple[str, str]] = set()
            queue = [first]
            while queue:
                key = queue.pop()
                if key not in region:
                    region.add(key)
                    pending.pop(key, None)
                    queue.extend(neighbors[key] - region)
            targets = [index.targets[key] for key in sorted(region)]
            by_id = {
                part.component_id: part
                for key in sorted(region)
                for part in groups[key]
            }
            group = list(by_id.values())
            incomplete = [
                target
                for target in targets
                if set(instance_paths[target.key]) != set(target.instance_paths)
                or len(instance_paths[target.key])
                != len(set(instance_paths[target.key]))
            ]
            if incomplete:
                blocked = True
                reason = "PCB links do not cover every schematic instance exactly once"
                missing = {
                    path
                    for target in incomplete
                    for path in set(target.instance_paths)
                    - set(instance_paths[target.key])
                }
                if missing:
                    reason += f" (missing: {', '.join(sorted(missing))})"
                for part in group:
                    skipped.append(part.component_id)
                    diagnostics.append(
                        f"{part.label}: {reason}; assignment and BOM preserved."
                    )
                continue
            values = {part.value for part in group}
            if all(
                part.assignment.status == "missing" and not part.assignment.aliases
                for part in group
            ):
                preserved.extend(part.component_id for part in group)
                existing_values = {
                    safe_assignment_value(target.assignment, target.lcsc)
                    for target in targets
                    if target.assignment.status != "missing"
                }
                unsafe = any(
                    safe_assignment_value(target.assignment, target.lcsc) is None
                    and not (
                        target.assignment.status == "missing"
                        and not target.assignment.aliases
                    )
                    for target in targets
                )
                conflict = len(existing_values - {None}) > 1
                for part in group:
                    if unsafe or conflict:
                        blocked = True
                        reason = (
                            "component units disagree on schematic assignment; "
                            "reconcile their fields before running Update PCB"
                            if conflict
                            else "unsafe schematic assignment preserved"
                        )
                        diagnostics.append(f"{part.label}: {reason}.")
                    elif existing_values - {None, ""}:
                        schematic_value = next(iter(existing_values - {None, ""}))
                        advisory.append(
                            f"{part.label}: schematic assignment {schematic_value} was "
                            "preserved; run Update PCB to copy it to the board."
                        )
                    else:
                        information.append(
                            f"{part.label}: no PCB or schematic assignment; nothing to save."
                        )
            elif None not in values and len(values) == 1:
                value = next(iter(values))
                assert value is not None
                for target in targets:
                    assignments.setdefault(target.file_path, {})[target.symbol_uuid] = (
                        value
                    )
                saved.extend(part.component_id for part in group)
            else:
                blocked = True
                for part in group:
                    if part.assignment.status == "missing":
                        preserved.append(part.component_id)
                    else:
                        skipped.append(part.component_id)
                    reason = (
                        f"{part.assignment.status} assignment"
                        if part.value is None
                        else "schematic instances disagree"
                    )
                    if part.value is None and part.assignment.status == "valid":
                        reason = "unsafe assignment aliases (including whitespace)"
                    diagnostics.append(
                        f"{part.label}: {reason}; schematic assignment preserved."
                    )
            states = {part.exclude_from_bom for part in group}
            if len(states) == 1:
                state = states.pop()
                for target in targets:
                    bom_states.setdefault(target.file_path, {})[target.symbol_uuid] = (
                        state
                    )
                bom_saved.extend(part.component_id for part in group)
            else:
                blocked = True
                for part in group:
                    diagnostics.append(
                        f"{part.label}: schematic instances disagree; BOM preserved."
                    )
        if not index.texts:
            diagnostics.append("No schematic files selected; no assignments saved.")
        outcome = ExportOutcome(
            tuple(dict.fromkeys(saved)),
            tuple(dict.fromkeys(preserved)),
            tuple(dict.fromkeys(skipped)),
            tuple(dict.fromkeys(unresolved)),
            tuple(dict.fromkeys(bom_saved)),
            tuple(dict.fromkeys(diagnostics)),
            bool(index.texts) and not blocked,
            tuple(dict.fromkeys(advisory)),
            tuple(dict.fromkeys(information)),
        )
        return outcome, assignments, bom_states
