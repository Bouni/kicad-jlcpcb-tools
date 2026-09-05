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

    # This only works with KiCad v6+ files; if the format changes, this will probably break.

    _IN_BOM_RX = re.compile(r"^(\s*)\(in_bom\s+(yes|no)\)")
    _REFERENCE_RX = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')
    _INSTANCE_REF_RX = re.compile(r'\(reference\s+"([^"]*)"\)')
    _PROJECT_RX = re.compile(r'\(project\s+"([^"]*)"')
    _UUID_RX = re.compile(r'\(uuid\s+"?([^"\s)]*)"?\)')
    _PATH_RX = re.compile(r'\(path\s+"([^"]*)"')

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    def _project_name(self):
        """Return the project name associated with the open board."""
        fallback = os.path.splitext(self.parent.board_name)[0]
        pcbnew = getattr(self.parent, "pcbnew", None)
        get_manager = getattr(pcbnew, "GetSettingsManager", None)
        if get_manager is None:
            return fallback

        board = pcbnew.GetBoard()
        get_project = getattr(board, "GetProject", None)
        board_project = get_project() if get_project else None
        if board_project is None:
            return fallback

        manager = get_manager()
        matches = [
            path
            for path in glob.glob(os.path.join(self.parent.project_path, "*.kicad_pro"))
            if manager.GetProject(path) == board_project
        ]
        if len(matches) == 1:
            return os.path.splitext(os.path.basename(matches[0]))[0]
        return fallback

    def _resolved_bom(self, refs, store_parts):
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

    def _symbol_instances6(self):
        """Read KiCad 6's project-level symbol instance references by UUID."""
        root_name = self._project_name() + ".kicad_sch"
        path = os.path.join(self.parent.project_path, root_name)
        try:
            with open(path, encoding="utf-8") as f:
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
            if match := self._PATH_RX.search(line):
                symbol_uuid = match.group(1).rsplit("/", 1)[-1]
            if symbol_uuid and (match := self._INSTANCE_REF_RX.search(line)):
                refs.setdefault(symbol_uuid, set()).add(match.group(1))
                symbol_uuid = ""
        if in_instances:
            return refs

        self.logger.warning(
            "Unable to find KiCad 6 symbol instances; BOM states will not be updated"
        )
        return {}

    def _bom_updates(self, lines, store_parts, instance_refs=None):
        """Return in_bom line updates that are safe for every symbol instance."""
        project_name = self._project_name()
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
            if instance_refs is None:
                if "(instances" in in_line:
                    symbol["instances"] = {}
                if match := self._PROJECT_RX.search(in_line):
                    project = match.group(1)
                    if symbol["instances"] is None:
                        symbol["instances"] = {}
                    symbol["instances"].setdefault(project, set())
                if project is not None and (
                    match := self._INSTANCE_REF_RX.search(in_line)
                ):
                    symbol["instances"][project].add(match.group(1))

        updates = {}
        for symbol in symbols:
            if instance_refs is not None:
                refs = instance_refs.get(symbol["uuid"], set())
            elif symbol["instances"] is None:
                refs = {symbol["reference"]}
            else:
                refs = symbol["instances"].get(project_name)
                if refs is None and set(symbol["instances"]) == {""}:
                    refs = symbol["instances"][""]
                if refs is None:
                    refs = set()
            bom = self._resolved_bom(refs, store_parts)
            if bom is not None and symbol["bom_line"] is not None:
                updates[symbol["bom_line"]] = "no" if bom else "yes"
        return updates

    def load_schematic(self, paths):
        """Load schematic file."""
        if is_version6(GetBuildVersion()):
            self.logger.info("Kicad 6...")
            instance_refs = self._symbol_instances6()
            for path in paths:
                self._update_schematic6(path, instance_refs)
        elif is_version7(GetBuildVersion()):
            self.logger.info("Kicad 7...")
            for path in paths:
                self._update_schematic7(path)
        else:
            self.logger.info("Kicad 8+...")
            for path in paths:
                self._update_schematic(path)

    def _update_schematic6(self, path, instance_refs):
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

        for index, desired in self._bom_updates(
            lines, store_parts, instance_refs=instance_refs
        ).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])

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

        for index, desired in self._bom_updates(lines, store_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])

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

        for index, desired in self._bom_updates(lines, store_parts).items():
            lines[index] = self._IN_BOM_RX.sub(rf"\1(in_bom {desired})", lines[index])

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
