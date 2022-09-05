import logging
import os
import os.path
import re

import wx


class SchematicExport:

    """A class to export Schematic files"""

    """This only works with KiCad V6 files, if the format changes, this will probably break"""

    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    def load_schematic(self, paths):
        for path in paths:
            self._update_schematic(path)

    def _update_schematic(self, path):
        self.logger.info(f"Reading {path}...")
        """Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it"""
        """keep track of property ids and Reference property location to use with new LCSC property"""
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
        with open(path) as f:
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
                    if lastLcsc != newLcsc and newLcsc != "":
                        self.logger.info(f"Updating {newLcsc} on {lastRef}")
                        outLine = outLine.replace(
                            '"' + lastLcsc + '"', '"' + newLcsc + '"'
                        )
                        lastLcsc = newLcsc

                if key == "Reference":
                    lastLoc = m.group(4)
                    lastRef = value
                    for part in store_parts:
                        if value == part[0]:
                            newLcsc = part[3]
                            break
            # if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search(inLine)
            if m:
                if lastLcsc == "" and newLcsc != "" and lastLoc != "" and lastID != -1:
                    self.logger.info(f"added {newLcsc} to {lastRef}")
                    newTxt = '    (property "LCSC" "{}" (id {}) (at {} 0)'.format(
                        newLcsc, (lastID + 1), lastLoc
                    )
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                lastID = -1
                lastLoc = ""
                lastLcsc = ""
                newLcsc = ""
                lastRef = ""
            newlines.append(outLine)

        f = open(path, "w")
        for line in newlines:
            f.write(line + "\n")
        f.close()
        self.logger.info(f"Added LCSC's to {path}(maybe?)")
