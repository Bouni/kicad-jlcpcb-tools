"""Handles the generation of the Gerber files, the BOM and the POS file."""

import csv
import logging
import math
import os
from pathlib import Path
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

try:
    from .export_api import SWIGExportPlan, create_export_plan
except ImportError:  # pragma: no cover - fallback for direct script imports/tests
    from export_api import SWIGExportPlan, create_export_plan


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
        self.layer_count = None
        board_filename = self.get_board_filename()
        self.path, self.filename = os.path.split(board_filename)
        self.create_folders()
        self.export_plan = create_export_plan(self)

    def get_board_filename(self) -> str:
        """Return the current board filename across SWIG and IPC backends."""
        board_filename = ""
        board_adapter = getattr(self.kicad, "board", None)
        if board_adapter is not None and hasattr(board_adapter, "get_board_filename"):
            board_filename = board_adapter.get_board_filename() or ""

        if not board_filename and hasattr(self.board, "GetFileName"):
            board_filename = self.board.GetFileName()

        if not board_filename and isinstance(self.board, dict):
            board_filename = str(self.board.get("path", ""))

        if not board_filename:
            project_path = getattr(self.parent, "project_path", "")
            board_name = getattr(self.parent, "board_name", "")
            if project_path and board_name:
                board_filename = os.path.join(project_path, board_name)

        if not board_filename:
            return ""

        if os.path.isabs(board_filename):
            if os.path.exists(board_filename):
                return board_filename

        if board_filename and not os.path.isabs(board_filename):
            kiprjmod = os.getenv("KIPRJMOD", "")
            if kiprjmod:
                candidate = os.path.abspath(os.path.join(kiprjmod, board_filename))
                if os.path.exists(candidate):
                    return candidate

        project_path = getattr(self.parent, "project_path", "")
        if project_path:
            candidate = os.path.abspath(os.path.join(project_path, board_filename))
            if os.path.exists(candidate):
                return candidate

        if not board_filename:
            board_name = getattr(self.parent, "board_name", "")
            kiprjmod = os.getenv("KIPRJMOD", "")
            if board_name and kiprjmod:
                candidate = os.path.abspath(os.path.join(kiprjmod, board_name))
                if os.path.exists(candidate):
                    return candidate

        pcbnew_module = getattr(self.kicad, "pcbnew", None)
        if pcbnew_module is not None and hasattr(pcbnew_module, "GetBoard"):
            try:
                swig_board = pcbnew_module.GetBoard()
                swig_name = swig_board.GetFileName() if swig_board is not None else ""
                if swig_name:
                    return os.path.abspath(swig_name)
            except Exception:  # noqa: BLE001
                pass

        return os.path.abspath(board_filename)

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
        footprint_api = self.kicad.footprint
        rotation = footprint_api.get_orientation(footprint)
        if footprint_api.get_layer(footprint) != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
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
        self.layer_count = layer_count
        self.export_plan.generate_gerbers(layer_count)

    def _generate_gerber_impl(self, layer_count=None):
        """Compatibility shim; use `generate_geber()` / `export_plan` instead."""
        SWIGExportPlan(self).generate_gerbers(layer_count)

    def generate_excellon(self):
        """Generate Excellon files."""
        self.export_plan.generate_drill_files()

    def _generate_excellon_impl(self):
        """Compatibility shim; use `generate_excellon()` / `export_plan` instead."""
        SWIGExportPlan(self).generate_drill_files()

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
        aux_orgin = self.kicad.board.get_aux_origin()
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
        self.logger.info(
            "Finished generating CPL file %s", os.path.join(self.outputdir, cplname)
        )

    def generate_bom(self):
        """Generate BOM file."""
        bomname = f"BOM-{Path(self.filename).stem}.csv"
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {
            self.kicad.footprint.get_reference(fp): fp
            for fp in self.kicad.board.get_footprints()
        }
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
