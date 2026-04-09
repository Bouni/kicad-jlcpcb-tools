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

    def get_gerber_zip_path(self):
        """Return the full path to the generated Gerber ZIP file."""
        return os.path.join(self.outputdir, f"GERBER-{Path(self.filename).stem}.zip")

    def get_cpl_csv_path(self):
        """Return the full path to the generated CPL CSV file."""
        return os.path.join(self.outputdir, f"CPL-{Path(self.filename).stem}.csv")

    def get_bom_csv_path(self):
        """Return the full path to the generated BOM CSV file."""
        return os.path.join(self.outputdir, f"BOM-{Path(self.filename).stem}.csv")

    def get_artifact_paths(self):
        """Return all generated production artifact paths."""
        return {
            "gerber_zip": self.get_gerber_zip_path(),
            "cpl_csv": self.get_cpl_csv_path(),
            "bom_csv": self.get_bom_csv_path(),
        }

    def fill_zones(self):
        """Refill copper zones following user prompt."""
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            self.kicad.utility.refill_zones(self.board)

    def _find_correction(self, value):
        """Return (rotation, offset) for the first correction matching value.

        Tries anchored match (pattern + '$') before falling back to unanchored,
        so 'SOT-23-3' beats 'SOT-23' when both patterns exist.
        """
        anchored = [(f"(?:{r})$", rot, off) for r, rot, off in self.corrections]
        for regex, rotation, offset in anchored:
            if re.search(regex, value):
                return rotation, offset
        for regex, rotation, offset in self.corrections:
            if re.search(regex, value):
                return rotation, offset
        return None

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        footprint_api = self.kicad.footprint
        rotation = footprint_api.get_orientation(footprint)
        if footprint_api.get_layer(footprint) != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
<<<<<<< HEAD
        for getter in (
            lambda: str(footprint.GetReference()),
            lambda: str(footprint.GetValue()),
            lambda: str(footprint.GetFPID().GetLibItemName()),
        ):
            match = self._find_correction(getter())
            if match:
                return self.rotate(footprint, rotation, match[0])
=======
        # First check if the part name matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint_api.get_reference(footprint))):
                return self.rotate(footprint, rotation, correction)
        # Then try to match by value
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint_api.get_value(footprint))):
                return self.rotate(footprint, rotation, correction)
        # Then if the package matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint_api.get_fpid_name(footprint))):
                return self.rotate(footprint, rotation, correction)
        # If no correction matches, return the original rotation
>>>>>>> 71a1b0f (refactor(fabrication): use FootprintAPI for footprint metadata)
        return rotation

    def rotate(self, footprint, rotation, correction):
        """Calculate the actual correction."""
        footprint_api = self.kicad.footprint
        rotation = (rotation + int(correction)) % 360
        self.logger.info(
            "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
            footprint_api.get_reference(footprint),
            footprint_api.get_value(footprint),
            footprint_api.get_fpid_name(footprint),
            "Top" if footprint_api.get_layer(footprint) == 0 else "Bottom",
            correction,
        )
        return rotation

    def reposition(self, footprint, position, offset):
        """Adjust the position of the footprint, returning the new position as a wxPoint."""
        if offset[0] != 0 or offset[1] != 0:
            footprint_api = self.kicad.footprint
            rotation = footprint_api.get_orientation(footprint)
            if footprint_api.get_layer(footprint) != 0:
                # bottom angles need to be mirrored on Y-axis
                rotation = (180 - rotation) % 360
            offset_x = self.kicad.utility.from_mm(offset[0]) * math.cos(
                math.radians(rotation)
            ) + self.kicad.utility.from_mm(offset[1]) * math.sin(math.radians(rotation))
            offset_y = -self.kicad.utility.from_mm(offset[0]) * math.sin(
                math.radians(rotation)
            ) + self.kicad.utility.from_mm(offset[1]) * math.cos(math.radians(rotation))
            if footprint_api.get_layer(footprint) != 0:
                # mirrored coordinate system needs to be taken into account on the bottom
                offset_x = -offset_x
            self.logger.info(
                "Fixed position of %s (%s / %s) on %s Layer by %f/%f",
                footprint_api.get_reference(footprint),
                footprint_api.get_value(footprint),
                footprint_api.get_fpid_name(footprint),
                "Top" if footprint_api.get_layer(footprint) == 0 else "Bottom",
                offset[0],
                offset[1],
            )
            return self.kicad.utility.create_wx_point(
                position.x + offset_x, position.y + offset_y
            )
        return position

    def fix_position(self, footprint, position):
        """Fix the position of footprints in order to be correct for JLCPCB."""
