"""Pad geometry: the pad record, its frame and units, its fingerprint, and the CPL angle conversions.

Everything here is stdlib and frame-explicit.  A ``Pad`` is always in
millimetres, in the footprint's own frame, with KiCad's Y-down axis.  EasyEDA
raw pads are converted exactly once, by :func:`easyeda_pads_to_mm`.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import NamedTuple, Optional

# One EasyEDA canvas unit (the classic per-LCSC response format) is 10 mil.
EASYEDA_UNIT_MM = 0.254

# Whether EasyEDA's classic canvas Y axis must be negated to match KiCad's Y-down
# frame.  It must not: the classic canvas also counts Y downwards.  JLC's placement
# preview agrees on both board sides (on the front, a SOT-23 at 180 degrees and a
# SOIC-8 with a -BL token at 270), and a flip would leave every part whose pads are
# not mirror-symmetric fitting only as its mirror image.  (EasyEDA Pro footprint
# text is in mils with Y up, and is flipped when it is read.)
FLIP_EASYEDA_Y = False


class Pad(NamedTuple):
    """One pad in millimetres in the footprint's own frame (KiCad Y-down).

    ``rotation`` is the pad's own angle relative to the footprint.  ``pin_function``
    is the schematic pin name KiCad stored on the pad ("" when unknown).  ``shape``
    is the KiCad pad shape name when known; a ``custom`` pad's stored size is only
    its anchor, so its extent cannot be judged.
    """

    number: str
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0.0
    pin_function: str = ""
    shape: str = ""


def named_pads(pads: list[Pad]) -> list[Pad]:
    """Return the pads that carry a name, the only pads that can be paired.

    Unnamed pads are NPTH holes and paste-only copper.  Pads are paired between
    two footprints by name, compared as strings, so lettered pads (A1, B12) pair
    too.
    """
    return [pad for pad in pads if pad.number]


def _quantised(value: float) -> float:
    """Round to 0.001 mm and normalise -0.0 to 0.0 so equal geometry hashes equally."""
    return round(float(value), 3) + 0.0


def pad_hash(pads: list[Pad]) -> str:
    """Return a 16-hex-character fingerprint of the pads' names, positions and effective sizes.

    The effective size is the pad's box after its own 90-degree rotation, so
    turning a rectangular pad on its side changes the hash.  Pin functions do not:
    the hash identifies the copper, and a part's polarity is worked out from its
    pin functions each time rather than stored under the hash.  Order independent,
    0.001 mm quantised, stable across processes and Python versions (blake2b,
    8-byte digest).  It is meant as a persistent key, so anything stored under it
    is found again only while the hash stays the same: changing what it covers
    orphans every stored result.
    """
    rows = []
    for pad in pads:
        _, _, width, height = pad_geom(pad)
        rows.append(
            (
                pad.number,
                _quantised(pad.x),
                _quantised(pad.y),
                _quantised(width),
                _quantised(height),
            )
        )
    payload = json.dumps(sorted(rows)).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def easyeda_pads_to_mm(
    raw_pads: list[dict], flip_y: Optional[bool] = None
) -> list[Pad]:
    """Convert raw classic-format EasyEDA pads (origin-subtracted canvas units) to millimetre pads.

    ``flip_y`` defaults to :data:`FLIP_EASYEDA_Y`.  A flip negates Y only; the
    pad's own rotation is left as stored because it is only ever used modulo 180
    (to swap width and height, :func:`pad_geom`).
    """
    flip = FLIP_EASYEDA_Y if flip_y is None else flip_y
    sign = -1.0 if flip else 1.0
    return [
        Pad(
            number=str(raw["number"]),
            x=float(raw["x"]) * EASYEDA_UNIT_MM,
            y=sign * float(raw["y"]) * EASYEDA_UNIT_MM,
            width=float(raw["w"]) * EASYEDA_UNIT_MM,
            height=float(raw["h"]) * EASYEDA_UNIT_MM,
            rotation=float(raw.get("rotation", 0.0) or 0.0),
            shape=str(raw.get("shape", "") or ""),
        )
        for raw in raw_pads
    ]


def mirror_y(pads: list[Pad]) -> list[Pad]:
    """Return the pads mirrored across the X axis (y -> -y), for bottom-side footprints.

    The pad's own rotation is left as stored; it is only ever used modulo 180.
    """
    return [pad._replace(y=-pad.y) for pad in pads]


def mirror_box(
    box: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Return a box mirrored across the X axis (y -> -y), for bottom-side footprints."""
    x1, y1, x2, y2 = box
    return (x1, -y2, x2, -y1)


def rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    """Rotate ``(x, y)`` by ``degrees`` around the origin, turning +X towards +Y.

    That is counter-clockwise in the usual Y-up picture; in KiCad's Y-down frame
    it reads clockwise on screen.  Every rotation the package makes from an angle
    in degrees goes through here, so one sign convention holds everywhere.  Code
    that already holds radians (the solver's ``atan2`` result) rotates directly
    instead, because a round trip through degrees would cost precision.
    """
    theta = math.radians(degrees)
    cos, sin = math.cos(theta), math.sin(theta)
    return (x * cos - y * sin, x * sin + y * cos)


