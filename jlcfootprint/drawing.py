"""Polarity marks and the body outline read from EasyEDA drawings.

No EasyEDA record carries a pad's polarity as metadata.  What the data carries is
the drawing: the symbol draws a ``+`` beside its positive pin and the footprint
draws a ``+`` beside its positive pad (a diode's anode, a capacitor's positive
terminal) on the silkscreen or the document layer, as two short bars crossing at
their midpoints.  JLC's placement preview places polarized two-pad parts by
these marks: where a tantalum's footprint numbers its terminals the other way
round, the package name's FD/RD token is wrong and the marks are right.

Both drawing formats are read.  Pro records are JSON arrays in mils with Y up;
classic shapes are ``~``-separated strings in canvas units (10 mil) with Y down.
A mark is found in its drawing's own frame, against that drawing's own pins or
pads, so it needs no unit conversion; only the footprint's absolute floor on a
bar's length is converted into each format's units.  The body box is converted to
millimetres in the footprint's pad frame, the frame of ``geometry.Pad`` (KiCad's
Y down, origin at the footprint origin).  Stdlib only, pure functions; malformed
records are skipped, never raised.
"""

from __future__ import annotations

from collections.abc import Hashable
import json
import math
import re
from typing import Any, NamedTuple, Optional

from .easyeda_parse import (
    PRO_ARC_TOKENS,
    PRO_MILS_PER_CANVAS_UNIT,
    classic_pin_records,
    pro_pin_records,
    pro_points_straight,
)
from .geometry import EASYEDA_UNIT_MM, normal_number
from .records import DrawingMarks

# Drawing layers that carry polarity marks, by format: the top silkscreen and the
# document layer (Pro numbers 3 and 13, classic numbers 3 and 12).
_PRO_MARK_LAYERS = {3: "silk", 13: "document"}
_CLASSIC_MARK_LAYERS = {"3": "silk", "12": "document"}
# The component body outline: Pro layer 48 (COMPONENT_SHAPE), classic layer 99.
_PRO_BODY_LAYER = 48
_CLASSIC_BODY_LAYER = "99"

# A bar is a filled rectangle at least this many times longer than wide, or a
# two-point stroke.
_BAR_ASPECT = 2.5
# The two bars of a ``+`` cross within this fraction of each bar's length from its
# midpoint (a ``T`` or an outline corner meets at an end and is rejected).
_MIDPOINT_TOLERANCE = 0.35
# The bars of one ``+`` are alike: neither is longer than this many times the other.
_LENGTH_RATIO = 2.5
# Bar lengths as a fraction of the pin span (symbol) or the pad spacing (footprint):
# a capacitor's plates and a footprint's outline are longer, pin stubs shorter.
_SYMBOL_BAR_RANGE = (0.05, 0.45)
_FOOTPRINT_BAR_RANGE = (0.05, 0.70)
# The footprint's lower bound is never longer than this, in millimetres, so a + of
# ordinary size beside the pads of a large part (a battery holder's, pads tens of
# millimetres apart) is still read: 5 % of that spacing would be longer than the
# mark.  Measured over 13,257 two-pad footprint drawings from EasyEDA's library,
# every + the 5 % bound drops is at least 1.016 mm (40 mil) long, on pads 28.8 to
# 86.1 mm apart, so any floor up to that reads the same 26 drawings; 0.75 mm sits a
# quarter below it and still reads the 30-mil (0.762 mm) + that parts 8 to 15 mm
# apart often carry.
_FOOTPRINT_BAR_FLOOR_MM = 0.75
# The crossing sits off the centre by at least this fraction of the pad spacing, so
# a centre cross or a diode's silk symbol never names a pad.
_OFF_CENTRE = 0.15
# No third bar comes closer to the crossing than this fraction of the shorter bar.
_CLEARANCE = 0.25
# The nearer pin is at least this much nearer than the other.
_NEARER_RATIO = 1.2


class Segment(NamedTuple):
    """A bar's centreline (or a stroke) in the drawing's own units, with its layer."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: str

    @property
    def horizontal(self) -> bool:
        """Return True for a bar along X."""
        return abs(self.y1 - self.y2) < 1e-9

    @property
    def vertical(self) -> bool:
        """Return True for a bar along Y."""
        return abs(self.x1 - self.x2) < 1e-9

    @property
    def length(self) -> float:
        """Return the bar's length."""
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def midpoint(self) -> tuple[float, float]:
        """Return the bar's midpoint."""
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


