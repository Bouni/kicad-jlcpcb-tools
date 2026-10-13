"""Write the fixture boards that the golden fabrication tests export.

Run it with KiCad's own Python from the repository root, once under KiCad 9
and once under KiCad 10:

    python3 tests/golden/make_boards.py

KiCad 10 opens a KiCad 9 board but KiCad 9 cannot open a KiCad 10 one, so
KiCad 9 writes every board both versions test, and KiCad 10 writes only the
design-variant board. Each board's UUIDs come from a fixed seed, so an
unchanged script writes the same files again. Footprints are drawn here and
saved inside each board, so the tests need no footprint libraries.

After changing a board, regenerate the expected outputs as described in
tests/test_golden_outputs.py.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import pcbnew

GOLDEN = Path(__file__).resolve().parent

# The board's top-left corner on KiCad's canvas, in millimetres.
LEFT, TOP = 100.0, 100.0


@dataclass(frozen=True)
class Pad:
    """One pad of a footprint drawing, relative to its origin, in millimetres."""

    number: str
    kind: str  # "smd", "tht" or "npth"
    x: float
    y: float
    width: float
    height: float
    shape: str = "roundrect"
    drill: float = 0.0


def _smd_pair(pitch: float, width: float, height: float) -> tuple[Pad, ...]:
    """Return a chip's two pads, either side of the origin."""
    return (
        Pad("1", "smd", -pitch / 2, 0, width, height),
        Pad("2", "smd", pitch / 2, 0, width, height),
    )


FOOTPRINTS: dict[str, tuple[Pad, ...]] = {
    "R_0603_1608Metric": _smd_pair(1.65, 0.8, 0.95),
    "C_0603_1608Metric": _smd_pair(1.65, 0.8, 0.95),
    "C_0805_2012Metric": _smd_pair(1.9, 1.0, 1.45),
    "LED_0603_1608Metric": _smd_pair(1.575, 0.875, 0.95),
    "SOT-23": (
        Pad("1", "smd", -0.95, 1.1, 0.6, 1.0, "rect"),
        Pad("2", "smd", 0.95, 1.1, 0.6, 1.0, "rect"),
        Pad("3", "smd", 0, -1.1, 0.6, 1.0, "rect"),
    ),
    "SOIC-8_3.9x4.9mm_P1.27mm": tuple(
        Pad(str(index + 1), "smd", -2.475, -1.905 + index * 1.27, 1.95, 0.6)
        for index in range(4)
    )
    + tuple(
        Pad(str(8 - index), "smd", 2.475, -1.905 + index * 1.27, 1.95, 0.6)
        for index in range(4)
    ),
    # KiCad puts a header's origin on pin 1, away from the middle of its pads.
    "PinHeader_1x03_P2.54mm_Vertical": (
        Pad("1", "tht", 0, 0, 1.7, 1.7, "rect", 1.0),
        Pad("2", "tht", 0, 2.54, 1.7, 1.7, "oval", 1.0),
        Pad("3", "tht", 0, 5.08, 1.7, 1.7, "oval", 1.0),
    ),
    "PinHeader_1x02_P2.54mm_Vertical": (
        Pad("1", "tht", 0, 0, 1.7, 1.7, "rect", 1.0),
        Pad("2", "tht", 0, 2.54, 1.7, 1.7, "oval", 1.0),
    ),
    "CP_Radial_D5.0mm_P2.00mm": (
        Pad("1", "tht", 0, 0, 1.6, 1.6, "rect", 0.8),
        Pad("2", "tht", 2.0, 0, 1.6, 1.6, "circle", 0.8),
    ),
    "TestPoint_Pad_D1.0mm": (Pad("1", "smd", 0, 0, 1.0, 1.0, "circle"),),
    "MountingHole_3.2mm_M3": (Pad("", "npth", 0, 0, 3.2, 3.2, "circle", 3.2),),
    # Soldered pads around the origin, with two locating pegs on one side.
    "Jack_3.5mm_SMD_Pegs": (
        Pad("1", "smd", -3.0, -2.0, 1.6, 1.2, "rect"),
        Pad("2", "smd", 3.0, -2.0, 1.6, 1.2, "rect"),
        Pad("3", "smd", -3.0, 2.0, 1.6, 1.2, "rect"),
        Pad("4", "smd", 3.0, 2.0, 1.6, 1.2, "rect"),
        Pad("", "npth", -1.5, 4.5, 1.2, 1.2, "circle", 1.2),
        Pad("", "npth", 1.5, 4.5, 1.2, 1.2, "circle", 1.2),
    ),
}

