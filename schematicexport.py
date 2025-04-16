"""Module for exporting LCSC data to schematic."""

import logging
import os
import os.path
import re

from pcbnew import GetBuildVersion  # pylint: disable=import-error

from .core.version import is_version6, is_version7


class SchematicExport:
    """A class to export Schematic files."""

    # This only works with KiCad v6/v7/v8 files, if the format changes, this will probably break

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    def load_schematic(self, paths):
        """Load schematic file."""
        if is_version6(GetBuildVersion()):
            self.logger.info("Kicad 6...")
            for path in paths:
                self._update_schematic6(path)
        elif is_version7(GetBuildVersion()):
            self.logger.info("Kicad 7...")
            for path in paths:
                self._update_schematic7(path)
        else:
            self.logger.info("Kicad 8+...")
            for path in paths:
                self._update_schematic(path)

    def _update_schematic6(self, path):
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
