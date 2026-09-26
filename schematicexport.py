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
        self, refs: set[str], store_parts: tuple[dict[str, Any], ...]
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
        store_parts: tuple[dict[str, Any], ...],
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
        parts: Optional[Iterable[Mapping[str, Any]]] = None,  # noqa: UP045
    ) -> None:
        """Export one validated Default snapshot using the existing base writer.

        Every sheet under the given schematics is exported once. Nothing is
        written if a sheet file is missing, unreadable or read-only, or
        while KiCad has a lock on any sheet, or on the project's own
        schematic, other than approved_locks (SchematicLockedError names
        them all). Every sheet is prepared before the first is written, so
        a sheet that cannot be processed stops the export before any write;
        a sheet that cannot be replaced still leaves the sheets before it
        exported.

        Matrix callers supply an explicit Default snapshot. The ordinary fallback
        accepts only a Default store view and reads it once for the whole export.
        Neither the focused matrix cell nor the native editor selection changes
        the meaning of this source.
        """
        self._require_default(variant_name)
        get_board = getattr(self.parent, "_get_current_board", None)
        if callable(get_board):
            get_board()
        if parts is None:
            store = self.parent.store
            self._require_default(getattr(store, "variant_name", ""))
            parts = store.read_all()
        store_parts = tuple(dict(part) for part in parts)
        references: set[str] = set()
        for part in store_parts:
            self._require_default(part.get("variant_name", ""))
            # Check required source keys before any format branch opens a file.
            if not {"reference", "lcsc", "exclude_from_bom"}.issubset(part):
                raise ValueError(
                    "Default schematic export requires reference, LCSC, and BOM data."
                )
            reference = part["reference"]
            if reference in references:
                raise ValueError(
                    f"Duplicate PCB reference {reference}; annotate the PCB before saving."
                )
            references.add(reference)
            if part.get("assignment_status") in {"invalid", "conflict"}:
                raise ValueError(
                    f"PCB reference {part['reference']} has unresolved LCSC "
                    f"fields ({part['assignment_status']}). "
                    "Resolve its Default part fields before saving the schematic."
                )

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

        if is_version7(GetBuildVersion()):
            self.logger.info("Kicad 7...")
            render = self._render_schematic7
        else:
            self.logger.info("Kicad 8+...")
            render = self._render_schematic
        rendered = [(path, render(path, store_parts)) for path in encountered]

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
            self.logger.info("Added LCSC's to %s (maybe?)", path)
            identity = _file_identity(path)
            if identity is not None:
                written.add(identity)

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

    def _render_schematic7(
        self, path: str, store_parts: tuple[dict[str, Any], ...]
    ) -> str:
        """Prepare a KiCad 7 sheet without writing any project file."""
        return self._render_assignments(path, store_parts, version7=True)

    def _render_schematic(
        self, path: str, store_parts: tuple[dict[str, Any], ...]
    ) -> str:
        """Prepare a KiCad 8+ sheet without writing any project file."""
        return self._render_assignments(path, store_parts, version7=False)

    def _render_assignments(
        self,
        path: str,
        store_parts: tuple[dict[str, Any], ...],
        *,
        version7: bool,
    ) -> str:
        """Require every matched symbol's instances to save one shared assignment.

        Parse direct placed-symbol fields independently of their order or pins.
        Unsafe shared symbols abort preparation, before the first sheet is
        written, so incomplete saving cannot authorize legacy-table retirement.
        """
        self.logger.info("Reading %s...", path)
        warnings: list[str] = []
        with open(path, encoding="utf-8") as source:
            updated = update_assignment_fields(
                source.read(),
                {part["reference"]: part["lcsc"] for part in store_parts},
                version7=version7,
                project_name=lambda: self._project_name,
                warnings=warnings,
            )
        if warnings:
            raise ValueError(
                "Cannot save schematic assignments: " + "; ".join(warnings)
            )
        lines = StringIO(updated).readlines()
        for index, desired in self._bom_updates(lines, store_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])
        return "".join(lines)