_PAD_SHAPES = {
    "rect": "PAD_SHAPE_RECT",
    "roundrect": "PAD_SHAPE_ROUNDRECT",
    "oval": "PAD_SHAPE_OVAL",
    "circle": "PAD_SHAPE_CIRCLE",
}


@dataclass(frozen=True)
class Part:
    """One placed footprint: what the plugin reads and where it sits."""

    reference: str
    value: str
    footprint: str
    x: float
    y: float
    rotation: float = 0.0
    lcsc: str = ""
    back: bool = False
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    nets: tuple[str, ...] = ()


def mm(value: float) -> int:
    """Convert millimetres to KiCad's internal units."""
    return pcbnew.FromMM(value)


def point(x: float, y: float) -> Any:
    """Return a board position, relative to the board's top-left corner."""
    return pcbnew.VECTOR2I(mm(LEFT + x), mm(TOP + y))


def offset(x: float, y: float) -> Any:
    """Return a vector in millimetres."""
    return pcbnew.VECTOR2I(mm(x), mm(y))


class BoardWriter:
    """Build one board in memory, then save it beside this script."""

    def __init__(self, name: str, width: float, height: float, copper: int) -> None:
        self.name = name
        self.board = pcbnew.BOARD()
        self.board.SetCopperLayerCount(copper)
        self.nets: dict[str, Any] = {}
        self._outline(width, height)
        # The CPL and the plots are measured from the bottom-left corner.
        self.board.GetDesignSettings().SetAuxOrigin(point(0, height))

    def _outline(self, width: float, height: float) -> None:
        """Draw the board edge."""
        shape = pcbnew.PCB_SHAPE(self.board)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetStart(point(0, 0))
        shape.SetEnd(point(width, height))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(mm(0.1))
        self.board.Add(shape)

    def net(self, name: str) -> Any:
        """Return the board's net of that name, adding it the first time."""
        if name not in self.nets:
            item = pcbnew.NETINFO_ITEM(self.board, name)
            self.board.Add(item)
            self.nets[name] = item
        return self.nets[name]

    def part(self, part: Part) -> Any:
        """Place one footprint with its fields, pads and silkscreen outline."""
        footprint = pcbnew.FOOTPRINT(self.board)
        footprint.SetFPID(pcbnew.LIB_ID("Golden", part.footprint))
        footprint.SetReference(part.reference)
        footprint.SetValue(part.value)
        pads = FOOTPRINTS[part.footprint]
        attributes = (
            pcbnew.FP_THROUGH_HOLE
            if any(pad.kind == "tht" for pad in pads)
            else pcbnew.FP_SMD
        )
        if part.exclude_from_bom:
            attributes |= pcbnew.FP_EXCLUDE_FROM_BOM
        if part.exclude_from_pos:
            attributes |= pcbnew.FP_EXCLUDE_FROM_POS_FILES
        footprint.SetAttributes(attributes)
        footprint.SetDNP(part.dnp)
        if part.lcsc:
            footprint.SetField("LCSC", part.lcsc)
            field = next(
                item for item in footprint.GetFields() if item.GetName() == "LCSC"
            )
            field.SetVisible(False)
            field.SetLayer(pcbnew.F_Fab)
        nets = iter(part.nets)
        for pad in pads:
            self._pad(footprint, pad, "" if pad.kind == "npth" else next(nets, ""))
        self._fields_and_outline(footprint, pads)
        self.board.Add(footprint)
        footprint.SetPosition(point(part.x, part.y))
        footprint.SetOrientationDegrees(part.rotation)
        if part.back:
            footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        return footprint

    def _pad(self, footprint: Any, pad: Pad, net: str) -> None:
        """Add one pad while the footprint still sits at the canvas origin."""
        item = pcbnew.PAD(footprint)
        item.SetNumber(pad.number)
        item.SetShape(getattr(pcbnew, _PAD_SHAPES[pad.shape]))
        item.SetSize(offset(pad.width, pad.height))
        if pad.shape == "roundrect":
            item.SetRoundRectRadiusRatio(0.25)
        if pad.kind == "smd":
            item.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            item.SetLayerSet(pcbnew.PAD.SMDMask())
        else:
            item.SetAttribute(
                pcbnew.PAD_ATTRIB_PTH if pad.kind == "tht" else pcbnew.PAD_ATTRIB_NPTH
            )
            item.SetLayerSet(
                pcbnew.PAD.PTHMask()
                if pad.kind == "tht"
                else pcbnew.PAD.UnplatedHoleMask()
            )
            item.SetDrillSize(offset(pad.drill, pad.drill))
        item.SetPosition(offset(pad.x, pad.y))
        if net:
            item.SetNet(self.net(net))
        footprint.Add(item)

    @staticmethod
    def _fields_and_outline(footprint: Any, pads: tuple[Pad, ...]) -> None:
        """Put the reference on the silkscreen above a box around the pads."""
        left = min(pad.x - pad.width / 2 for pad in pads) - 0.3
        right = max(pad.x + pad.width / 2 for pad in pads) + 0.3
        top = min(pad.y - pad.height / 2 for pad in pads) - 0.3
        bottom = max(pad.y + pad.height / 2 for pad in pads) + 0.3
        reference = footprint.Reference()
        reference.SetLayer(pcbnew.F_SilkS)
        reference.SetTextSize(offset(0.8, 0.8))
        reference.SetTextThickness(mm(0.12))
        reference.SetPosition(offset((left + right) / 2, top - 0.8))
        value = footprint.Value()
        value.SetLayer(pcbnew.F_Fab)
        value.SetTextSize(offset(0.6, 0.6))
        value.SetTextThickness(mm(0.1))
        value.SetPosition(offset((left + right) / 2, bottom + 0.6))
        outline = pcbnew.PCB_SHAPE(footprint)
        outline.SetShape(pcbnew.SHAPE_T_RECT)
        outline.SetStart(offset(left, top))
        outline.SetEnd(offset(right, bottom))
        outline.SetLayer(pcbnew.F_SilkS)
        outline.SetWidth(mm(0.12))
        footprint.Add(outline)

    def text(self, text: str, x: float, y: float, layer: int) -> None:
        """Add board text, such as the JLC order-number placeholder."""
        item = pcbnew.PCB_TEXT(self.board)
        item.SetText(text)
        item.SetLayer(layer)
        item.SetPosition(point(x, y))
        item.SetTextSize(offset(1.0, 1.0))
        item.SetTextThickness(mm(0.15))
        self.board.Add(item)

    def track(self, net: str, layer: int, *points: tuple[float, float]) -> None:
        """Route a net along straight segments."""
        for start, end in zip(points, points[1:]):
            item = pcbnew.PCB_TRACK(self.board)
            item.SetStart(point(*start))
            item.SetEnd(point(*end))
            item.SetWidth(mm(0.25))
            item.SetLayer(layer)
            item.SetNet(self.net(net))
            self.board.Add(item)

    def via(self, net: str, x: float, y: float) -> None:
        """Add a through via."""
        item = pcbnew.PCB_VIA(self.board)
        item.SetViaType(pcbnew.VIATYPE_THROUGH)
        item.SetPosition(point(x, y))
        item.SetWidth(mm(0.6))
        item.SetDrill(mm(0.3))
        item.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        item.SetNet(self.net(net))
        self.board.Add(item)

    def zone(self, net: str, layer: int, *corners: tuple[float, float]) -> None:
        """Add a copper pour, saved unfilled so that export must fill it."""
        item = pcbnew.ZONE(self.board)
        item.SetLayer(layer)
        item.SetNet(self.net(net))
        outline = item.Outline()
        outline.NewOutline()
        for x, y in corners:
            corner = point(x, y)
            outline.Append(corner.x, corner.y)
        self.board.Add(item)

    def save(self) -> Path:
        """Write the board file into its own directory."""
        directory = GOLDEN / self.name
        directory.mkdir(exist_ok=True)
        path = directory / f"{self.name}.kicad_pcb"
        self.board.SetFileName(str(path))
        pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, str(path), self.board)
        return path


