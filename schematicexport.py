import logging
import os
import re
import wx

class SchematicExport:

    """A class to export files"""
    def __init__(self, parent):
        self.logger = logging.getLogger(__name__)
        self.parent = parent

    def load_schematic(self, paths):
        self.logger.info(f"SchematicExport worked")
        for path in paths:
            self.logger.info(f"{path}")
            self._update_schematic(path)

    def _update_schematic(self, path):
        """Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it"""
        """keep track of property ids and Reference property location to use with new LCSC property"""
        propRx = re.compile('\\(property\\s\\"(.*)\\"\s\\"(.*)\\"\s\\(id\\s(\\d+)\\)\\s\\(at\\s(-?\\d+.\\d+\\s-?\\d+.\\d+)\s\\d+\\)')
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        store_parts = self.parent.store.read_all()

        lastID = 0
        lastLoc = ""
        lastLcsc = ""
        newLcsc = ""

        lines = []
        newlines = []
        with open(path) as f:
            lines = f.readlines()

        os.rename(path, path + "_old")
        for line in lines:
            inLine = line.rstrip()
            outLine = inLine
            m = propRx.search( inLine )
            if m:
                key = m.group(1)
                value = m.group(2)
                lastID = int(m.group(3))
                #lastLoc = m.group(4)
                #self.logger.info(key)
                #self.logger.info(value)
                #self.logger.info(lastID)
                #self.logger.info(lastLoc)

                #found a LCSC property, so update it if needed
                if key == "LCSC":
                    lastLcsc = value
                    if lastLcsc == newLcsc:
                        outLine.replace(lastLcsc, newLcsc)

                if key == "Reference":
                    lastLoc = m.group(4)
                    for part in store_parts:
                        if value == part[0]:
                            newLcsc = part[3]
                            break
            #if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search( inLine )
            if m:
                if lastLcsc == "" and newLcsc != "" and lastLoc != "" and lastID != 0 :
                    self.logger.info(f'found {newLcsc}')
                    #    (property "LCSC" "C192778" (id 6) (at 173.99 101.6 0)
                    newTxt = "    (property \"LCSC\" \"{}\" (id {}) (at {} 0)".format(newLcsc,(lastID+1),lastLoc)
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                lastID = 0
                lastLoc = ""
                lastLcsc = ""
                newLcsc = ""
            newlines.append(outLine)

        f = open(path, "w")
        for line in newlines:
            f.write(line + "\n")
        f.close()
        self.logger.info(f'Added LCSC\'s to {path}(maybe?)')
