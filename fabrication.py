"""Handles the generation of the Gerber files, the BOM and the POS file."""

import csv
import logging
import math
import os
from pathlib import Path
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


class Fabrication:
    """Contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent, board, adapter_set=None):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = board
        self.kicad: Any = adapter_set or getattr(parent, "kicad", None)
        if self.kicad is None:
            raise ValueError("Fabrication requires an initialized KiCad adapter set")
        self.corrections = []
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.outputdir = os.path.join(self.path, "jlcpcb", "production_files")
        Path(self.outputdir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    def fill_zones(self):
        """Refill copper zones following user prompt."""
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            self.kicad.utility.refill_zones(self.board)

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        original = footprint.GetOrientation()
        # `.AsDegrees()` added in KiCAD 6.99
        try:
            rotation = original.AsDegrees()
        except AttributeError:
            # we need to divide by 10 to get 180 out of 1800 for example.
            # This might be a bug in 5.99 / 6.0 RC
            rotation = original / 10
        if footprint.GetLayer() != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
        # First check if the part name matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetReference())):
                return self.rotate(footprint, rotation, correction)
        # Then try to match by value
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetValue())):
                return self.rotate(footprint, rotation, correction)
        # Then if the package matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetFPID().GetLibItemName())):
                return self.rotate(footprint, rotation, correction)
        # If no correction matches, return the original rotation
        return rotation

    def rotate(self, footprint, rotation, correction):
        """Calculate the actual correction."""
        rotation = (rotation + int(correction)) % 360
        self.logger.info(
            "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
            footprint.GetReference(),
            footprint.GetValue(),
            footprint.GetFPID().GetLibItemName(),
            "Top" if footprint.GetLayer() == 0 else "Bottom",
            correction,
        )
        return rotation

    def reposition(self, footprint, position, offset):
        """Adjust the position of the footprint, returning the new position as a wxPoint."""
        if offset[0] != 0 or offset[1] != 0:
            original = footprint.GetOrientation()
            # `.AsRadians()` added in KiCAD 6.99
            try:
                rotation = original.AsDegrees()
            except AttributeError:
                # we need to divide by 10 to get 180 out of 1800 for example.
                # This might be a bug in 5.99 / 6.0 RC
                rotation = original / 10
            if footprint.GetLayer() != 0:
                # bottom angles need to be mirrored on Y-axis
                rotation = (180 - rotation) % 360
            offset_x = self.kicad.utility.from_mm(offset[0]) * math.cos(
                math.radians(rotation)
            ) + self.kicad.utility.from_mm(offset[1]) * math.sin(math.radians(rotation))
            offset_y = -self.kicad.utility.from_mm(offset[0]) * math.sin(
                math.radians(rotation)
            ) + self.kicad.utility.from_mm(offset[1]) * math.cos(math.radians(rotation))
            if footprint.GetLayer() != 0:
                # mirrored coordinate system needs to be taken into account on the bottom
                offset_x = -offset_x
            self.logger.info(
                "Fixed position of %s (%s / %s) on %s Layer by %f/%f",
                footprint.GetReference(),
                footprint.GetValue(),
                footprint.GetFPID().GetLibItemName(),
                "Top" if footprint.GetLayer() == 0 else "Bottom",
                offset[0],
                offset[1],
            )
            return self.kicad.utility.create_wx_point(
                position.x + offset_x, position.y + offset_y
            )
        return position

    def fix_position(self, footprint, position):
        """Fix the position of footprints in order to be correct for JLCPCB."""
        # First check if the part name matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetReference())):
                return self.reposition(footprint, position, correction)
        # Then try to match by value
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetValue())):
                return self.reposition(footprint, position, correction)
        # Then if the package matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetFPID().GetLibItemName())):
                return self.reposition(footprint, position, correction)
        # If no correction matches, return the original position
        return position

    def get_position(self, footprint):
        """Calculate position based on center of bounding box."""
        try:
            pads = footprint.Pads()
            bbox = pads[0].GetBoundingBox()
            for pad in pads:
                bbox.Merge(pad.GetBoundingBox())
            return bbox.GetCenter()
        except:
            self.logger.info(
                "WARNING footprint %s: original position used", footprint.GetReference()
            )
            return footprint.GetPosition()

    def generate_geber(self, layer_count=None):
        """Generate Gerber files."""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py

        layers = self.kicad.utility.get_layer_constants()
        pctl = self.kicad.gerber.create_plot_controller(self.board)
        popt = self.kicad.gerber.get_plot_options(pctl)

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        self.kicad.gerber.set_output_directory(popt, self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        self.kicad.gerber.set_format(popt, 1)

        # General Options
        self.kicad.gerber.set_plot_component_values(
            popt,
            self.parent.settings.get("gerber", {}).get("plot_values", True),
        )
        self.kicad.gerber.set_plot_reference_designators(
            popt,
            self.parent.settings.get("gerber", {}).get("plot_references", True),
        )
        self.kicad.gerber.set_sketch_pads_on_mask_layers(popt, False)

        # Gerber Options
        self.kicad.gerber.set_use_protel_extensions(popt, False)
        self.kicad.gerber.set_create_job_file(popt, False)
        self.kicad.gerber.set_mask_color(popt, True)
        self.kicad.gerber.set_use_auxiliary_origin(popt, True)

        # Tented vias or not, selcted by user in settings
        # Only possible via settings in KiCAD < 8.99
        # In KiCAD 8.99 this must be set in the layer settings of KiCAD
        self.kicad.gerber.set_plot_vias_on_mask(
            popt,
            not self.parent.settings.get("gerber", {}).get("tented_vias", True),
        )

        self.kicad.gerber.set_use_x2_format(popt, True)
        self.kicad.gerber.set_include_netlist_attributes(popt, True)
        self.kicad.gerber.set_disable_macros(popt, False)
        self.kicad.gerber.set_drill_marks(popt, self.kicad.utility.get_no_drill_shape())

        popt.SetPlotFrameRef(False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", layers["F_Cu"], "Top layer"),
            ("SilkTop", layers["F_SilkS"], "Silk top"),
            ("MaskTop", layers["F_Mask"], "Mask top"),
            ("PasteTop", layers["F_Paste"], "Paste top"),
        ]
        plot_plan_bottom = [
            ("CuBottom", layers["B_Cu"], "Bottom layer"),
            ("SilkBottom", layers["B_SilkS"], "Silk bottom"),
            ("MaskBottom", layers["B_Mask"], "Mask bottom"),
            ("EdgeCuts", layers["Edge_Cuts"], "Edges"),
            ("PasteBottom", layers["B_Paste"], "Paste bottom"),
        ]

        plot_plan = []

        # Single sided PCB
        if layer_count == 1:
            plot_plan = plot_plan_top + plot_plan_bottom[-2:]
        # Double sided PCB
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        # Everything with inner layers
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (
                        f"CuIn{layer}",
                        self.kicad.utility.get_inner_cu_layer(layer),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        # Add all JLC prefixed layers - layers must have "JLC_" in their name
        jlc_layers_to_plot = []
        enabled_layer_ids = list(self.board.GetEnabledLayers().Seq())
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = str(self.board.GetLayerName(enabled_layer_id)).upper()
            if "JLC_" in layer_name_string:
                plotter_info = (layer_name_string, enabled_layer_id, layer_name_string)
                jlc_layers_to_plot.append(plotter_info)
        plot_plan += jlc_layers_to_plot

        for layer_info in plot_plan:
            if layer_info[1] <= layers["B_Cu"]:
                popt.SetSkipPlotNPTH_Pads(True)
            else:
                popt.SetSkipPlotNPTH_Pads(False)
            self.kicad.gerber.set_layer(pctl, layer_info[1])
            self.kicad.gerber.open_plot_file(
                pctl,
                layer_info[0],
                self.kicad.utility.get_plot_format_gerber(),
                layer_info[2],
            )
            plotted = self.kicad.gerber.plot_layer(pctl)

            if plotted is False:
                self.logger.error("Error plotting %s", layer_info[2])
            self.logger.info("Successfully plotted %s", layer_info[2])
        self.kicad.gerber.close_plot(pctl)

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = self.kicad.gerber.create_excellon_writer(self.board)
        mirror = False
        minimalHeader = False
        offset = self.board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        drlwriter.SetFormat(False)
        self.kicad.gerber.generate_drill_files(drlwriter, self.gerberdir)
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        zipname = f"GERBER-{Path(self.filename).stem}.zip"
        with ZipFile(
            os.path.join(self.outputdir, zipname),
            "w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
        ) as zipfile:
            for folderName, _, filenames in os.walk(self.gerberdir):
                for filename in filenames:
                    if not filename.endswith(("gbr", "drl", "pdf")):
                        continue
                    filePath = os.path.join(folderName, filename)
                    zipfile.write(filePath, os.path.basename(filePath))
        self.logger.info(
            "Finished generating ZIP file %s", os.path.join(self.outputdir, zipname)
        )

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        cplname = f"CPL-{Path(self.filename).stem}.csv"
        self.corrections = self.parent.library.get_all_correction_data()
        aux_orgin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        with open(
            os.path.join(self.outputdir, cplname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(self.board.Footprints(), key=lambda x: x.GetReference())
            for fp in footprints:
                if self.kicad.footprint.get_is_dnp(fp):
                    self.logger.info(
                        "Component %s has 'Do not place' enabled: removing from CPL",
                        fp.GetReference(),
                    )
                    continue
                part = self.parent.store.get_part(fp.GetReference())
                if not part:  # No matching part in the database, continue
                    continue
                if part["exclude_from_pos"] == 1:
                    continue
                if not add_without_lcsc and not part["lcsc"]:
                    continue
                try:  # Kicad <= 8.0
                    position = self.get_position(fp) - aux_orgin
                except TypeError:  # Kicad 8.99
                    x1, y1 = self.get_position(fp)
                    x2, y2 = aux_orgin
                    position = self.kicad.utility.create_vector2i(x1 - x2, y1 - y2)
                position = self.fix_position(fp, position)
                writer.writerow(
                    [
                        part["reference"],
                        part["value"],
                        part["footprint"],
                        self.kicad.utility.to_mm(position.x),
                        self.kicad.utility.to_mm(position.y) * -1,
                        self.fix_rotation(fp),
                        "top" if fp.GetLayer() == 0 else "bottom",
                    ]
                )
        self.logger.info(
            "Finished generating CPL file %s", os.path.join(self.outputdir, cplname)
        )

    def generate_bom(self):
        """Generate BOM file."""
        bomname = f"BOM-{Path(self.filename).stem}.csv"
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {fp.GetReference(): fp for fp in self.board.Footprints()}
        with open(
            os.path.join(self.outputdir, bomname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC", "Quantity"])
            for part in self.parent.store.read_bom_parts():
                if not add_without_lcsc and not part["lcsc"]:
                    self.logger.info(
                        "Component group %s has no LCSC number assigned and the setting Add parts without LCSC is disabled: removing from BOM",
                        part["refs"],
                    )
                    continue
                components = []
                for component in part["refs"].split(","):
                    fp = footprints.get(component)
                    if fp and self.kicad.footprint.get_is_dnp(fp):
                        self.logger.info(
                            "Component %s has 'Do not place' enabled: removing from BOM",
                            component,
                        )
                        continue
                    components.append(component)
                if not components:
                    continue
                writer.writerow(
                    [
                        part["value"],
                        ",".join(components),
                        part["footprint"],
                        part["lcsc"],
                        len(components),
                    ]
                )
        self.logger.info(
            "Finished generating BOM file %s", os.path.join(self.outputdir, bomname)
        )

    def get_part_consistency_warnings(self) -> str:
        """Check the plausibility of the parts, there should be just one value per LCSC number.

        Returns an empty sting if all parts are ok, otherwise a otherwise a overview of parts that share a LCSC number but have different values.
        """
        lcsc_numbers = {}
        for item in self.parent.store.read_bom_parts():
            if not item["lcsc"]:
                continue
            if item["lcsc"] not in lcsc_numbers:
                lcsc_numbers[item["lcsc"]] = [
                    {"refs": item["refs"], "values": item["value"]}
                ]
            else:
                lcsc_numbers[item["lcsc"]].append(
                    {"refs": item["refs"], "values": item["value"]}
                )
        filtered = {key: value for key, value in lcsc_numbers.items() if len(value) > 1}
        result = ""
        for lcsc, items in filtered.items():
            result += f"{lcsc}:\n"
            for item in items:
                result += f"  - {item['refs']} -> {item['values']}\n"
        return result