def two_layer() -> BoardWriter:
    """Mix SMD and THT on both sides, with every exclusion and unfilled pours."""
    writer = BoardWriter("two_layer", 50, 36, 2)
    for part in (
        # Three identical resistors share one BOM row.
        Part("R1", "10k", "R_0603_1608Metric", 8, 8, 0, "C25804", nets=("A", "GND")),
        Part("R2", "10k", "R_0603_1608Metric", 8, 13, 90, "C25804", nets=("A", "B")),
        Part("R3", "10k", "R_0603_1608Metric", 13, 8, 37.5, "C25804"),
        Part("R4", "4k7", "R_0603_1608Metric", 18, 8, 180, "C23162", nets=("B",)),
        Part("R5", "1k", "R_0603_1608Metric", 23, 8, 0, "C21190", dnp=True),
        Part("C1", "100n", "C_0603_1608Metric", 13, 14, 270, "C14663"),
        Part("C2", "100n", "C_0603_1608Metric", 8, 22, 0, "C14663", back=True),
        Part("C3", "10u", "C_0805_2012Metric", 14, 22, 123.4, "C15850", back=True),
        Part("Q1", "MMBT3904", "SOT-23", 20, 22, 90, "C20526", back=True),
        Part("U1", "LM358", "SOIC-8_3.9x4.9mm_P1.27mm", 30, 12, 90, "C7950"),
        Part("D1", "LED", "LED_0603_1608Metric", 24, 14, 45),
        Part(
            "J1",
            "Conn_01x03",
            "PinHeader_1x03_P2.54mm_Vertical",
            40,
            6,
            90,
            "C2337",
            nets=("GND", "A", "B"),
        ),
        Part("J2", "Jack", "Jack_3.5mm_SMD_Pegs", 38, 24, 180, "C431535"),
        Part(
            "C4",
            "47u",
            "CP_Radial_D5.0mm_P2.00mm",
            28,
            28,
            0,
            "C43350",
            exclude_from_pos=True,
            nets=("A", "GND"),
        ),
        Part("TP1", "TP", "TestPoint_Pad_D1.0mm", 4, 30, 0, exclude_from_bom=True),
        Part(
            "H1",
            "MountingHole",
            "MountingHole_3.2mm_M3",
            46,
            32,
            exclude_from_bom=True,
            exclude_from_pos=True,
        ),
    ):
        writer.part(part)
    writer.text("JLCJLCJLCJLC", 12, 32, pcbnew.F_SilkS)
    writer.track("A", pcbnew.F_Cu, (7.175, 8), (7.175, 12.175))
    writer.track("B", pcbnew.F_Cu, (8, 13.825), (17.175, 13.825), (17.175, 8))
    writer.via("GND", 10, 8)
    writer.track("GND", pcbnew.F_Cu, (8.825, 8), (10, 8))
    writer.zone("GND", pcbnew.B_Cu, (0.5, 0.5), (49.5, 0.5), (49.5, 35.5), (0.5, 35.5))
    writer.zone("GND", pcbnew.F_Cu, (30, 20), (49, 20), (49, 35), (30, 35))
    return writer


