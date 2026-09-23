"""Module for exporting LCSC data to schematic."""

from collections.abc import Collection, Iterable, Mapping
from functools import cached_property
from io import StringIO
import logging
import os
import os.path
import re
from typing import Any, Optional

from pcbnew import GetBuildVersion  # pylint: disable=import-error

from .core.version import is_version7
from .schematic_fields import update_assignment_fields
from .schematic_safety import (
    SchematicLockedError,
    assert_schematics_not_locked,
    assert_schematics_writable,
    atomic_write_schematic,
    collect_schematic_hierarchy,
    matching_project_names,
    project_schematic_path,
)
from .schematic_snapshot import DefaultSchematicSnapshot, capture_board

__all__ = [
    "SchematicExport",
    "SchematicLockedError",
    "SchematicVariantExportError",
]


class SchematicVariantExportError(ValueError):
    """Reject named-variant data before the base schematic writer touches files."""


def _file_identity(path: str) -> Optional[tuple[int, int]]:  # noqa: UP045
    """Return the device and inode that `path` currently refers to.

    None when the filesystem reports no inode, as some Windows filesystems
    do, since a zero inode would make every file look like the same one.
    """
    file_stat = os.stat(path)
    if file_stat.st_ino == 0:
        return None
    return (file_stat.st_dev, file_stat.st_ino)


