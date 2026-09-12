"""Handles the generation of the Gerber files, the BOM and the POS file."""

import csv
from importlib import import_module
import logging
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from zipfile import ZIP_DEFLATED, ZipFile

from pcbnew import (  # pylint: disable=import-error
    EXCELLON_WRITER,
    PCB_PLOT_PARAMS,
    PCB_VIA,
    PLOT_CONTROLLER,
    PLOT_FORMAT_GERBER,
    ZONE_FILLER,
    B_Cu,
    B_Mask,
    B_Paste,
    B_SilkS,
    Edge_Cuts,
    F_Cu,
    F_Mask,
    F_Paste,
    F_SilkS,
    FromMM,
    IsCopperLayer,
    Refresh,
    ToMM,
    wxPoint,
)

from .correction_data import (
    Correction,
    CorrectionMatch,
    LcscCorrection,
    match_correction,
)
from .footprint_helpers import get_is_dnp

# Compatibility hack for V6 / V7 / V7.99
try:
    from pcbnew import DRILL_MARKS_NO_DRILL_SHAPE  # pylint: disable=import-error

    NO_DRILL_SHAPE = DRILL_MARKS_NO_DRILL_SHAPE
except ImportError:
    NO_DRILL_SHAPE = PCB_PLOT_PARAMS.NO_DRILL_SHAPE

# JLC rejects BOM rows whose total length exceeds 2048 characters.  We budget
# 128 characters of headroom for the other fields (Comment, Footprint, LCSC,
# Quantity) so the Designator chunk alone is capped at 1920 characters.
_BOM_DESIGNATOR_MAX_LEN = 1920  # 2048 - 128 padding for remaining CSV fields


def _checked_position(x: float, y: float) -> Any:
    """Reject overflow before wxPoint converts doubles to signed 32-bit integers."""
    if any(not -(2**31) <= int(value) <= 2**31 - 1 for value in (x, y)):
        raise ValueError("position exceeds KiCad's signed 32-bit coordinate range")
    return wxPoint(x, y)


def split_bom_designators(
    designators: list, max_len: int = _BOM_DESIGNATOR_MAX_LEN
) -> list:
    """Split a list of reference designators into chunks whose joined length fits within *max_len*.

    JLCPCB rejects BOM rows whose total row length exceeds 2048 characters.
    The Designator field is capped below that limit to leave headroom for the
    other fields (Comment, Footprint, LCSC, Quantity).  When a single part has
    more references than fit in the limit, the row is duplicated with the
    designators spread across copies; each copy carries only its own count.

    Args:
        designators: Ordered list of reference strings, e.g. ``["R1", "R2", ...]``.
        max_len: Maximum allowed byte-length of the comma-joined designator string.

    Returns:
        A list of non-empty lists, each safe to pass to ``",".join()``.

    """
    if not designators:
        return []
    chunks = []
    current: list = []
    current_len = 0
    for ref in designators:
        # Length if this ref were appended: len(ref) plus the comma separator
        added = len(ref) if not current else len(ref) + 1
        if current and current_len + added > max_len:
            chunks.append(current)
            current = [ref]
            current_len = len(ref)
        else:
            current.append(ref)
            current_len += added
    if current:
        chunks.append(current)
    return chunks