def four_layer() -> BoardWriter:
    """Pour both inner layers, with parts on both sides and a JLC_ layer."""
    writer = BoardWriter("four_layer", 30, 24, 4)
    for part in (
        Part("U1", "NE5532", "SOIC-8_3.9x4.9mm_P1.27mm", 10, 10, 0, "C7426"),
        Part(
            "C1", "100n", "C_0603_1608Metric", 17, 6, 90, "C14663", nets=("VCC", "GND")
        ),
        Part(
            "C2",
            "100n",
            "C_0603_1608Metric",
            17,
            14,
            180,
            "C14663",
            nets=("VCC", "GND"),
        ),
        Part("R1", "22k", "R_0603_1608Metric", 10, 18, 270, "C31850", back=True),
        Part("R2", "22k", "R_0603_1608Metric", 14, 18, 15, "C31850", back=True),
        Part(
            "J1",
            "Conn_01x02",
            "PinHeader_1x02_P2.54mm_Vertical",
            25,
            8,
            0,
            "C49257",
            nets=("VCC", "GND"),
        ),
    ):
        writer.part(part)
    writer.via("GND", 20, 6)
    writer.via("VCC", 20, 14)
    writer.track("GND", pcbnew.F_Cu, (17, 6.825), (20, 6.825), (20, 6))
    writer.track("VCC", pcbnew.F_Cu, (17.825, 14), (20, 14))
    writer.zone(
        "GND", pcbnew.In1_Cu, (0.5, 0.5), (29.5, 0.5), (29.5, 23.5), (0.5, 23.5)
    )
    writer.zone(
        "VCC", pcbnew.In2_Cu, (0.5, 0.5), (29.5, 0.5), (29.5, 23.5), (0.5, 23.5)
    )
    board = writer.board
    layers = board.GetEnabledLayers()
    layers.AddLayer(pcbnew.User_1)
    board.SetEnabledLayers(layers)
    board.SetLayerName(pcbnew.User_1, "JLC_Notes")
    writer.text("Panelize 2x2", 4, 22, pcbnew.User_1)
    return writer


