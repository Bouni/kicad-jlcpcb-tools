import csv
import logging
import os
import re
from pathlib import Path
from zipfile import ZipFile

import requests
from pcbnew import *

from .helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_footprint_by_ref,
    get_valid_footprints,
    set_exclude_from_bom,
    set_exclude_from_pos,
)


class JLCPCBFabrication:
    def __init__(self, parent):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.plugin_path, _ = os.path.split(os.path.abspath(__file__))
        self.corrections = self.get_corrections()
        self.board = GetBoard()
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()
        self.load_part_assigments()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.assemblydir = os.path.join(self.path, "jlcpcb", "assembly")
        Path(self.assemblydir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    def load_part_assigments(self):
        # Read all footprints and their maybe set LCSC property
        self.parts = {}
        for fp in get_valid_footprints(self.board):
            reference = fp.GetReference()
            lcsc = fp.GetProperties().get("LCSC", "")
            self.parts[reference] = {"lcsc": lcsc}
        # Read all settings from the csv and overwrite if neccessary
        csvfile = os.path.join(self.path, "jlcpcb", "part_assignments.csv")
        if os.path.isfile(csvfile):
            with open(csvfile, "r+") as f:
                reader = csv.DictReader(
                    f,
                    delimiter=",",
                    quotechar='"',
                    fieldnames=["ref", "lcsc", "bom", "pos"],
                )
                for row in reader:
                    if row["ref"] in self.parts:
                        # Only set lcsc value from CSV if not already set from footprint property
                        if not self.parts[row["ref"]]["lcsc"]:
                            self.parts[row["ref"]]["lcsc"] = row["lcsc"]
                        # set the exclude from BOM / POS attribute of the footprint from CSV
                        fp = get_footprint_by_ref(self.board, row["ref"])
                        set_exclude_from_bom(fp, bool(int(row["bom"])))
                        set_exclude_from_pos(fp, bool(int(row["pos"])))
        self.save_part_assignments()

    def save_part_assignments(self):
        """Write part assignments to a csv file"""
        csvfile = os.path.join(self.path, "jlcpcb", "part_assignments.csv")
        with open(csvfile, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=",", quotechar='"')
            for part, values in self.parts.items():
                fp = get_footprint_by_ref(self.board, part)
                bom = get_exclude_from_bom(fp)
                pos = get_exclude_from_pos(fp)
                writer.writerow([part, values["lcsc"], int(bom), int(pos)])

    def get_corrections(self):
        """Try loading rotation corrections from local file, if not present, load them from GitHub."""
        csvfile = os.path.join(self.plugin_path, "corrections", "cpl_rotations_db.csv")
        if os.path.isfile(csvfile):
            with open(csvfile) as f:
                c = csv.reader(f, delimiter=",", quotechar='"')
                return list(c)[1:]
        else:
            """Download and parse footprint rotation corrections from Matthew Lai's JLCKicadTool repo"""
            url = "https://raw.githubusercontent.com/matthewlai/JLCKicadTools/master/jlc_kicad_tools/cpl_rotations_db.csv"
            self.logger.info(f"Load corrections from {url}")
            r = requests.get(url)
            c = csv.reader(r.text.splitlines(), delimiter=",", quotechar='"')
            return list(c)[1:]

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        original = footprint.GetOrientation()
        # we need to devide by 10 to get 180 out of 1800 for example.
        # This might be a bug in 5.99
        rotation = original / 10
        for pattern, correction in self.corrections:
            if re.match(pattern, str(footprint.GetFPID().GetLibItemName())):
                if footprint.GetLayer() == 0:
                    rotation = (rotation + int(correction)) % 360
                    self.logger.info(
                        f"Fixed rotation of {footprint.GetReference()} ({footprint.GetFPID().GetLibItemName()}) on Top Layer by {correction} degrees"
                    )
                else:
                    rotation = (rotation - int(correction)) % 360
                    self.logger.info(
                        f"Fixed rotation of {footprint.GetReference()} ({footprint.GetFPID().GetLibItemName()}) on Bottom Layer by {correction} degrees"
                    )
        return rotation

    def generate_geber(self, layer_count=None):
        """Generating Gerber files"""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py
        pctl = PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()
        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(True)
        popt.SetPlotReference(True)
        popt.SetPlotInvisibleText(False)

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)
        popt.SetCreateGerberJobFile(False)
        popt.SetSubtractMaskFromSilk(False)

        popt.SetUseGerberX2format(True)
        popt.SetIncludeGerberNetlistInfo(True)
        popt.SetDisableGerberMacros(False)

        popt.SetPlotFrameRef(False)
        popt.SetExcludeEdgeLayer(True)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        if layer_count == 1:
            plot_plan = [
                ("CuTop", F_Cu, "Top layer"),
                ("SilkTop", F_SilkS, "Silk top"),
                ("MaskTop", F_Mask, "Mask top"),
                ("EdgeCuts", Edge_Cuts, "Edges"),
            ]
        elif layer_count == 2:
            plot_plan = [
                ("CuTop", F_Cu, "Top layer"),
                ("SilkTop", F_SilkS, "Silk top"),
                ("MaskTop", F_Mask, "Mask top"),
                ("CuBottom", B_Cu, "Bottom layer"),
                ("SilkBottom", B_SilkS, "Silk top"),
                ("MaskBottom", B_Mask, "Mask bottom"),
                ("EdgeCuts", Edge_Cuts, "Edges"),
            ]
        elif layer_count == 4:
            plot_plan = [
                ("CuTop", F_Cu, "Top layer"),
                ("SilkTop", F_SilkS, "Silk top"),
                ("MaskTop", F_Mask, "Mask top"),
                ("CuIn1", In1_Cu, "Inner layer 1"),
                ("CuIn2", In2_Cu, "Inner layer 2"),
                ("CuBottom", B_Cu, "Bottom layer"),
                ("SilkBottom", B_SilkS, "Silk top"),
                ("MaskBottom", B_Mask, "Mask bottom"),
                ("EdgeCuts", Edge_Cuts, "Edges"),
            ]
        elif layer_count == 6:
            plot_plan = [
                ("CuTop", F_Cu, "Top layer"),
                ("SilkTop", F_SilkS, "Silk top"),
                ("MaskTop", F_Mask, "Mask top"),
                ("CuIn1", In1_Cu, "Inner layer 1"),
                ("CuIn2", In2_Cu, "Inner layer 2"),
                ("CuIn3", In3_Cu, "Inner layer 3"),
                ("CuIn4", In4_Cu, "Inner layer 4"),
                ("CuBottom", B_Cu, "Bottom layer"),
                ("SilkBottom", B_SilkS, "Silk top"),
                ("MaskBottom", B_Mask, "Mask bottom"),
                ("EdgeCuts", Edge_Cuts, "Edges"),
            ]

        for layer_info in plot_plan:
            if layer_info[1] <= B_Cu:
                popt.SetSkipPlotNPTH_Pads(True)
            else:
                popt.SetSkipPlotNPTH_Pads(False)
            pctl.SetLayer(layer_info[1])
            pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
            if pctl.PlotLayer() == False:
                self.logger.error(f"Error ploting {layer_info[2]}")
            self.logger.info(f"Successfully ploted {layer_info[2]}")
        pctl.ClosePlot()

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = EXCELLON_WRITER(self.board)
        mirror = False
        minimalHeader = False
        offset = wxPoint(0, 0)
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        drlwriter.SetFormat(False)
        genDrl = True
        genMap = False
        drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap)
        self.logger.info(f"Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        zipname = f"GERBER-{self.filename.split('.')[0]}.zip"
        with ZipFile(os.path.join(self.gerberdir, zipname), "w") as zipfile:
            for folderName, subfolders, filenames in os.walk(self.gerberdir):
                for filename in filenames:
                    if not filename.endswith(("gbr", "drl")):
                        continue
                    filePath = os.path.join(folderName, filename)
                    zipfile.write(filePath, os.path.basename(filePath))
        self.logger.info(f"Finished generating ZIP file")

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        cplname = f"CPL-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.assemblydir, cplname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(
                get_valid_footprints(self.board),
                key=lambda fp: (
                    fp.GetValue(),
                    int(re.search("\d+", fp.GetReference())[0]),
                ),
            )
            for footprint in footprints:
                if get_exclude_from_pos(footprint):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from POS' and is skipped!"
                    )
                    continue
                writer.writerow(
                    [
                        footprint.GetReference(),
                        footprint.GetValue(),
                        footprint.GetFPID().GetLibItemName(),
                        ToMM(footprint.GetPosition().x),
                        ToMM(footprint.GetPosition().y) * -1,
                        self.fix_rotation(footprint),
                        "top" if footprint.GetLayer() == 0 else "bottom",
                    ]
                )
        self.logger.info(f"Finished generating CPL file")

    def generate_bom(self):
        """Generate BOM file."""
        bomname = f"BOM-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.assemblydir, bomname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC"])
            footprints = {}
            for footprint in get_valid_footprints(self.board):
                if get_exclude_from_bom(footprint):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from BOM' and is skipped!"
                    )
                    continue
                lcsc = self.parts.get(footprint.GetReference(), {}).get("lcsc", "")
                if not lcsc:
                    self.logger.error(
                        f"{footprint.GetReference()} has no LCSC attribute and is skipped!"
                    )
                    continue
                if not lcsc in footprints:
                    footprints[lcsc] = {
                        "comment": footprint.GetValue(),
                        "designators": [footprint.GetReference()],
                        "footprint": footprint.GetFPID().GetLibItemName(),
                    }
                else:
                    footprints[lcsc]["designators"].append(footprint.GetReference())
            for lcsc, data in footprints.items():
                designators = sorted(
                    data["designators"],
                    key=lambda r: int(re.search("\d+", r)[0]),
                )
                writer.writerow(
                    [
                        data["comment"],
                        ",".join(designators),
                        data["footprint"],
                        lcsc,
                    ]
                )
        self.logger.info(f"Finished generating BOM file")
