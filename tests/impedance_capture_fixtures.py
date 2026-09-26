"""Reproducible physical PCB examples shared by framing tests and the gallery."""

from dataclasses import dataclass
from math import hypot
from uuid import NAMESPACE_URL, uuid5

from impedance.model import Bounds, Point, Section, Trace

NM_PER_MM = 1_000_000
BOARD_BOUNDS: Bounds = (20_000_000, 20_000_000, 270_000_000, 260_000_000)


def _points(*coordinates: tuple[float, float]) -> tuple[Point, ...]:
    """Convert exact fixture dimensions from millimetres to board nanometres."""
    return tuple((round(x * NM_PER_MM), round(y * NM_PER_MM)) for x, y in coordinates)


def _uuid(label: str) -> str:
    """Keep generated PCB object identities stable across gallery runs."""
    return str(uuid5(NAMESPACE_URL, f"jlcpcb-tools/impedance-captures/{label}"))


def _mm(value: int) -> str:
    """Serialize board coordinates without binary floating-point noise."""
    return f"{value / NM_PER_MM:.6f}"


@dataclass(frozen=True)
class CaptureExample:
    """One complete connected route on a 250 by 240 mm context board."""

    name: str
    title: str
    points: tuple[Point, ...]
    paired: bool = False
    width_nm: int = 150_000

    @property
    def length_mm(self) -> float:
        """Return centerline length, not the enclosing bounding-box diagonal."""
        return (
            sum(
                hypot(end[0] - start[0], end[1] - start[1])
                for start, end in zip(self.points, self.points[1:])
            )
            / NM_PER_MM
        )

    def traces(self) -> tuple[Trace, ...]:
        """Model actual separate KiCad track items with shared endpoints."""
        result = []
        for pair_index in range(2 if self.paired else 1):
            offset = pair_index * 350_000
            net = "SIGNAL_P" if pair_index == 0 else "SIGNAL_N"
            for index, (start, end) in enumerate(zip(self.points, self.points[1:])):
                result.append(
                    Trace(
                        _uuid(f"{self.name}/{net}/{index}"),
                        "F.Cu",
                        net,
                        self.width_nm,
                        ((start[0], start[1] + offset), (end[0], end[1] + offset)),
                    )
                )
        return tuple(result)

    def section(self) -> Section:
        """Create the expected complete-row geometry independently of matching."""
        traces = self.traces()
        points = tuple(point for trace in traces for point in trace.points)
        radius = (self.width_nm + 1) // 2
        return Section(
            self.name,
            self.name,
            "F.Cu",
            self.width_nm,
            traces,
            (
                min(x for x, _ in points) - radius,
                min(y for _, y in points) - radius,
                max(x for x, _ in points) + radius,
                max(y for _, y in points) + radius,
            ),
            tuple(sorted({trace.net for trace in traces})),
        )


EXAMPLES = (
    CaptureExample("short-2mm", "Short route: 2 mm", _points((225, 225), (227, 225))),
    CaptureExample(
        "horizontal-100mm", "Horizontal route: 100 mm", _points((45, 80), (145, 80))
    ),
    CaptureExample(
        "horizontal-200mm", "Horizontal route: 200 mm", _points((40, 120), (240, 120))
    ),
    CaptureExample(
        "vertical-200mm", "Vertical route: 200 mm", _points((120, 40), (120, 240))
    ),
    CaptureExample(
        "diagonal-200mm", "Diagonal route: 200 mm", _points((80, 70), (240, 190))
    ),
    CaptureExample(
        "meander-240mm",
        "Meandering route: 240 mm",
        _points(
            (40, 100),
            (100, 100),
            (100, 115),
            (55, 115),
            (55, 130),
            (105, 130),
            (105, 145),
            (65, 145),
        ),
    ),
    CaptureExample(
        "differential-150mm",
        "Differential pair: 150 mm per net",
        _points((55, 180), (205, 180)),
        paired=True,
    ),
)


def _landmarks() -> list[str]:
    """Provide recognizable mounting holes, pads, labels and ordinary routing."""
    items = []
    for index, (x, y) in enumerate(((28, 28), (262, 28), (262, 252), (28, 252))):
        items.append(
            f'(footprint "MountingHole" (layer "F.Cu") (at {x} {y}) '
            f'(uuid "{_uuid(f"mount/{index}")}") '
            '(pad "" thru_hole circle (at 0 0) (size 6 6) (drill 3) '
            '(layers "*.Cu" "*.Mask")))'
        )
    for ref, x, y in (("U1", 62, 54), ("U2", 205, 222), ("J1", 230, 60)):
        pads = " ".join(
            f'(pad "{row * 4 + column + 1}" thru_hole rect '
            f"(at {column * 2.54} {row * 7.62}) (size 1.7 2) (drill 0.8) "
            '(layers "*.Cu" "*.Mask"))'
            for row in range(2)
            for column in range(4)
        )
        items.append(
            f'(footprint "CaptureLandmark:{ref}" (layer "F.Cu") (at {x} {y}) '
            f'(uuid "{_uuid(ref)}") {pads})'
        )
        items.append(
            f'(gr_text "{ref}" (at {x + 4} {y - 4}) (layer "F.Cu") '
            "(effects (font (size 2.2 2.2) (thickness 0.25))))"
        )
    for index, (start, end) in enumerate(
        (
            ((65, 60), (65, 85)),
            ((65, 85), (175, 85)),
            ((175, 85), (175, 215)),
            ((210, 215), (235, 215)),
            ((235, 215), (235, 68)),
        )
    ):
        items.append(
            f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
            f'(width 0.5) (layer "F.Cu") (net 0) (uuid "{_uuid(f"context/{index}")}"))'
        )
    items.append(
        '(gr_text "TOP / CONNECTORS" (at 145 33) (layer "F.Cu") '
        "(effects (font (size 2.4 2.4) (thickness 0.25))))"
    )
    return items


def board_text(example: CaptureExample) -> str:
    """Generate a real KiCad PCB fixture, not pre-rendered SVG illustrations."""
    items = [
        '(kicad_pcb (version 20240108) (generator "pcbnew")',
        '(general (thickness 1.6)) (paper "A3")',
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user "f.silkscreen") (39 "F.Mask" user) (44 "Edge.Cuts" user))',
        "(setup (pad_to_mask_clearance 0))",
        '(net 0 "") (net 1 "SIGNAL_P") (net 2 "SIGNAL_N")',
        '(gr_rect (start 20 20) (end 270 260) (stroke (width 0.2) (type default)) (fill none) (layer "Edge.Cuts"))',
        *_landmarks(),
    ]
    for trace in example.traces():
        start, end = trace.points
        items.append(
            f"(segment (start {_mm(start[0])} {_mm(start[1])}) "
            f"(end {_mm(end[0])} {_mm(end[1])}) (width {_mm(trace.width_nm)}) "
            f'(layer "F.Cu") (net {1 if trace.net == "SIGNAL_P" else 2}) '
            f'(uuid "{trace.trace_id}"))'
        )
    return "\n".join([*items, ")", ""])
