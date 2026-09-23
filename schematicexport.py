"""Module for exporting LCSC data to schematic."""

from collections.abc import Collection, Iterable, Mapping
from functools import cached_property
import glob
import logging
import os
import os.path
import re
from typing import Any, Optional

from pcbnew import GetBuildVersion  # pylint: disable=import-error

from .core.version import is_version7
from .schematic_safety import (
    SchematicLockedError,
    assert_schematics_not_locked,
    assert_schematics_writable,
    atomic_write_schematic,
    collect_schematic_hierarchy,
    project_schematic_path,
)

__all__ = [
    "SchematicExport",
    "SchematicLockedError",
    "SchematicVariantExportError",
]


class SchematicVariantExportError(ValueError):
    """Reject named-variant data before the base schematic writer touches files."""


def _file_identity(path: str) -> tuple[int, int]:
    """Return the device and inode that `path` currently refers to."""
    file_stat = os.stat(path)
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

        manager = get_manager()
        matches = [
            path
            for path in glob.glob(os.path.join(self.parent.project_path, "*.kicad_pro"))
            if manager.GetProject(path) == board_project
        ]
        if len(matches) == 1:
            return os.path.splitext(os.path.basename(matches[0]))[0]
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
        them all).

        Matrix callers supply an explicit Default snapshot. The legacy fallback
        accepts only a Default store view and reads it once for the whole export.
        Neither the focused matrix cell nor the native editor selection changes
        the meaning of this source.
        """
        self._require_default(variant_name)
        if parts is None:
            store = self.parent.store
            self._require_default(getattr(store, "variant_name", ""))
            parts = store.read_all()
        store_parts = tuple(dict(part) for part in parts)
        for part in store_parts:
            self._require_default(part.get("variant_name", ""))
            # Check required source keys before any format branch opens a file.
            if not {"reference", "lcsc", "exclude_from_bom"}.issubset(part):
                raise ValueError(
                    "Default schematic export requires reference, LCSC, and BOM data."
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
        )
        lock_paths = list(encountered)
        if project_schematic and project_schematic not in lock_paths:
            lock_paths.append(project_schematic)
        assert_schematics_not_locked(lock_paths, approved_locks)
        assert_schematics_writable(encountered)

        if is_version7(GetBuildVersion()):
            self.logger.info("Kicad 7...")
            update = self._update_schematic7
        else:
            self.logger.info("Kicad 8+...")
            update = self._update_schematic
        # A name that already refers to a file this export wrote, through a
        # symlink or as another spelling of one directory entry, is not
        # written again, or its backup would hold the first export's output.
        # Writing replaces a directory entry, so a hard link to an exported
        # file still refers to the original and is written under its own name.
        written: set[tuple[int, int]] = set()
        for path in encountered:
            if _file_identity(path) in written:
                continue
            update(path, store_parts)
            written.add(_file_identity(path))

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

    def _update_schematic7(
        self, path: str, store_parts: tuple[dict[str, Any], ...]
    ) -> None:
        """Only works with KiCad V7 files."""
        self.logger.info("Reading %s...", path)
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile(
            '\\(property\\s\\"(.*)\\"\\s\\"(.*)\\"\\s\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)'
        )
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""
        lastRef = ""

        lines = []
        newlines = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        for index, desired in self._bom_updates(lines, store_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])

        partSection = False

        for line in lines:
            inLine = line.rstrip()
            outLine = inLine
            if "(symbol (lib_id" in inLine:  # skip library section
                partSection = True
            m = propRx.search(inLine)
            if m and partSection:
                key = m.group(1)
                value = m.group(2)

                # found a LCSC property, so update it if needed
                if key == "LCSC":
                    lastLcsc = value
                    if newLcsc not in (lastLcsc, ""):
                        self.logger.info("Updating %s on %s", newLcsc, lastRef)
                        outLine = outLine.replace(
                            '"' + lastLcsc + '"', '"' + newLcsc + '"'
                        )
                        lastLcsc = newLcsc

                if key == "Reference":
                    lastLoc = m.group(3)
                    lastRef = value
                    for part in store_parts:
                        if value == part["reference"]:
                            newLcsc = part["lcsc"]
                            break
            # if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search(inLine)
            if m:
                if lastLcsc == "" and newLcsc != "" and lastLoc != "":
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'    (property "LCSC" "{newLcsc}" (at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                lastLoc = ""
                lastLcsc = ""
                newLcsc = ""
                lastRef = ""
            newlines.append(outLine)

        atomic_write_schematic(path, "\n".join(newlines) + "\n")
        self.logger.info("Added LCSC's to %s (maybe?)", path)

    def _update_schematic(
        self, path: str, store_parts: tuple[dict[str, Any], ...]
    ) -> None:
        """Only works with KiCad V8+ files."""
        self.logger.info("Reading %s...", path)
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile('\\(property\\s\\"(.*)\\"\\s"(.*)\\"')
        atRx = re.compile("\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)")
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"')

        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""
        lastRef = ""

        lines = []
        newlines = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        for index, desired in self._bom_updates(lines, store_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])

        partSection = False

        for i in range(0, len(lines) - 1):
            inLine = lines[i].rstrip()
            inLine2 = lines[i + 1].rstrip()
            outLine = inLine

            if "(symbol" in inLine and "(lib_id" in inLine2:  # skip library section
                partSection = True

            # self.logger.info("line %d", i)
            m = propRx.search(inLine)
            m2 = atRx.search(inLine2)
            if m and m2 and partSection:
                key = m.group(1)
                # self.logger.info("key %s", key)
                # found a LCSC property, so update it if needed
                if key in {"LCSC", "LCSC_PN", "JLC_PN"}:
                    value = m.group(2)
                    lastLcsc = value
                    if newLcsc not in (lastLcsc, ""):
                        self.logger.info(
                            "Updating %s on %s in %s", newLcsc, lastRef, path
                        )
                        outLine = outLine.replace(
                            '"' + lastLcsc + '"', '"' + newLcsc + '"'
                        )
                        lastLcsc = newLcsc

                if key == "Reference":
                    lastLoc = m2.group(1)
                    value = m.group(2)
                    # self.logger.info("value %s", value)
                    lastRef = value
                    for part in store_parts:
                        if value == part["reference"]:
                            newLcsc = part["lcsc"]
                            break

            # if we hit the pin section without finding a LCSC property, add it
            m3 = pinRx.search(inLine)
            if m3 and partSection:
                if lastLcsc == "" and newLcsc != "" and lastLoc != "":
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'\t\t(property "LCSC" "{newLcsc}"\n\t\t\t(at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append(
                        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)"
                    )
                    newlines.append("\t\t\t)")
                    newlines.append("\t\t)")
                lastLoc = ""
                lastLcsc = ""
                newLcsc = ""
                lastRef = ""
            newlines.append(outLine)
        newlines.append(lines[len(lines) - 1].rstrip())
        atomic_write_schematic(path, "\n".join(newlines) + "\n")
        self.logger.info("Added LCSC's to %s (maybe?)", path)