def repeated_references() -> BoardWriter:
    """Give two footprints one reference, which generation must refuse."""
    writer = BoardWriter("repeated_references", 20, 12, 2)
    writer.part(Part("R1", "10k", "R_0603_1608Metric", 5, 5, 0, "C25804"))
    writer.part(Part("R1", "10k", "R_0603_1608Metric", 10, 5, 0, "C25804"))
    writer.part(Part("C1", "100n", "C_0603_1608Metric", 15, 5, 0, "C14663"))
    return writer


def variants() -> BoardWriter:
    """Vary population, values and exclusions across two design variants."""
    writer = BoardWriter("variants", 30, 20, 2)
    placed = {
        part.reference: writer.part(part)
        for part in (
            Part("R1", "10k", "R_0603_1608Metric", 6, 6, 0, "C25804"),
            Part("R2", "10k", "R_0603_1608Metric", 6, 11, 90, "C25804"),
            Part("C1", "100n", "C_0603_1608Metric", 14, 6, 0, "C14663"),
            Part("Q1", "MMBT3904", "SOT-23", 14, 12, 180, "C20526", back=True),
            Part(
                "J1",
                "Conn_01x02",
                "PinHeader_1x02_P2.54mm_Vertical",
                24,
                6,
                90,
                "C49257",
            ),
        )
    }
    board = writer.board
    for name, description in (("Lite", "Fewer parts"), ("Full", "Changed values")):
        board.AddVariant(name)
        board.SetVariantDescription(name, description)
    lite = placed["R2"].AddVariant("Lite")
    lite.SetDNP(True)
    lite = placed["C1"].AddVariant("Lite")
    lite.SetFieldValue("Value", "1u")
    lite.SetFieldValue("LCSC", "C15849")
    lite = placed["J1"].AddVariant("Lite")
    lite.SetExcludedFromPosFiles(True)
    full = placed["R1"].AddVariant("Full")
    full.SetFieldValue("Value", "4k7")
    full.SetFieldValue("LCSC", "C23162")
    full = placed["Q1"].AddVariant("Full")
    full.SetExcludedFromBOM(True)
    writer.text("${VARIANT}", 6, 17, pcbnew.F_SilkS)
    return writer


# Each board, the KiCad major that writes it, and its UUID seed.
BOARDS: tuple[tuple[Callable[[], BoardWriter], int, int], ...] = (
    (two_layer, 9, 1),
    (four_layer, 9, 2),
    (repeated_references, 9, 3),
    (variants, 10, 4),
)


def main() -> int:
    """Write the boards the running KiCad is responsible for."""
    major = int(str(pcbnew.GetMajorMinorVersion()).split(".")[0])
    written = []
    for build, writer_major, seed in BOARDS:
        if writer_major != major:
            continue
        pcbnew.KIID.SeedGenerator(seed)
        written.append(build().save())
    for path in written:
        sys.stdout.write(f"wrote {path.relative_to(GOLDEN.parent.parent)}\n")
    if not written:
        sys.stderr.write(f"KiCad {major} writes none of the golden boards\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
