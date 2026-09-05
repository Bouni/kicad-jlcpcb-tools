"""Module for exporting LCSC data to schematic."""

import glob
import logging
import os
import os.path
import re

from pcbnew import GetBuildVersion  # pylint: disable=import-error

from .core.version import is_version6, is_version7


class SchematicExport:
    """A class to export Schematic files."""

    # This only works with KiCad v6/v7/v8 files, if the format changes, this will probably break

    # A symbol's own Reference property, and any per-instance reference from
    # a reused hierarchical sheet.
    _TOP_REF_RX = re.compile(r'\(property\s"Reference"\s"([^"]*)"')
    _INSTANCE_REF_RX = re.compile(r'\(reference\s"([^"]*)"\)')
    _PROJECT_RX = re.compile(r'\(project\s"([^"]*)"')
    _UUID_RX = re.compile(r'\(uuid\s"?([^"\s)]*)"?\)')
    _PATH_RX = re.compile(r'\(path\s"([^"]*)"')
    _IN_BOM_RX = re.compile(r"\(in_bom\s(yes|no)\)")

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    @staticmethod
    def _is_symbol_start(line):
        """Return True for the opening line of a placed symbol, in v6, v7 and v8+ files.

        A locally modified symbol carries (lib_name ...) ahead of its
        (lib_id ...), so keying on lib_id alone silently misses it and
        shifts every following symbol's data by one. Definitions inside
        (lib_symbols ...) name their symbol on the opening line itself,
        so they never match.
        """
        stripped = line.strip()
        return stripped == "(symbol" or stripped.startswith(
            ("(symbol (lib_id", "(symbol (lib_name")
        )

    def _project_name(self):
        """Return the project name KiCad writes into (project "...") instances.

        The board filename is only a guess at it: a board saved under a
        different name still belongs to the project named by the
        .kicad_pro file sitting next to it.
        """
        found = glob.glob(os.path.join(self.parent.project_path, "*.kicad_pro"))
        if len(found) == 1:
            return os.path.splitext(os.path.basename(found[0]))[0]
        return os.path.splitext(self.parent.board_name)[0]

    def _resolved_bom(self, refs, store_parts):
        """Return the exclude-from-BOM state shared by every ref, or None when there isn't one."""
        matched = {p["reference"]: p for p in store_parts if p["reference"] in refs}
        if not matched:
            return None
        missing = refs - matched.keys()
        if missing:
            self.logger.warning(
                "Reused symbol %s is missing PCB data for %s, skipping",
                sorted(refs),
                sorted(missing),
            )
            return None
        states = {p["exclude_from_bom"] for p in matched.values()}
        if len(states) != 1:
            self.logger.warning(
                "Reused symbol %s disagrees on its BOM state, skipping", sorted(refs)
            )
            return None
        return states.pop()

    def _placed_symbols(self, lines):
        """Map each placed symbol to its uuid, its in_bom line and every reference it resolves to."""
        project_name = self._project_name()
        symbols = []
        uuid = None
        top_ref = None
        bom_line = None
        per_project = {}
        has_instances = False
        current_project = None
        started = False
        symbol_end = None

        def finish():
            if project_name in per_project:
                instance_refs = per_project[project_name]
            elif list(per_project) == [""]:
                # KiCad writes (project "") for a sheet with no owning
                # project, so a sole empty name is this project's own.
                instance_refs = per_project[""]
            elif per_project:
                self.logger.warning(
                    "Reused symbol's instances %s don't include project %s, skipping",
                    sorted(per_project),
                    project_name,
                )
                instance_refs = set()
            elif has_instances:
                self.logger.warning(
                    "Symbol %s has an empty instances block, skipping", uuid
                )
                instance_refs = set()
            else:
                instance_refs = None
            if instance_refs is None:
                refs = {top_ref} if top_ref else set()
            else:
                refs = set(instance_refs)
            symbols.append({"uuid": uuid, "refs": refs, "bom_line": bom_line})

        for index, line in enumerate(lines):
            if self._is_symbol_start(line):
                if started:
                    finish()
                started = True
                uuid = None
                top_ref = None
                bom_line = None
                per_project = {}
                has_instances = False
                current_project = None
                # The symbol's fields end at the closing paren indented
                # like its opening one. Anything after that belongs to a
                # sheet or an instance table, not to this symbol.
                symbol_end = line[: line.index("(symbol")] + ")"
            if not started:
                continue
            if line.rstrip() == symbol_end:
                finish()
                started = False
                continue
            if uuid is None and (m := self._UUID_RX.search(line)):
                uuid = m.group(1)
            if bom_line is None and self._IN_BOM_RX.search(line):
                bom_line = index
            if m := self._TOP_REF_RX.search(line):
                top_ref = m.group(1)
            if "(instances" in line:
                has_instances = True
            if m := self._PROJECT_RX.search(line):
                current_project = m.group(1)
                per_project.setdefault(current_project, set())
            elif current_project is not None and (
                m := self._INSTANCE_REF_RX.search(line)
            ):
                per_project[current_project].add(m.group(1))
        if started:
            finish()
        return symbols

    def _v6_root_instance_refs(self):
        """Map each symbol uuid to its reused-sheet references, read from KiCad 6's project-wide symbol_instances table.

        Only the project's own root sheet is read. A reused child sheet can
        carry another project's table, so picking whichever selected file
        happens to hold one would resolve references against the wrong
        project.
        """
        root = os.path.join(
            self.parent.project_path, self._project_name() + ".kicad_sch"
        )
        try:
            with open(root, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []

        refs = {}
        in_instances = False
        symbol_uuid = ""
        for line in lines:
            if "(symbol_instances" in line:
                in_instances = True
                continue
            if not in_instances:
                continue
            if m := self._PATH_RX.search(line):
                symbol_uuid = m.group(1).rsplit("/", 1)[-1]
            if symbol_uuid and (m := self._INSTANCE_REF_RX.search(line)):
                refs.setdefault(symbol_uuid, set()).add(m.group(1))
                symbol_uuid = ""
        if not in_instances:
            self.logger.warning(
                "No KiCad 6 symbol_instances table in %s; BOM/LCSC sync will be skipped",
                root,
            )
        return refs

    def _sync_bom(self, lines, store_parts, v6_refs=None):
        """Rewrite the in_bom line of every symbol whose instances agree, in place.

        Runs before the file is read for the LCSC pass, so the two stay
        independent: this only ever rewrites an existing in_bom value.
        """
        for symbol in self._placed_symbols(lines):
            if symbol["bom_line"] is None:
                continue
            refs = (
                symbol["refs"]
                if v6_refs is None
                else v6_refs.get(symbol["uuid"], set())
            )
            bom = self._resolved_bom(refs, store_parts)
            if bom is None:
                continue
            desired = "no" if bom else "yes"
            lines[symbol["bom_line"]] = self._IN_BOM_RX.sub(
                f"(in_bom {desired})", lines[symbol["bom_line"]]
            )

    def load_schematic(self, paths):
        """Load schematic file."""
        if is_version6(GetBuildVersion()):
            self.logger.info("Kicad 6...")
            v6_instance_refs = self._v6_root_instance_refs()
            for path in paths:
                self._update_schematic6(path, v6_instance_refs)
        elif is_version7(GetBuildVersion()):
            self.logger.info("Kicad 7...")
            for path in paths:
                self._update_schematic7(path)
        else:
            self.logger.info("Kicad 8+...")
            for path in paths:
                self._update_schematic(path)

    def _update_schematic6(self, path, v6_instance_refs):
        """Only works with KiCad V6 files."""
        self.logger.info("Reading %s...", path)
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile(
            '\\(property\\s\\"(.*)\\"\\s\\"(.*)\\"\\s\\(id\\s(\\d+)\\)\\s\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)'
        )
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        store_parts = self.parent.store.read_all()

        lastID = -1
        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""
        lastRef = ""

        lines = []
        newlines = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        self._sync_bom(lines, store_parts, v6_instance_refs)

        if os.path.exists(path + "_old"):
            os.remove(path + "_old")
        os.rename(path, path + "_old")
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
                lastID = int(m.group(3))

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
                    lastLoc = m.group(4)
                    lastRef = value
                    for part in store_parts:
                        if value == part["reference"]:
                            newLcsc = part["lcsc"]
                            break
            # if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search(inLine)
            if m:
                if lastLcsc == "" and newLcsc != "" and lastLoc != "" and lastID != -1:
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'    (property "LCSC" "{newLcsc}" (id {lastID + 1}) (at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                lastID = -1
                lastLoc = ""
                lastLcsc = ""
                newLcsc = ""
                lastRef = ""
            newlines.append(outLine)

        with open(path, "w", encoding="utf-8") as f:
            for line in newlines:
                f.write(line + "\n")
        self.logger.info("Added LCSC's to %s(maybe?)", path)

    def _update_schematic7(self, path):
        """Only works with KiCad V7 files."""
        self.logger.info("Reading %s...", path)
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile(
            '\\(property\\s\\"(.*)\\"\\s\\"(.*)\\"\\s\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)'
        )
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        store_parts = self.parent.store.read_all()

        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""
        lastRef = ""

        lines = []
        newlines = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        self._sync_bom(lines, store_parts)

        if os.path.exists(path + "_old"):
            os.remove(path + "_old")
        os.rename(path, path + "_old")
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

        with open(path, "w", encoding="utf-8") as f:
            for line in newlines:
                f.write(line + "\n")
        self.logger.info("Added LCSC's to %s (maybe?)", path)

    def _update_schematic(self, path):
        """Only works with KiCad V8+ files."""
        self.logger.info("Reading %s...", path)
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile('\\(property\\s\\"(.*)\\"\\s"(.*)\\"')
        atRx = re.compile("\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)")
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"')

        store_parts = self.parent.store.read_all()

        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""
        lastRef = ""

        lines = []
        newlines = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        self._sync_bom(lines, store_parts)

        partSection = False
        files_seen = set()  # keeps sheet files already processed.

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
                if key == "Sheetfile":
                    file_name = m.group(2)
                    if file_name not in files_seen:
                        files_seen.add(file_name)
                        dir_name = os.path.dirname(path)
                        self._update_schematic(os.path.join(dir_name, file_name))
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
        if os.path.exists(path + "_old"):
            os.remove(path + "_old")
        os.rename(path, path + "_old")
        with open(path, "w", encoding="utf-8") as f:
            for line in newlines:
                f.write(line + "\n")
        self.logger.info("Added LCSC's to %s (maybe?)", path)