<<<<<<< HEAD
        for getter in (
            lambda: str(footprint.GetReference()),
            lambda: str(footprint.GetValue()),
            lambda: str(footprint.GetFPID().GetLibItemName()),
        ):
            match = self._find_correction(getter())
            if match:
                return self.reposition(footprint, position, match[1])
=======
        footprint_api = self.kicad.footprint
        # First check if the part name matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint_api.get_reference(footprint))):
                return self.reposition(footprint, position, correction)
        # Then try to match by value
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint_api.get_value(footprint))):
                return self.reposition(footprint, position, correction)
        # Then if the package matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint_api.get_fpid_name(footprint))):
                return self.reposition(footprint, position, correction)
        # If no correction matches, return the original position
>>>>>>> 71a1b0f (refactor(fabrication): use FootprintAPI for footprint metadata)
        return position

    def get_position(self, footprint):
        """Calculate position based on center of bounding box."""
        try:
            pads = self.kicad.footprint.get_pads(footprint)
            bbox = pads[0].GetBoundingBox()
            for pad in pads:
                bbox.Merge(pad.GetBoundingBox())
            return bbox.GetCenter()
        except:
            self.logger.info(
                "WARNING footprint %s: original position used",
                self.kicad.footprint.get_reference(footprint),
            )
            x_pos, y_pos = self.kicad.footprint.get_position(footprint)
            return self.kicad.utility.create_vector2i(int(x_pos), int(y_pos))

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
        self.kicad.gerber.set_plot_frame_ref(popt, False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.kicad.board.get_copper_layer_count()

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
        enabled_layer_ids = self.kicad.board.get_enabled_layers()
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = self.kicad.board.get_layer_name(enabled_layer_id).upper()
            if "JLC_" in layer_name_string:
                plotter_info = (layer_name_string, enabled_layer_id, layer_name_string)
                jlc_layers_to_plot.append(plotter_info)
        plot_plan += jlc_layers_to_plot

        for layer_info in plot_plan:
            if layer_info[1] <= layers["B_Cu"]:
                self.kicad.gerber.set_skip_plot_npth_pads(popt, True)
            else:
                self.kicad.gerber.set_skip_plot_npth_pads(popt, False)
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
        offset = self.kicad.board.get_aux_origin()
        mergeNPTH = False
        self.kicad.gerber.set_drill_options(
            drlwriter,
            Options=(mirror, minimalHeader, offset, mergeNPTH),
        )
        self.kicad.gerber.set_drill_format(drlwriter, False)
        self.kicad.gerber.generate_drill_files(drlwriter, self.gerberdir)
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        zip_path = self.get_gerber_zip_path()
        with ZipFile(
            zip_path,
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
        self.logger.info("Finished generating ZIP file %s", zip_path)

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        cpl_path = self.get_cpl_csv_path()
        self.corrections = self.parent.library.get_all_correction_data()
        aux_orgin = self.kicad.board.get_aux_origin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        with open(cpl_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(
                self.kicad.board.get_footprints(),
                key=lambda footprint: self.kicad.footprint.get_reference(footprint),
            )
            for fp in footprints:
                if self.kicad.footprint.get_is_dnp(fp):
                    self.logger.info(
                        "Component %s has 'Do not place' enabled: removing from CPL",
                        self.kicad.footprint.get_reference(fp),
                    )
                    continue
                part = self.parent.store.get_part(self.kicad.footprint.get_reference(fp))
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
                        "top" if self.kicad.footprint.get_layer(fp) == 0 else "bottom",
                    ]
                )
        self.logger.info("Finished generating CPL file %s", cpl_path)

    def generate_bom(self):
        """Generate BOM file."""
        bom_path = self.get_bom_csv_path()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {
            self.kicad.footprint.get_reference(fp): fp
            for fp in self.kicad.board.get_footprints()
        }
        with open(bom_path, "w", newline="", encoding="utf-8") as csvfile:
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
        self.logger.info("Finished generating BOM file %s", bom_path)

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
