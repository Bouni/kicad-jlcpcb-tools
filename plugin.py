import csv
import logging
import os
import re
import sys
from pathlib import Path
from zipfile import ZipFile

import wx
from pcbnew import *


class JLCPCBPlugin(ActionPlugin):
    def __init__(self):
        super(JLCPCBPlugin, self).__init__()

        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self.description = "Generate JLCPCB conform Gerber, Excellon, BOM and CPL files"

    def Run(self):
        """Run is caled when the action button is clicked."""
        dialog = Dialog(None)
        dialog.Center()
        dialog.ShowModal()
        dialog.Destroy()


class Dialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self, parent, id=-1, title="KiCAD JLCPCB tools", size=(820, 420)
        )
        self.SetIcon(
            wx.Icon(
                os.path.join(
                    os.path.abspath(os.path.dirname(__file__)), "jlcpcb-icon.png"
                )
            )
        )
        panel = wx.Panel(self)
        log = wx.TextCtrl(
            panel,
            wx.ID_ANY,
            size=(800, 300),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        description = wx.StaticText(
            panel, label="Generate JLCPCB production and assembly files."
        )
        execbtn = wx.Button(panel, label="Generate")
        clsbtn = wx.Button(panel, label="Close")
        clsbtn.Bind(wx.EVT_BUTTON, self.close)
        execbtn.Bind(wx.EVT_BUTTON, self.exec)
        buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
        buttonSizer.Add(execbtn)
        buttonSizer.Add(clsbtn)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(description, flag=wx.EXPAND | wx.BOTTOM | wx.TOP | wx.LEFT, border=5)
        layout.Add(buttonSizer, flag=wx.EXPAND | wx.LEFT, border=5)
        layout.Add(
            log, flag=wx.EXPAND | wx.BOTTOM | wx.TOP | wx.LEFT | wx.RIGHT, border=5
        )
        panel.SetSizer(layout)
        panel.Refresh()
        panel.Layout()
        # redirect text here
        sys.stdout = log
        self.init_logger()

    def close(self, e):
        e.Skip()
        self.Close()

    def exec(self, e):
        e.Skip()
        self.setup()
        self.generate_geber()
        self.generate_excellon()
        self.zip_gerber_excellon()
        self.generate_cpl()
        self.generate_bom()

    def init_logger(self):
        # Remove all handlers associated with the root logger object.
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y.%m.%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
        self.logger = logging.getLogger(__name__)

    def setup(self):
        """Setup when Run is called, before the board is not available."""
        self.board = GetBoard()
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.assemblydir = os.path.join(self.path, "jlcpcb", "assembly")
        Path(self.assemblydir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def decode_attributes(footprint):
        """Decode the footprint attributes. Didn't came up with a solution from pcbnew so far."""
        attributes = {}
        val = footprint.GetAttributes()
        attributes["tht"] = bool(val & 0b1)
        attributes["smd"] = bool(val & 0b10)
        attributes["not_in_schematic"] = bool(val & 0b100)
        attributes["exclude_from_pos"] = bool(val & 0b1000)
        attributes["exclude_from_bom"] = bool(val & 0b1000)
        attributes["other"] = not (attributes["tht"] or attributes["smd"])
        return attributes

    def generate_geber(self):
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
        plot_plan = [
            ("CuTop", F_Cu, "Top layer"),
            ("CuBottom", B_Cu, "Bottom layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("SilkBottom", B_SilkS, "Silk top"),
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("MaskTop", F_Mask, "Mask top"),
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
        cplname = f"CPL-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.assemblydir, cplname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(
                self.board.GetFootprints(),
                key=lambda fp: (
                    fp.GetValue(),
                    int(re.search("\d+", fp.GetReference())[0]),
                ),
            )
            for footprint in footprints:
                attributes = self.decode_attributes(footprint)
                if attributes.get("exclude_from_pos"):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from POS' and skipped!"
                    )
                    continue
                writer.writerow(
                    [
                        footprint.GetReference(),
                        footprint.GetValue(),
                        footprint.GetFPID().GetLibItemName(),
                        ToMM(footprint.GetPosition().x),
                        ToMM(footprint.GetPosition().y),
                        footprint.GetOrientation() / 10,
                        "top" if footprint.GetLayer() == 0 else "bottom",
                    ]
                )
        self.logger.info(f"Finished generating CPL file")

    def generate_bom(self):
        bomname = f"BOM-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.assemblydir, bomname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC"])
            footprints = {}
            for footprint in self.board.GetFootprints():
                attributes = self.decode_attributes(footprint)
                if attributes.get("exclude_from_bom"):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from BOM' and skipped!"
                    )
                    continue
                lcsc = footprint.GetProperties().get("LCSC")
                if not lcsc:
                    self.logger.error(
                        f"{footprint.GetReference()} has no LCSC attribute and skipped!"
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


JLCPCBPlugin().register()