class Fabrication:
    """Contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent: Any, board: Any) -> None:
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = board
        self.corrections: tuple[Correction, ...] = ()
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
        """Refill copper zones, returning the zone layers that poured no copper."""
        zones = self.board.Zones()
        # Refilling mutates the board, reporting does not, so only the refill is optional.
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            ZONE_FILLER(self.board).Fill(zones)
            Refresh()

        empty_pours = []
        for zone in zones:
            # Rule areas are keepouts, so they never hold copper.
            if zone.GetIsRuleArea():
                continue
            # The filler pours each layer of a zone separately, so check them the same way.
            for layer in zone.GetLayerSet().Seq():
                # Zones on technical layers are graphics, not copper.
                if not IsCopperLayer(layer):
                    continue
                if zone.GetFilledPolysList(layer).Area() > 0:
                    continue
                name = self.board.GetLayerName(layer)
                empty_pours.append(f"{zone.GetNetname() or 'no net'} on {name}")
        return empty_pours

    def _correction_for_footprint(
        self, footprint: Any, lcsc: str = ""
    ) -> Optional[CorrectionMatch]:  # noqa: UP045
        """Select the same part, reference, value or package rule as the parts table.

        The part number is the store's, which is what the BOM orders and the
        parts list shows. The footprint's own LCSC field is not consulted: it
        only seeds the store when the board is read, and a field the store has
        since moved past would rotate one part while the BOM ordered another.
        """
        return match_correction(
            self.corrections,
            str(footprint.GetReference()),
            str(footprint.GetValue()),
            str(footprint.GetFPID().GetLibItemName()),
            lcsc,
        )

    def fix_rotation(self, footprint: Any, lcsc: str = "") -> float:
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        return self._rotation_for_match(
            footprint, self._correction_for_footprint(footprint, lcsc)
        )

    def _rotation_for_match(
        self,
        footprint: Any,
        match: Optional[CorrectionMatch],  # noqa: UP045
    ) -> float:
        """Apply the already selected rule, including an explicit no-match result."""
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
        if match is not None:
            return self.rotate(footprint, rotation, match.correction.rotation)
        return rotation

    def rotate(self, footprint: Any, rotation: float, correction: int) -> float:
        """Calculate the actual correction."""
        # Keep exact whole degrees before adding KiCad's floating point angle.
        rotation = (rotation + correction % 360) % 360
        self.logger.info(
            "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
            footprint.GetReference(),
            footprint.GetValue(),
            footprint.GetFPID().GetLibItemName(),
            "Top" if footprint.GetLayer() == 0 else "Bottom",
            correction,
        )
        return rotation

    def reposition(
        self, footprint: Any, position: Any, offset: tuple[float, float]
    ) -> Any:
        """Adjust the position of the footprint, returning the new position as a wxPoint."""
        if offset[0] != 0 or offset[1] != 0:
            rotation = math.radians(self._rotation_for_match(footprint, None))
            x, y = map(FromMM, offset)
            cosine, sine = math.cos(rotation), math.sin(rotation)
            offset_x = x * cosine + y * sine
            offset_y = -x * sine + y * cosine
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
            return _checked_position(position.x + offset_x, position.y + offset_y)
        return position

    def fix_position(self, footprint: Any, position: Any, lcsc: str = "") -> Any:
        """Apply the offset from the same selected rule used for rotation."""
        return self._position_for_match(
            footprint, position, self._correction_for_footprint(footprint, lcsc)
        )

    def _position_for_match(
        self,
        footprint: Any,
        position: Any,
        match: Optional[CorrectionMatch],  # noqa: UP045
    ) -> Any:
        """Apply the selected offset without resolving the footprint again."""
        if match is not None:
            return self.reposition(footprint, position, match.correction.offset)
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

        pctl = PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        popt.SetPlotReference(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)

        popt.SetCreateGerberJobFile(False)

        popt.SetSubtractMaskFromSilk(
            self.parent.settings.get("gerber", {}).get("subtract_mask_from_silk", True)
        )

        popt.SetUseAuxOrigin(True)

        # Tented vias or not, selcted by user in settings
        # Only possible via settings in KiCAD < 8.99
        # In KiCAD 8.99 this must be set in the layer settings of KiCAD
        if hasattr(PCB_VIA, "SetPlotViaOnMaskLayer"):
            popt.SetPlotViaOnMaskLayer(
                not self.parent.settings.get("gerber", {}).get("tented_vias", True)
            )

        popt.SetUseGerberX2format(True)

        popt.SetIncludeGerberNetlistInfo(True)

        popt.SetDisableGerberMacros(False)

        popt.SetDrillMarksType(NO_DRILL_SHAPE)

        popt.SetPlotFrameRef(False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", F_Cu, "Top layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("MaskTop", F_Mask, "Mask top"),
            ("PasteTop", F_Paste, "Paste top"),
        ]
        # Silkscreen needs no copper under it, so JLCPCB prints a bottom legend
        # even on a board with a single copper layer.
        # https://jlcpcb.com/blog/single-sided-pcb-design
        plot_plan_bottom_silk = ("SilkBottom", B_SilkS, "Silk bottom")
        plot_plan_bottom = [
            ("CuBottom", B_Cu, "Bottom layer"),
            plot_plan_bottom_silk,
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("PasteBottom", B_Paste, "Paste bottom"),
        ]
        # Edge cuts are not a side, so they are added to every plan.
        plot_plan_edges = [("EdgeCuts", Edge_Cuts, "Edges")]

        plot_plan = []

        # Single sided PCB, so bottom copper, mask and paste describe a side that
        # is not manufactured. JLCPCB publishes no layer set for a 1-layer order,
        # so this follows from the board rather than from their spec.
        if layer_count == 1:
            plot_plan = plot_plan_top + [plot_plan_bottom_silk]
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
                        getattr(import_module("pcbnew"), f"In{layer}_Cu"),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        plot_plan = plot_plan + plot_plan_edges

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
            popt.SetSkipPlotNPTH_Pads(IsCopperLayer(layer_info[1]))
            pctl.SetLayer(layer_info[1])
            pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
            if pctl.PlotLayer() is False:
                self.logger.error("Error plotting %s", layer_info[2])
            self.logger.info("Successfully plotted %s", layer_info[2])
        pctl.ClosePlot()

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = EXCELLON_WRITER(self.board)
        mirror = False
        minimalHeader = False
        offset = self.board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        # JLCPCB asks for Excellon drill data in metric units.
        metric = True
        drlwriter.SetFormat(metric)
        genDrl = True
        genMap = True
        drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap)
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

    def generate_cpl(
        self,
        corrections: Optional[tuple[Correction, ...]] = None,  # noqa: UP045
    ) -> None:
        """Prepare every placement before opening the output file."""
        self.write_cpl(self.prepare_cpl(corrections))

    def prepare_cpl(
        self,
        corrections: Optional[tuple[Correction, ...]] = None,  # noqa: UP045
    ) -> tuple[tuple[Any, ...], ...]:
        """Capture placement rows from one complete immutable correction set.

        Direct calls read current storage; a supplied preflight tuple stays fixed
        for the operation. Unavailable or unresolved storage is rejected before
        touching the board or opening an existing output file.
        """
        if corrections is None:
            snapshot = self.parent.library.read_correction_data()
            corrections = snapshot.corrections
            if corrections is None:
                raise ValueError(
                    f"Corrections are unresolved in the active {snapshot.scope} "
                    f"database ({snapshot.db_path}). Open Corrections Manager "
                    "to repair or retry loading before generating fabrication files."
                )
        if not isinstance(corrections, tuple) or any(
            not isinstance(correction, (Correction, LcscCorrection))
            for correction in corrections
        ):
            raise TypeError(
                "Expected an immutable tuple of Correction or LcscCorrection values"
            )
        self.corrections = corrections
        aux_origin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        rows = []
        footprints = sorted(self.board.Footprints(), key=lambda x: x.GetReference())
        for fp in footprints:
            if get_is_dnp(fp):
                self.logger.info(
                    "Component %s has 'Do not place' enabled: removing from CPL",
                    fp.GetReference(),
                )
                continue
            part = self.parent.store.get_part(fp.GetReference())
            if not part or part["exclude_from_pos"] == 1:
                continue
            if not add_without_lcsc and not part["lcsc"]:
                continue
            match = self._correction_for_footprint(fp, part["lcsc"])
            try:
                center = self.get_position(fp)
                # Subtract in Python, before native coordinate arithmetic can wrap.
                position = SimpleNamespace(
                    x=center.x - aux_origin.x, y=center.y - aux_origin.y
                )
                position = self._position_for_match(fp, position, match)
                position = _checked_position(position.x, position.y)
                rows.append(
                    (
                        part["reference"],
                        part["value"],
                        part["footprint"],
                        # Fixed-point millimetres follow JLCPCB's exporter:
                        # https://github.com/JLCPCB/jlcpcb-eagle/blob/master/ulps/jlcpcb_smta_exporter.ulp
                        # Six decimals preserve KiCad's 1 nm internal resolution:
                        # https://docs.kicad.org/doxygen/base__units_8h.html
                        f"{ToMM(position.x):.6f}",
                        f"{ToMM(position.y) * -1:.6f}",
                        self._rotation_for_match(fp, match),
                        "top" if fp.GetLayer() == 0 else "bottom",
                    )
                )
            except (OverflowError, ValueError) as error:
                source = (
                    f"correction {match.correction.key!r}" if match else "no correction"
                )
                raise ValueError(
                    f"Cannot generate CPL for {fp.GetReference()} ({source}): {error}"
                ) from error
        return tuple(rows)

    def write_cpl(self, rows: tuple[tuple[Any, ...], ...]) -> None:
        """Write prepared placements without rereading the board or corrections."""
        cpl_path = self.get_cpl_csv_path()
        with open(cpl_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            writer.writerows(rows)
        self.logger.info("Finished generating CPL file %s", cpl_path)

    def generate_bom(self):
        """Generate BOM file."""
        bom_path = self.get_bom_csv_path()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {fp.GetReference(): fp for fp in self.board.Footprints()}
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
                    if fp is None:
                        self.logger.info(
                            "Component %s is no longer on the board: removing from BOM",
                            component,
                        )
                        continue
                    if get_is_dnp(fp):
                        self.logger.info(
                            "Component %s has 'Do not place' enabled: removing from BOM",
                            component,
                        )
                        continue
                    components.append(component)
                if not components:
                    continue
                for chunk in split_bom_designators(components):
                    writer.writerow(
                        [
                            part["value"],
                            ",".join(chunk),
                            part["footprint"],
                            part["lcsc"],
                            len(chunk),
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