def centroid(pads: list[Pad]) -> tuple[float, float]:
    """Return the mean pad centre; raise ValueError for no pads."""
    if not pads:
        raise ValueError("centroid of no pads")
    count = len(pads)
    return (sum(p.x for p in pads) / count, sum(p.y for p in pads) / count)


def pad_box_centre(pads: list[Pad]) -> Optional[tuple[float, float]]:
    """Return the centre of the pads' bounding box, or None without any.

    The box is the union of the pads' effective boxes (:func:`pad_geom`).  Given a
    footprint's soldered pads, every copper pad that is neither an NPTH hole nor
    paste-only, numbered or not, it is the footprint-frame counterpart of the box
    ``Fabrication.get_position`` merges in board coordinates for the CPL's Mid X/Y,
    which keeps the same pads.  No pad is dropped here, so an unnumbered soldered
    pad (a thermal or shield tab) counts, as it does there.
    """
    boxes = [pad_geom(pad) for pad in pads]
    if not boxes:
        return None
    left = min(x - width / 2.0 for x, _y, width, _h in boxes)
    right = max(x + width / 2.0 for x, _y, width, _h in boxes)
    top = min(y - height / 2.0 for _x, y, _w, height in boxes)
    bottom = max(y + height / 2.0 for _x, y, _w, height in boxes)
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def pad_pitch(pads: list[Pad]) -> Optional[float]:
    """Return the smallest centre-to-centre distance between differently numbered pads.

    This is the footprint's pitch as a reader would measure it: the distance
    between its two nearest terminals, in millimetres, on a KiCad footprint or a
    JLC drawing alike.  None when fewer than two distinct numbers carry a
    position, so a one-pad drawing reports nothing rather than zero.  Pads sharing
    a number (a DPAK's tab and its stub) are one terminal and never set the pitch.
    """
    named = named_pads(pads)
    distances = [
        math.hypot(one.x - other.x, one.y - other.y)
        for index, one in enumerate(named)
        for other in named[index + 1 :]
        if one.number != other.number
    ]
    return min(distances) if distances else None


def pad_geom(pad: Pad) -> tuple[float, float, float, float]:
    """Return ``(x, y, w, h)``: the pad's centre and its axis-aligned size after its own rotation.

    Exact for multiples of 90 degrees (a swap of width and height at 90 and 270);
    for other angles it is the bounding box of the rotated rectangle.
    """
    theta = math.radians(pad.rotation)
    cos, sin = abs(math.cos(theta)), abs(math.sin(theta))
    width = pad.width * cos + pad.height * sin
    height = pad.width * sin + pad.height * cos
    return (pad.x, pad.y, round(width, 9), round(height, 9))


def normalize_angle(degrees: float) -> float:
    """Return the angle in degrees, normalised to the half-open interval (-180, 180]."""
    while degrees <= -180.0:
        degrees += 360.0
    while degrees > 180.0:
        degrees -= 360.0
    return degrees


def snap_to_90(degrees: float) -> int:
    """Return the multiple of 90 nearest the angle, as 0, 90, 180 or 270.

    Exact 45-degree ties round to the even multiple (Python's ``round``), which is
    deterministic.
    """
    return (round(degrees / 90.0) * 90) % 360


def ccw_correction(math_rotation_deg: float) -> int:
    """Convert the solver's Y-down math-frame angle to the CPL correction, snapped to 90.

    The solver's angle turns KiCad's pads onto JLC's drawing, from +X towards +Y,
    which in KiCad's Y-down frame reads clockwise on screen.  The CPL correction
    is the inverse turn, JLC's drawing back onto KiCad's pads, counted
    counter-clockwise as KiCad counts rotation.  Inverting the turn and reversing
    the direction cancel, so the two are the same number and the angle is taken
    as it is.  JLC's placement preview agrees: a SOIC-8 with a -BL token needs 270
    degrees, where the negated angle would give 90 (a 180-degree part such as a
    SOT-23 reads the same either way).
    Feed it ``TransformResult.rotation_deg`` (or the raw angle; both snap here).
    The snap is :func:`snap_to_90`: exact 45-degree ties round to the even
    multiple, which is irrelevant in practice because a part turned 45 degrees
    from both snaps fits at neither.
    """
    return snap_to_90(math_rotation_deg)


def naming_rule_to_cpl(rotation: int) -> int:
    """Convert a rotation from EasyEDA's naming rule (``naming.parse_package_name``) to the CPL's sense.

    The naming-rule table counts the counter-clockwise turn that carries KiCad's
    footprint onto JLC's drawing: for a -BL token, KiCad's top-left pin 1 lands
    on JLC's bottom-left after 90 degrees.  The CPL counts the turn that carries
    JLC's drawing back onto KiCad's footprint, so the two are negatives of each
    other: a SOIC-8 with a -BL token is 90 in the table and 270 in the CPL, which
    is what JLC's placement preview shows.
    """
    return (-rotation) % 360