# ---------------------------------------------------------------------------
# Reading records
# ---------------------------------------------------------------------------


def is_pro(shapes: list[Any]) -> bool:
    """Return True when the drawing is in the Pro text form (JSON array records)."""
    return any(
        isinstance(s, list) or (isinstance(s, str) and s.lstrip().startswith("["))
        for s in shapes
    )


def _pro_records(shapes: list[Any]) -> list[list]:
    """Return the Pro records that parse, in order; an empty array is no record."""
    records: list[list] = []
    for shape in shapes:
        if isinstance(shape, list):
            if shape:
                records.append(shape)
            continue
        if not isinstance(shape, str) or not shape.lstrip().startswith("["):
            continue
        try:
            record = json.loads(shape)
        except ValueError:
            continue
        if isinstance(record, list) and record:
            records.append(record)
    return records


def _numbers(values: Any) -> list[float]:
    """Return the numeric entries of a point list, skipping ``L`` and other tokens."""
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append(float(value))
    return out


def _box_of(numbers: list[float]) -> Optional[tuple[float, float, float, float]]:
    """Return the bounding box of x/y pairs, or None for fewer than two points."""
    if len(numbers) < 4:
        return None
    xs = numbers[0::2]
    ys = numbers[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _bar_from_box(
    box: tuple[float, float, float, float], layer: str
) -> Optional[Segment]:
    """Return the centreline of a filled box when it is a thin bar, else None."""
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    if width <= 0 and height <= 0:
        return None
    if height >= _BAR_ASPECT * max(width, 1e-9):
        x = (x1 + x2) / 2.0
        return Segment(x, y1, x, y2, layer)
    if width >= _BAR_ASPECT * max(height, 1e-9):
        y = (y1 + y2) / 2.0
        return Segment(x1, y, x2, y, layer)
    return None


def _stroke(numbers: list[float], layer: str) -> Optional[Segment]:
    """Return a two-point stroke of any angle as a segment, else None.

    Only axis-aligned strokes can form a ``+``; the others still count as
    neighbours in the crowding test (a diode's silk arrow is drawn with diagonals).
    """
    if len(numbers) != 4:
        return None
    segment = Segment(numbers[0], numbers[1], numbers[2], numbers[3], layer)
    return segment if segment.length > 0 else None


def _edges_between(
    corners: list[Optional[tuple[float, float]]], layer: str
) -> list[Segment]:
    """Return the segments between consecutive corners; None breaks the chain."""
    return [
        Segment(a[0], a[1], b[0], b[1], layer)
        for a, b in zip(corners, corners[1:])
        if a is not None and b is not None and a != b
    ]


def _pro_edges(points: Any, layer: str) -> list[Segment]:
    """Return the straight edges of a Pro point list; the chain breaks at an arc.

    Only straight edges matter here (they are what a diode's silk arrow is drawn
    with); a circle record gives none and an arc's sweep is left out.
    """
    if not isinstance(points, list) or (points and points[0] == "CIRCLE"):
        return []
    corners: list[Optional[tuple[float, float]]] = []
    index = 0
    skip_angle = False
    while index < len(points):
        value = points[index]
        if isinstance(value, str):
            if value.upper() in PRO_ARC_TOKENS:
                skip_angle = True
                corners.append(None)
            index += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or skip_angle:
            skip_angle = False
            index += 1
            continue
        partner = points[index + 1] if index + 1 < len(points) else None
        if isinstance(partner, bool) or not isinstance(partner, (int, float)):
            index += 1
            continue
        corners.append((float(value), float(partner)))
        index += 2
    return _edges_between(corners, layer)


def _edges_of(numbers: list[float], layer: str) -> list[Segment]:
    """Return the edges of a flat ``x y x y ...`` point list."""
    corners: list[Optional[tuple[float, float]]] = list(
        zip(numbers[0::2], numbers[1::2])
    )
    return _edges_between(corners, layer)


_PATH_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _classic_path_numbers(path: str) -> Optional[list[float]]:
    """Return the points of a classic ``M x y L x y ... Z`` path, or None when it has arcs.

    Commands and numbers may be run together (``L3996.0158,3002.1968``), so the
    path is tokenised rather than split on spaces.
    """
    numbers: list[float] = []
    for token in _PATH_TOKEN.findall(path):
        upper = token.upper()
        if upper in ("M", "L", "Z"):
            continue
        if upper.isalpha():
            return None
        numbers.append(float(token))
    return numbers


def _classic_fields(shape: str) -> list[str]:
    return shape.split("~")


# ---------------------------------------------------------------------------
# Footprint: pads, bars and the body
# ---------------------------------------------------------------------------


def footprint_pads(shapes: list[Any]) -> list[tuple[str, float, float]]:
    """Return ``(number, x, y)`` per pad in the drawing's own frame and units.

    Numbers lose their leading zeros as the pad parser's do, so a ``+`` beside
    pad ``01`` names pad ``1``, the pad the rest of the check knows.
    """
    pads: list[tuple[str, float, float]] = []
    if is_pro(shapes):
        for record in _pro_records(shapes):
            if record[0] != "PAD" or len(record) < 8:
                continue
            try:
                pads.append(
                    (
                        normal_number(str(record[5]).strip()),
                        float(record[6]),
                        float(record[7]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return pads
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith("PAD~"):
            continue
        parts = _classic_fields(shape)
        if len(parts) < 9:
            continue
        try:
            pads.append(
                (normal_number(parts[8].strip()), float(parts[2]), float(parts[3]))
            )
        except ValueError:
            continue
    return pads


def _footprint_primitives(shapes: list[Any]) -> tuple[list[Segment], list[Segment]]:
    """Return the bars and the other straight edges drawn on the silk and document layers.

    A record is either a bar (a two-point stroke or a thin filled rectangle) or
    a source of edges (a longer polyline, a wider filled region); the edges only
    crowd a ``+`` (see :func:`plus_marks`), they never form one.
    """
    bars: list[Segment] = []
    edges: list[Segment] = []
    if is_pro(shapes):
        for record in _pro_records(shapes):
            if (
                len(record) < 5
                or not isinstance(record[4], Hashable)
                or record[4] not in _PRO_MARK_LAYERS
            ):
                continue
            layer = _PRO_MARK_LAYERS[record[4]]
            if record[0] == "POLY" and len(record) > 6:
                bar = _stroke(_numbers(record[6]), layer)
                if bar is None:
                    edges.extend(_pro_edges(record[6], layer))
            elif record[0] == "FILL" and len(record) > 7:
                rings = record[7] if isinstance(record[7], list) else []
                nested = bool(rings) and isinstance(rings[0], list)
                points = rings[0] if nested else rings
                # Only a straight ring is boxed: an arc's sweep angle would be read
                # as a coordinate.  A ring with an arc still gives its straight edges.
                box = _box_of(_numbers(points)) if pro_points_straight(points) else None
                bar = _bar_from_box(box, layer) if box else None
                if bar is None:
                    for ring in rings if nested else [rings]:
                        edges.extend(_pro_edges(ring, layer))
            else:
                bar = None
            if bar is not None:
                bars.append(bar)
        return bars, edges
    for shape in shapes:
        if not isinstance(shape, str):
            continue
        parts = _classic_fields(shape)
        bar = None
        if parts[0] == "TRACK" and len(parts) > 4 and parts[2] in _CLASSIC_MARK_LAYERS:
            layer = _CLASSIC_MARK_LAYERS[parts[2]]
            try:
                numbers = [float(v) for v in parts[4].split()]
            except ValueError:
                continue
            bar = _stroke(numbers, layer)
            if bar is None:
                edges.extend(_edges_of(numbers, layer))
        elif (
            parts[0] == "SOLIDREGION"
            and len(parts) > 3
            and parts[1] in _CLASSIC_MARK_LAYERS
        ):
            layer = _CLASSIC_MARK_LAYERS[parts[1]]
            numbers = _classic_path_numbers(parts[3])
            box = _box_of(numbers) if numbers else None
            bar = _bar_from_box(box, layer) if box else None
            if bar is None and numbers:
                edges.extend(_edges_of(numbers, layer))
        if bar is not None:
            bars.append(bar)
    return bars, edges


def footprint_bars(shapes: list[Any]) -> list[Segment]:
    """Return the bars drawn on the silkscreen and document layers."""
    return _footprint_primitives(shapes)[0]


def footprint_edges(shapes: list[Any]) -> list[Segment]:
    """Return the other straight edges on those layers: polylines and wider filled regions."""
    return _footprint_primitives(shapes)[1]


def _body_boxes(shapes: list[Any]) -> list[tuple[float, float, float, float]]:
    """Return the boxes of the body-outline records in the drawing's own units.

    Only straight outlines are boxed: a Pro ``POLY`` or ``FILL`` whose points are
    straight segments (:func:`jlcfootprint.easyeda_parse.pro_points_straight`), a
    classic ``SOLIDREGION``.  Any other record on the body layer, an arc, a circle
    or a curved outline, is left out, so a body drawn partly with them gives the
    box of its straight records.  That box can only understate the body, so the
    courtyard caveat it feeds never reports an overhang the body does not have.
    """
    boxes: list[tuple[float, float, float, float]] = []
    if is_pro(shapes):
        for record in _pro_records(shapes):
            if len(record) < 5 or record[4] != _PRO_BODY_LAYER:
                continue
            if record[0] == "POLY" and len(record) > 6:
                points = record[6]
            elif record[0] == "FILL" and len(record) > 7:
                rings = record[7] if isinstance(record[7], list) else []
                points = rings[0] if rings and isinstance(rings[0], list) else rings
            else:
                continue
            if not pro_points_straight(points):
                continue
            box = _box_of(_numbers(points))
            if box:
                boxes.append(box)
        return boxes
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith("SOLIDREGION~"):
            continue
        parts = _classic_fields(shape)
        if len(parts) < 4 or parts[1] != _CLASSIC_BODY_LAYER:
            continue
        numbers = _classic_path_numbers(parts[3])
        box = _box_of(numbers) if numbers else None
        if box:
            boxes.append(box)
    return boxes


def footprint_body_box(
    shapes: list[Any], origin: Optional[tuple[float, float]] = None
) -> Optional[tuple[float, float, float, float]]:
    """Return the body outline's box in millimetres in the footprint's pad frame, or None.

    Pro drawings are in mils with Y up about the footprint origin; classic drawings
    are in canvas units with Y down about ``origin`` (the ``head`` x/y that the pad
    parser subtracts, ``ComponentRecord.footprint_origin``).  A classic drawing must
    be given it: a box about (0, 0) would sit a metre or so away from the pads.  A
    Pro drawing has no such origin and ignores the argument.  The result has
    KiCad's Y-down sense, like the pads that
    :func:`jlcfootprint.geometry.easyeda_pads_to_mm` produces.
    """
    boxes = _body_boxes(shapes)
    if not boxes:
        return None
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[2] for box in boxes)
    y2 = max(box[3] for box in boxes)
    if is_pro(shapes):
        scale = EASYEDA_UNIT_MM / PRO_MILS_PER_CANVAS_UNIT
        return (x1 * scale, -y2 * scale, x2 * scale, -y1 * scale)
    if origin is None:
        raise ValueError("a classic footprint drawing needs its origin (the head x/y)")
    ox, oy = origin
    return (
        (x1 - ox) * EASYEDA_UNIT_MM,
        (y1 - oy) * EASYEDA_UNIT_MM,
        (x2 - ox) * EASYEDA_UNIT_MM,
        (y2 - oy) * EASYEDA_UNIT_MM,
    )


# ---------------------------------------------------------------------------
# The "+" mark
# ---------------------------------------------------------------------------


def _point_to_segment(px: float, py: float, segment: Segment) -> float:
    """Return the distance from a point to a segment."""
    dx, dy = segment.x2 - segment.x1, segment.y2 - segment.y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return math.hypot(px - segment.x1, py - segment.y1)
    t = ((px - segment.x1) * dx + (py - segment.y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (segment.x1 + t * dx), py - (segment.y1 + t * dy))


def plus_marks(
    bars: list[Segment],
    span: float,
    bar_range: tuple[float, float],
    edges: Optional[list[Segment]] = None,
) -> list[tuple[float, float]]:
    """Return the crossing points of every ``+`` drawn by the bars.

    A ``+`` is one horizontal and one vertical bar on the same layer, each between
    ``bar_range[0]`` and ``bar_range[1]`` of ``span`` long and alike in length,
    crossing within :data:`_MIDPOINT_TOLERANCE` of both midpoints, with no third
    bar of that layer nearer the crossing or either bar's ends than
    :data:`_CLEARANCE` of the shorter bar, and no polyline or polygon edge
    (``edges``) of that layer through the crossing itself: a ``+`` stands alone,
    where a diode's silk arrow, drawn as one polyline, ends on its bar exactly at
    the bar's crossing with the lead.  Edges are tested at the crossing only: over
    some 13,000 two-pad drawings from EasyEDA's library that changes no answer,
    while testing them at the bars' ends would reject the marks drawn beside a pad
    outline.
    """
    if span <= 0:
        return []
    low, high = bar_range[0] * span, bar_range[1] * span
    candidates = [
        bar
        for bar in bars
        if (bar.horizontal or bar.vertical) and low <= bar.length <= high
    ]
    marks: list[tuple[float, float]] = []
    for h in (bar for bar in candidates if bar.horizontal):
        hx1, hx2 = sorted((h.x1, h.x2))
        for v in (bar for bar in candidates if bar.vertical and bar.layer == h.layer):
            vy1, vy2 = sorted((v.y1, v.y2))
            if not (hx1 <= v.x1 <= hx2 and vy1 <= h.y1 <= vy2):
                continue
            longer, shorter = max(h.length, v.length), min(h.length, v.length)
            if longer > _LENGTH_RATIO * shorter:
                continue
            if abs(v.x1 - h.midpoint[0]) > _MIDPOINT_TOLERANCE * h.length:
                continue
            if abs(h.y1 - v.midpoint[1]) > _MIDPOINT_TOLERANCE * v.length:
                continue
            cx, cy = v.x1, h.y1
            clearance = _CLEARANCE * shorter
            points = (
                (cx, cy),
                (h.x1, h.y1),
                (h.x2, h.y2),
                (v.x1, v.y1),
                (v.x2, v.y2),
            )
            crowded = any(
                other is not h
                and other is not v
                and other.layer == h.layer
                and any(
                    _point_to_segment(px, py, other) < clearance for px, py in points
                )
                for other in bars
            )
            if not crowded and edges:
                crowded = any(
                    edge.layer == h.layer
                    and _point_to_segment(cx, cy, edge) < clearance
                    for edge in edges
                )
            if not crowded:
                marks.append((cx, cy))
    return marks


def footprint_bar_range(shapes: list[Any], spacing: float) -> tuple[float, float]:
    """Return the footprint's bar lengths as fractions of the pad ``spacing``.

    The lower bound is the smaller of 5 % of the spacing and
    :data:`_FOOTPRINT_BAR_FLOOR_MM` in the drawing's own units (Pro mils, classic
    10-mil canvas units), so the bound stops growing with the part; the upper
    bound is :data:`_FOOTPRINT_BAR_RANGE`'s.
    """
    if is_pro(shapes):
        mm_per_unit = EASYEDA_UNIT_MM / PRO_MILS_PER_CANVAS_UNIT
    else:
        mm_per_unit = EASYEDA_UNIT_MM
    low, high = _FOOTPRINT_BAR_RANGE
    if spacing <= 0:
        return (low, high)
    return (min(low, _FOOTPRINT_BAR_FLOOR_MM / mm_per_unit / spacing), high)


def footprint_positive_pad(shapes: list[Any]) -> Optional[str]:
    """Return the number of the pad the footprint's ``+`` marks sit beside, or None.

    Only two-pad drawings are read.  The bars' lengths are judged by
    :func:`footprint_bar_range`.  A ``+`` within :data:`_OFF_CENTRE` of the pad
    spacing from the centre (an origin cross) is ignored; the others must all sit
    on the same side, else no pad is named.
    """
    pads = footprint_pads(shapes)
    if len(pads) != 2:
        return None
    (n1, x1, y1), (n2, x2, y2) = pads
    spacing = math.hypot(x2 - x1, y2 - y1)
    if spacing <= 0:
        return None
    ux, uy = (x2 - x1) / spacing, (y2 - y1) / spacing
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    bars, edges = _footprint_primitives(shapes)
    sides: set[str] = set()
    bar_range = footprint_bar_range(shapes, spacing)
    for mx, my in plus_marks(bars, spacing, bar_range, edges):
        along = (mx - cx) * ux + (my - cy) * uy
        if abs(along) < _OFF_CENTRE * spacing:
            continue
        sides.add(n2 if along > 0 else n1)
    return sides.pop() if len(sides) == 1 else None


# ---------------------------------------------------------------------------
# Symbol: pins and bars
# ---------------------------------------------------------------------------


def symbol_pins(shapes: list[Any]) -> list[tuple[str, float, float]]:
    """Return ``(number, x, y)`` per pin in the symbol's own units, both formats.

    A pin the symbol leaves unnumbered is skipped on purpose: the marks are matched
    to pads through the pin number, so a pin with no number names no terminal and
    must not be offered as one.  The numbered pins of the same symbol are kept.
    """
    if is_pro(shapes):
        records = pro_pin_records(shapes)
    else:
        records = classic_pin_records(shapes)
    return [(number, x, y) for number, _label, x, y in records if number]


def _box_edges(box: tuple[float, float, float, float], layer: str) -> list[Segment]:
    """Return the four edges of a box."""
    x1, y1, x2, y2 = box
    return _edges_between([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)], layer)


def _symbol_primitives(shapes: list[Any]) -> tuple[list[Segment], list[Segment]]:
    """Return the symbol's bars (strokes, thin rectangles) and its other straight edges."""
    bars: list[Segment] = []
    edges: list[Segment] = []
    if is_pro(shapes):
        for record in _pro_records(shapes):
            bar = None
            if record[0] == "POLY" and len(record) > 2:
                bar = _stroke(_numbers(record[2]), "symbol")
                if bar is None:
                    edges.extend(_pro_edges(record[2], "symbol"))
            elif record[0] == "RECT" and len(record) > 5:
                numbers = _numbers(record[2:6])
                box = _box_of(numbers) if len(numbers) == 4 else None
                bar = _bar_from_box(box, "symbol") if box else None
                if bar is None and box:
                    edges.extend(_box_edges(box, "symbol"))
            if bar is not None:
                bars.append(bar)
        return bars, edges
    for shape in shapes:
        if not isinstance(shape, str):
            continue
        parts = _classic_fields(shape)
        bar = None
        if parts[0] in ("PL", "PG") and len(parts) > 1:
            try:
                numbers = [float(v) for v in parts[1].split()]
            except ValueError:
                continue
            bar = _stroke(numbers, "symbol") if parts[0] == "PL" else None
            if bar is None:
                edges.extend(_edges_of(numbers, "symbol"))
        elif parts[0] == "R" and len(parts) > 6:
            try:
                x, y, w, h = (float(parts[i]) for i in (1, 2, 5, 6))
            except ValueError:
                continue
            bar = _bar_from_box((x, y, x + w, y + h), "symbol")
            if bar is None:
                edges.extend(_box_edges((x, y, x + w, y + h), "symbol"))
        if bar is not None:
            bars.append(bar)
    return bars, edges


def symbol_bars(shapes: list[Any]) -> list[Segment]:
    """Return the symbol's axis-aligned strokes and thin rectangles as bars."""
    return _symbol_primitives(shapes)[0]


def symbol_edges(shapes: list[Any]) -> list[Segment]:
    """Return the symbol's other straight edges: polylines, polygons, wider rectangles."""
    return _symbol_primitives(shapes)[1]


def symbol_positive_pin(shapes: list[Any]) -> Optional[str]:
    """Return the number of the pin the symbol's ``+`` sits beside, or None.

    Only two-pin symbols are read; the nearer pin must be clearly nearer and every
    ``+`` must name the same pin.
    """
    pins = symbol_pins(shapes)
    if len(pins) != 2:
        return None
    (n1, x1, y1), (n2, x2, y2) = pins
    span = math.hypot(x2 - x1, y2 - y1)
    if span <= 0:
        return None
    bars, edges = _symbol_primitives(shapes)
    names: set[str] = set()
    for mx, my in plus_marks(bars, span, _SYMBOL_BAR_RANGE, edges):
        d1, d2 = math.hypot(mx - x1, my - y1), math.hypot(mx - x2, my - y2)
        if d1 <= d2 / _NEARER_RATIO:
            names.add(n1)
        elif d2 <= d1 / _NEARER_RATIO:
            names.add(n2)
        else:
            return None
    return names.pop() if len(names) == 1 else None


def drawing_marks(
    symbol_shapes: list[Any],
    footprint_shapes: list[Any],
    origin: Optional[tuple[float, float]] = None,
) -> DrawingMarks:
    """Read both drawings; ``origin`` is the classic footprint's head x/y.

    A classic footprint drawing must be given its origin (see
    :func:`footprint_body_box`); a Pro one ignores it.
    """
    return DrawingMarks(
        positive_pin=symbol_positive_pin(symbol_shapes),
        positive_pad=footprint_positive_pad(footprint_shapes),
        body_box=footprint_body_box(footprint_shapes, origin),
    )