class SchematicExport:
    """A class to export Schematic files."""

    # This only works with KiCad v7+ files; if the format changes, this will probably break.

    _IN_BOM_RX = re.compile(r"^(\s*)\(in_bom\s+(yes|no)\)")
    _REFERENCE_RX = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')
    _INSTANCE_REF_RX = re.compile(r'\(reference\s+"([^"]*)"\)')
    _PROJECT_RX = re.compile(r'\(project\s+"([^"]*)"')
    _UUID_RX = re.compile(r'\(uuid\s+"?([^"\s)]*)"?\)')

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
                "Not updating project-specific BOM states for %s; "
                "open board has no project identity",
                self.parent.board_name,
            )
            return None

        matches = matching_project_names(
            get_manager(), self.parent.project_path, board_project
        )
        if len(matches) == 1:
            return matches[0]
        self.logger.warning(
            "Not updating project-specific BOM states for %s; "
            "expected one matching .kicad_pro file, found %d",
            self.parent.board_name,
            len(matches),
        )
        return None

    def _resolved_bom(
        self, refs: set[str], store_parts: tuple[Mapping[str, Any], ...]
    ) -> Optional[bool]:  # noqa: UP045
        """Return a shared exclude-from-BOM state, or None when it is unsafe."""
        matched = {
            part["reference"]: bool(part["exclude_from_bom"])
            for part in store_parts
            if part["reference"] in refs
        }
        if not matched:
            return None
        if set(matched) != refs:
            self.logger.warning(
                "Not updating BOM state for %s; PCB data is missing %s",
                sorted(refs),
                sorted(refs - set(matched)),
            )
            return None
        states = set(matched.values())
        if len(states) != 1:
            self.logger.warning(
                "Not updating BOM state for %s; instances disagree", sorted(refs)
            )
            return None
        return states.pop()

    def _bom_updates(
        self,
        lines: list[str],
        store_parts: tuple[Mapping[str, Any], ...],
    ) -> dict[int, str]:
        """Return in_bom line updates that are safe for every symbol instance."""
        symbols = []
        symbol = None
        symbol_end = ""
        project = None

        for index, line in enumerate(lines):
            in_line = line.rstrip()
            stripped = in_line.strip()
            symbol_start = stripped == "(symbol" or stripped.startswith(
                ("(symbol (lib_id", "(symbol (lib_name")
            )
            if symbol_start:
                symbol = {
                    "bom_line": None,
                    "uuid": "",
                    "reference": "",
                    "instances": None,
                }
                symbols.append(symbol)
                symbol_end = in_line[: in_line.index("(symbol")] + ")"
                project = None
                continue
            if symbol is None:
                continue
            if in_line == symbol_end:
                symbol = None
                project = None
                continue
            if symbol["bom_line"] is None and self._IN_BOM_RX.search(in_line):
                symbol["bom_line"] = index
            if not symbol["uuid"] and (match := self._UUID_RX.search(in_line)):
                symbol["uuid"] = match.group(1)
            if match := self._REFERENCE_RX.search(in_line):
                symbol["reference"] = match.group(1)
            if "(instances" in in_line:
                symbol["instances"] = {}
            if match := self._PROJECT_RX.search(in_line):
                project = match.group(1)
                if symbol["instances"] is None:
                    symbol["instances"] = {}
                symbol["instances"].setdefault(project, set())
            if project is not None and (match := self._INSTANCE_REF_RX.search(in_line)):
                symbol["instances"][project].add(match.group(1))

        project_name = None
        if any(
            symbol["instances"] is not None and set(symbol["instances"]) != {""}
            for symbol in symbols
        ):
            project_name = self._project_name

        updates = {}
        for symbol in symbols:
            if symbol["instances"] is None:
                refs = {symbol["reference"]}
            elif set(symbol["instances"]) == {""}:
                refs = symbol["instances"][""]
            elif project_name is None:
                refs = set()
            else:
                refs = symbol["instances"].get(project_name, set())
            if not refs:
                if project_name is not None:
                    self.logger.warning(
                        "Not updating BOM state for %s; no instances resolve for project %s",
                        symbol["reference"] or symbol["uuid"],
                        project_name,
                    )
                continue
            bom = self._resolved_bom(refs, store_parts)
            if bom is not None and symbol["bom_line"] is not None:
                updates[symbol["bom_line"]] = "no" if bom else "yes"
        return updates

    def load_schematic(
        self,
        paths: Iterable[str],
        approved_locks: Collection[str] = (),
        *,
        variant_name: str = "",
        snapshot: Optional[DefaultSchematicSnapshot] = None,
    ) -> None:
        """Export one captured Default board state without consulting cached mappings.

        Every sheet under the given schematics is exported once. Nothing is
        written if a sheet file is missing, unreadable or read-only, or
        while KiCad has a lock on any sheet, or on the project's own
        schematic, other than approved_locks (SchematicLockedError names
        them all). Every sheet is prepared before the first is written, so
        a sheet that cannot be processed stops the export before any write;
        a sheet that cannot be replaced still leaves the sheets before it
        exported.
        """
        self._require_default(variant_name)
        if snapshot is None:
            snapshot = capture_board(self.parent.pcbnew.GetBoard())
        if not isinstance(snapshot, DefaultSchematicSnapshot):
            raise TypeError("Schematic export requires a captured Default snapshot.")

        warnings = list(snapshot.warnings)
        version7 = is_version7(GetBuildVersion())
        try:
            # Every name a sheet is reached by is checked for a lock, because
            # KiCad locks the path it opened; each file is then written once.
            encountered = list(
                dict.fromkeys(hp for p in paths for hp in collect_schematic_hierarchy(p))
            )
            # KiCad locks the project's own schematic whenever the project is
            # open, even when that file is not one of the sheets written here.
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

            rendered = [
                (
                    path,
                    "".join(
                        self._prepare_schematic(
                            path, snapshot, version7=version7, warnings=warnings
                        )
                    ),
                )
                for path in encountered
            ]

            # A name that already refers to a file this export wrote, through a
            # symlink or as another spelling of one directory entry, is not
            # written again, or its backup would hold the first export's output.
            # Writing replaces a directory entry, so a hard link to an exported
            # file still refers to the original and is written under its own name.
            written: set[tuple[int, int]] = set()
            for path, content in rendered:
                identity = _file_identity(path)
                if identity is not None and identity in written:
                    self.logger.info("%s is another name for a sheet already written", path)
                    continue
                atomic_write_schematic(path, content)
                self.logger.info("Updated part assignments in %s", path)
                identity = _file_identity(path)
                if identity is not None:
                    written.add(identity)
        finally:
            if warnings:
                self.logger.warning(
                    "Preserved unresolved schematic assignments: %s. "
                    "Resolve the affected Default part fields and export again.",
                    "; ".join(dict.fromkeys(warnings)),
                )

    @staticmethod
    def _require_default(variant_name: str) -> None:
        """Require the canonical empty Default name, never a display label."""
        if not isinstance(variant_name, str):
            raise TypeError("Schematic export requires a canonical variant name.")
        if variant_name:
            raise SchematicVariantExportError(
                "Schematic export supports Default only. "
                f"Variant {variant_name!r} assignments cannot be written into "
                "base schematic fields."
            )

    def _prepare_schematic(
        self,
        path: str,
        snapshot: DefaultSchematicSnapshot,
        *,
        version7: bool,
        warnings: list[str],
    ) -> list[str]:
        """Render assignment and BOM changes using physical file-line boundaries."""
        self.logger.info("Reading %s...", path)
        with open(path, encoding="utf-8") as source:
            updated = update_assignment_fields(
                source.read(),
                snapshot.assignments,
                version7=version7,
                project_name=lambda: self._project_name,
                warnings=warnings,
            )
        lines = StringIO(updated).readlines()
        for index, desired in self._bom_updates(lines, snapshot.bom_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])
        return lines
