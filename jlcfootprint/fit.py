"""Pad pairing, placement and fit grading.

Pure geometry: pair pads by name, solve the placement, transform KiCad pads into
JLC's frame, grade how each JLC pad lands on KiCad copper, and align by shape
alone when the names cannot be trusted.  Everything is in the solver's math
frame (KiCad's Y-down data); ``geometry.ccw_correction`` converts angles for the
CPL.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import math
from typing import Optional

from .geometry import Pad, centroid, named_pads, pad_geom, rotate
from .polarity import is_no_function
from .quality import angular_rms
from .records import SymbolPin
from .solver import TransformResult, solve_transform

# A JLC pad lands on copper when its centre sits inside the inner part of the KiCad pad
# (this fraction of the pad's half-size each way) and the overlap is at least MIN_OVERLAP
# of the smaller pad.  The centre rule makes 0402 pads under an 0603 part "tight"; the
# overlap rule tolerates land patterns that differ in size, such as KiCad's small SOD-323
# pads.
CENTRE_TOLERANCE = 0.8
MIN_OVERLAP = 0.5
# Pads this much bigger on one side are reported: KiCad's larger as the grade
# ``fits_larger_pads``, JLC's larger as a note that KiCad's pads are smaller.  When
# pads that share a number are paired, a pad this much bigger than another is of
# another kind (a tab beside a pin).
SIZE_RATIO = 1.5
# One pairing of a two-pad part's pads fits clearly better than the swapped one when
# its worst pad overlaps by at least this much more.  Mirror-symmetric pads give
# exactly 0, and so does every two-pad part on the two gate boards.  The coin-cell
# holder in test_jlcfootprint_two_pad_polarity.py (C7498149), whose pads differ in
# shape, gives 0.263 (0.947 against 0.684), so 0.1 sits well clear of both.
SHAPE_MARGIN = 0.1


@dataclass
class Placement:
    """A solved alignment in the solver's math frame.

    ``rotation_deg`` is snapped to 90 and ``offset_x/y`` is the translation that keeps
    the matched centroids together under that snapped angle, which is what a CPL
    position means and what keeps a pin-1-origin footprint from drifting.
    """

    rotation_deg: int
    offset_x: float
    offset_y: float
    is_mirrored: bool
    is_underdetermined: bool
    residual: float
    angular_rms: float
    matched: int


@dataclass
class FitReport:
    """What the fit pass found: the grade plus the notes and metrics that explain it."""

    fit: str  # fits | fits_larger_pads | fits_tight | count | pitch
    notes: list[str] = field(default_factory=list)
    missing_central_pad: bool = False
    overlap_min: float = 0.0
    overlap_mean: float = 0.0


@dataclass
class FunctionPairing:
    """Pads paired by the schematic's pin functions against the symbol's pin names.

    ``kicad`` and ``jlc`` are keyed like :func:`pair_by_name`'s dicts (the JLC pad
    number, then ``number#1`` for a group's second member).  ``differences`` lists
    ``(function, kicad_number, jlc_number)`` for every pair matched by function
    whose numbers differ, and ``eliminated`` the one pair made by elimination, if
    any, whatever its numbers.
    """

    kicad: dict[str, Pad]
    jlc: dict[str, Pad]
    leftovers: list[Pad]
    differences: list[tuple[str, str, str]]
    eliminated: Optional[tuple[str, str]] = None


def _number_key(number: str) -> tuple[int, int, str]:
    """Sort key for pad numbers in a note: 2 before 10, numbers before names."""
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _area(pad: Pad) -> float:
    """Return the pad's area."""
    return pad.width * pad.height


def _groups(pads: list[Pad]) -> dict[str, list[Pad]]:
    """Return the named pads grouped by number, numbers in the order they first appear."""
    groups: dict[str, list[Pad]] = {}
    for pad in named_pads(pads):
        groups.setdefault(pad.number, []).append(pad)
    return groups


def _main_members(pads: list[Pad]) -> list[Pad]:
    """Return the pads of one number's largest kind: a DPAK's tab without the stub beside it."""
    largest = max(_area(pad) for pad in pads)
    return [pad for pad in pads if largest <= SIZE_RATIO * _area(pad)]


def _one_kind(pads: list[Pad]) -> bool:
    """Return True when the pads are all of one kind, none SIZE_RATIO bigger than another."""
    return len(_main_members(pads)) == len(pads)


def anchor(pads: list[Pad]) -> tuple[float, float]:
    """Return where one number's terminal sits: the centroid of its pads of the largest kind.

    A side that draws a terminal as several like pads (a PLCC-4 LED's 1, 1, 2, 2) and
    a side that draws it as one put it in the same place, and a tab with the stub of a
    cut lead beside it puts it on the tab.
    """
    return centroid(_main_members(pads))


def _first_placement(
    pairs: dict[str, str],
    groups_k: dict[str, list[Pad]],
    groups_j: dict[str, list[Pad]],
) -> Optional[TransformResult]:
    """Return a rough placement of KiCad's pads onto JLC's that no duplicate number sways, or None.

    It is solved on the numbers that name one pad on each side, or, when fewer than
    two do, on the centroid of each number's pads on each side.  None when neither
    fixes a rotation.
    """
    shared = [(nj, nk) for nj, nk in pairs.items() if groups_k.get(nk)]
    unique = [
        (nj, nk)
        for nj, nk in shared
        if len(groups_j[nj]) == 1 and len(groups_k[nk]) == 1
    ]
    for chosen in (unique, shared):
        if len(chosen) < 2:
            continue
        transform = solve_transform(
            {nj: centroid(groups_k[nk]) for nj, nk in chosen},
            {nj: centroid(groups_j[nj]) for nj, _ in chosen},
        )
        if not transform.is_underdetermined:
            return transform
    return None


def _pair_members(
    group_k: list[Pad], group_j: list[Pad], transform: Optional[TransformResult]
) -> tuple[list[tuple[Pad, Pad]], list[Pad]]:
    """Pair the pads one number names on each side; return the (KiCad, JLC) pairs and the JLC pads left over.

    A JLC pad pairs with a KiCad pad of its own kind first: one whose area is within
    SIZE_RATIO of its own, so a pin meets a pin and a tab meets a tab whatever the
    placement says.  Among pads of one kind the nearest after the first placement
    wins, or, without one, the closest in area.  The closest candidates pair first,
    and equal candidates are told apart by the pads themselves, never by the order
    they are listed in.
    """

    def cost(pad_k: Pad, pad_j: Pad) -> tuple[int, float]:
        small, large = sorted((_area(pad_k), _area(pad_j)))
        other_kind = int(large > SIZE_RATIO * small)
        if transform is None:
            return other_kind, round(large - small, 6)
        x, y = rotate(pad_k.x, pad_k.y, transform.rotation_deg_raw)
        distance = math.hypot(
            x + transform.offset_x - pad_j.x, y + transform.offset_y - pad_j.y
        )
        return other_kind, round(distance, 6)

    candidates = sorted(
        (cost(pad_k, pad_j), pad_j, pad_k, index_j, index_k)
        for index_k, pad_k in enumerate(group_k)
        for index_j, pad_j in enumerate(group_j)
    )
    used_k: set[int] = set()
    used_j: set[int] = set()
    matched: list[tuple[Pad, Pad]] = []
    for _, pad_j, pad_k, index_j, index_k in candidates:
        if index_j in used_j or index_k in used_k:
            continue
        used_j.add(index_j)
        used_k.add(index_k)
        matched.append((pad_k, pad_j))
    rest = [pad for index, pad in enumerate(group_j) if index not in used_j]
    return sorted(matched, key=lambda pair: (_area(pair[1]), pair[1])), sorted(
        rest, key=lambda pad: (_area(pad), pad)
    )


def _pair_groups(
    pairs: dict[str, str],
    groups_k: dict[str, list[Pad]],
    groups_j: dict[str, list[Pad]],
) -> tuple[dict[str, Pad], dict[str, Pad], list[Pad]]:
    """Pair the members of matched groups, keyed by the JLC number.

    ``pairs`` maps a JLC number to the KiCad number it pairs with.  A number that
    names one pad on each side pairs those two.  Where a number names several pads
    on a side (KiCad numbers a SOT-223's tab as a second pad 2, a DPAK's tab and the
    stub of its cut lead both 2, a switch its four legs 1, 1, 2, 2), they pair by
    kind and then by position after :func:`_first_placement` (:func:`_pair_members`),
    so the pairing does not depend on the order either footprint lists its pads in.  The JLC pads a number has no
    KiCad pad left for are returned as the third value.  Keys are the JLC number,
    then ``number#1``, ``number#2``, ... in order of the JLC pads' area.
    """
    transform = _first_placement(pairs, groups_k, groups_j)
    kicad: dict[str, Pad] = {}
    jlc: dict[str, Pad] = {}
    leftovers: list[Pad] = []
    for number_j, number_k in pairs.items():
        matched, rest = _pair_members(
            groups_k.get(number_k, []), groups_j[number_j], transform
        )
        for index, (pad_k, pad_j) in enumerate(matched):
            key = number_j if index == 0 else f"{number_j}#{index}"
            kicad[key] = pad_k
            jlc[key] = pad_j
        leftovers.extend(rest)
    return kicad, jlc, leftovers


def _signal_name(text: str, number: str) -> str:
    """Return a pin's whole signal name, upper-case, less KiCad's ``_<number>`` suffix.

    KiCad appends the pad number to a pin function (``D_2`` on pad 2, ``V+_8`` on
    pad 8), and only that suffix is cut: ``DATA_IN`` and ``DATA_OUT_1`` stay apart,
    where the leading token :func:`normalise_function` reads for polarity would make
    both ``DATA``.  A name without letters (``1``, ``1_1``) names nothing, so ``""``
    comes back; a bare sign (``+``, ``+_3``) is kept.
    """
    name = text.strip().upper()
    suffix = f"_{number.upper()}"
    if number and name.endswith(suffix) and len(name) > len(suffix):
        name = name[: -len(suffix)]
    if name in ("+", "-") or any(char.isalpha() for char in name):
        return name
    return ""


def pair_by_function(
    kicad_pads: list[Pad], jlc_pads: list[Pad], symbol_pins: list[SymbolPin]
) -> Optional[FunctionPairing]:
    """Pair pads by pin function when the numbers do not line up, or return None.

    A KiCad pad's function is the schematic pin name it carries (``D_2`` reads as
    ``D``; ``NC`` and its kin name nothing); a JLC pad's name is its symbol pin's
    label.  Names are compared whole (:func:`_signal_name`).  A name that occurs
    once on each side pairs the two pads (a group of KiCad pads sharing a number
    counts once).  At least two names must pair; when exactly one KiCad number and
    one JLC number are then left, they pair by elimination.  Any other unpaired JLC
    pad means the functions do not describe this part and nothing is returned.
    """
    labels: dict[str, str] = {}
    for pin in symbol_pins:
        name = _signal_name(pin.label, pin.number)
        if name and not is_no_function(pin.label):
            labels.setdefault(pin.number, name)
    groups_k = _groups(kicad_pads)
    groups_j = _groups(jlc_pads)
    functions_k: dict[str, str] = {}
    for number, group in groups_k.items():
        for pad in group:
            name = _signal_name(pad.pin_function, pad.number)
            if name and not is_no_function(pad.pin_function):
                # NC names no pin: two pads that merely lack a signal never pair.
                functions_k[number] = name
                break
    by_name_k: dict[str, list[str]] = {}
    for number, name in functions_k.items():
        by_name_k.setdefault(name, []).append(number)
    by_name_j: dict[str, list[str]] = {}
    for number in groups_j:
        name = labels.get(number)
        if name:
            by_name_j.setdefault(name, []).append(number)
    pairs: dict[str, str] = {}
    names: dict[str, str] = {}
    for name, numbers_j in by_name_j.items():
        numbers_k = by_name_k.get(name, [])
        if len(numbers_j) == 1 and len(numbers_k) == 1:
            pairs[numbers_j[0]] = numbers_k[0]
            names[numbers_j[0]] = name
    if len(pairs) < 2:
        return None
    rest_j = [n for n in groups_j if n not in pairs]
    rest_k = [n for n in groups_k if n not in pairs.values()]
    eliminated = None
    if len(rest_j) == 1 and len(rest_k) == 1:
        pairs[rest_j[0]] = rest_k[0]
        eliminated = (rest_j[0], rest_k[0])
    elif rest_j:
        return None
    kicad, jlc, leftovers = _pair_groups(pairs, groups_k, groups_j)
    differences = [
        (names[number_j], number_k, number_j)
        for number_j, number_k in pairs.items()
        if number_j != number_k and number_j in names
    ]
    return FunctionPairing(kicad, jlc, leftovers, differences, eliminated)


def pair_by_name(
    kicad_pads: list[Pad], jlc_pads: list[Pad]
) -> tuple[dict[str, Pad], dict[str, Pad], list[Pad]]:
    """Pair pads by name; return the matched KiCad and JLC dicts and the unmatched JLC pads.

    When a name covers several pads on one side (KiCad numbers a SOT-223 tab as a
    second pad ``2``, and a DPAK's tab and its stub pin both as ``2``), the pads
    pair by position after a first placement (:func:`_pair_groups`), so a pin meets
    a pin and a tab meets a tab; the leftovers go to the position pass.  Keys are
    ``name`` then ``name#1``, ...
    """
    groups_j = _groups(jlc_pads)
    return _pair_groups(
        {number: number for number in groups_j}, _groups(kicad_pads), groups_j
    )


def _mean(points: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """Return the mean of the points; raise ValueError for none, as ``centroid`` does."""
    points = list(points)
    if not points:
        raise ValueError("centroid of no pads")
    return (
        sum(x for x, _ in points) / len(points),
        sum(y for _, y in points) / len(points),
    )


def _solve_points(
    kicad: dict[str, Pad],
    jlc: dict[str, Pad],
    kicad_pads: Optional[list[Pad]],
    jlc_pads: Optional[list[Pad]],
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Return the points :func:`align` solves on, keyed alike on both sides.

    Each matched pair is a point, except that a number the two footprints draw as a
    different count of pads of one kind (a PLCC-4 LED's two pads 1 against a PLCC-2
    footprint's one) is one point, its :func:`anchor` on each side: the member pair
    the pairing chose would pull the placement half the members' spacing off.  A
    number with pads of two kinds keeps its member pairs, which pairing by kind made
    (KiCad's SOT-223 pad 2 is a pin and the tab; JLC's is the pin alone).
    """
    keys = [key for key in kicad if key in jlc]
    points_k: dict[str, tuple[float, float]] = {}
    points_j: dict[str, tuple[float, float]] = {}
    groups_k = _groups(kicad_pads) if kicad_pads is not None else {}
    groups_j = _groups(jlc_pads) if jlc_pads is not None else {}
    for key in keys:
        members_k = groups_k.get(kicad[key].number, [kicad[key]])
        members_j = groups_j.get(jlc[key].number, [jlc[key]])
        if len(members_k) == len(members_j) or not (
            _one_kind(members_k) and _one_kind(members_j)
        ):
            points_k[key] = (kicad[key].x, kicad[key].y)
            points_j[key] = (jlc[key].x, jlc[key].y)
        else:
            points_k[jlc[key].number] = anchor(members_k)
            points_j[jlc[key].number] = anchor(members_j)
    return points_k, points_j


def align(
    kicad: dict[str, Pad],
    jlc: dict[str, Pad],
    kicad_pads: Optional[list[Pad]] = None,
    jlc_pads: Optional[list[Pad]] = None,
) -> Placement:
    """Solve the rigid alignment of pads matched by dict key and return the placement.

    Given the footprints' pads, a number drawn as a different count of pads on the two
    sides is solved on its anchors (:func:`_solve_points`).
    """
    points_k, points_j = _solve_points(kicad, jlc, kicad_pads, jlc_pads)
    transform = solve_transform(points_k, points_j)
    common_keys = sorted(points_k)
    # An underdetermined solve has no rotation worth checking the bearings against,
    # and a non-finite pad coordinate (which is how the solver reaches that flag
    # without raising) would make the RMS NaN.  The resolver copies the placement
    # into the verdict before it refuses on ``is_underdetermined``, so the RMS is
    # 0.0 there rather than a NaN that would reach everything that reads the verdict.
    rms = (
        0.0
        if transform.is_underdetermined
        else angular_rms(
            [points_k[key] for key in common_keys],
            [points_j[key] for key in common_keys],
            transform.rotation_deg,
        )
    )
    kx, ky = _mean(points_k.values())
    jx, jy = _mean(points_j.values())
    rx, ry = rotate(kx, ky, transform.rotation_deg)
    return Placement(
        rotation_deg=transform.rotation_deg,
        offset_x=jx - rx,
        offset_y=jy - ry,
        is_mirrored=transform.is_mirrored,
        is_underdetermined=transform.is_underdetermined,
        residual=transform.residual,
        angular_rms=rms,
        matched=len(kicad),
    )


def package_origin(placement: Placement) -> tuple[float, float]:
    """Return JLC's package origin in the KiCad footprint frame.

    The placement maps KiCad pads onto the JLC drawing in the solver's frame,
    ``jlc = R(theta) * kicad + offset``; JLC's package origin is the drawing's
    (0, 0), so its position in the footprint frame is the inverse placement
    applied to the origin, ``-R(-theta) * offset``.  That point is where JLC's
    Mid X/Y must be for JLC's pads to land on the footprint's pads, which is
    what the placement preview checks.  The frame is the one the pad hash uses:
    millimetres, KiCad's Y down, a bottom-side footprint's pads already mirrored
    back into its own frame (``geometry.mirror_y``), so a caller placing a
    bottom-side part mirrors the point back, as ``Fabrication.reposition``
    mirrors a correction's offset.
    """
    x, y = -placement.offset_x, -placement.offset_y
    return rotate(x, y, -placement.rotation_deg)


def transformed(pad: Pad, placement: Placement) -> tuple[float, float, float, float]:
    """Return a KiCad pad's ``(x, y, w, h)`` after the placement (solver's math frame)."""
    rx, ry = rotate(pad.x, pad.y, placement.rotation_deg)
    x = rx + placement.offset_x
    y = ry + placement.offset_y
    _, _, width, height = pad_geom(pad)
    if placement.rotation_deg % 180 == 90:
        width, height = height, width
    return x, y, width, height


def _union_area(boxes: list[tuple[float, float, float, float]]) -> float:
    """Return the area the boxes ``(x0, y0, x1, y1)`` cover together, overlaps once.

    The boxes' edges cut the plane into cells, and a cell inside any box counts.
    Even a thermal-via exposed pad is a few dozen pads, so the cells stay few.
    """
    xs = sorted({edge for box in boxes for edge in (box[0], box[2])})
    ys = sorted({edge for box in boxes for edge in (box[1], box[3])})
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            if any(
                bx0 <= x0 and x1 <= bx1 and by0 <= y0 and y1 <= by1
                for bx0, by0, bx1, by1 in boxes
            ):
                area += (x1 - x0) * (y1 - y0)
    return area


def group_fit(
    kicad_geoms: list[tuple[float, float, float, float]], jlc_pad: Pad
) -> tuple[int, float]:
    """Grade one JLC pad against the KiCad pads sharing a number: 2 fits, 1 tight, 0 misses.

    The group is one terminal whose copper is the union of its pads (a DPAK's tab
    and the stub of its cut lead are both pad 2), so the overlap is the JLC pad's
    intersection with that union over the smaller of the two areas.  Tight means
    the pads overlap well but the JLC pad's centre falls outside the inner part of
    every KiCad pad, so the part sits at the edge of its copper.  Pads of one group
    can overlap (a thermal-via footprint numbers its vias and its back-side pad like
    the exposed pad), so copper they share is counted once.
    """
    bx, by, bw, bh = pad_geom(jlc_pad)
    jlc_area = bw * bh
    jlc_box = (bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2)
    boxes = [
        (ax - aw / 2, ay - ah / 2, ax + aw / 2, ay + ah / 2)
        for ax, ay, aw, ah in kicad_geoms
    ]
    clipped = [
        (
            max(x0, jlc_box[0]),
            max(y0, jlc_box[1]),
            min(x1, jlc_box[2]),
            min(y1, jlc_box[3]),
        )
        for x0, y0, x1, y1 in boxes
    ]
    intersection = _union_area(
        [box for box in clipped if box[0] < box[2] and box[1] < box[3]]
    )
    union_area = _union_area(boxes)
    centred = any(
        abs(bx - ax) <= CENTRE_TOLERANCE * aw / 2
        and abs(by - ay) <= CENTRE_TOLERANCE * ah / 2
        for ax, ay, aw, ah in kicad_geoms
    )
    smaller = min(jlc_area, union_area)
    overlap = min(intersection, jlc_area) / smaller if smaller > 0 else 0.0
    if overlap < MIN_OVERLAP:
        return 0, overlap
    return (2 if centred else 1), overlap


def pad_fit(
    kicad_geom: tuple[float, float, float, float], jlc_pad: Pad
) -> tuple[int, float]:
    """Grade one JLC pad against one transformed KiCad pad (see :func:`group_fit`)."""
    return group_fit([kicad_geom], jlc_pad)


def assess_fit(
    kicad: dict[str, Pad],
    jlc: dict[str, Pad],
    kicad_all: list[Pad],
    jlc_rest: list[Pad],
    placement: Placement,
    count_kicad: int,
    count_jlc: int,
) -> FitReport:
    """Grade the fit: every JLC pad must land on KiCad copper after the placement.

    Matched pads pair by key, and each is graded against every KiCad pad in
    ``kicad_all`` that shares its partner's number, because every KiCad pad
    carrying one number is one terminal: a DPAK's tab and the stub of its cut
    lead are both pad 2 and are graded together.  A remaining JLC pad whose
    number paired (a third pad 2 where KiCad draws two) belongs to that terminal:
    it is graded against the terminal's copper, or, when it misses that, against
    the nearest KiCad pad whose number no JLC pad paired with (JLC's tab 2 on
    KiCad's tab 4), never against another terminal's copper.  Any other remaining
    JLC pad (a tab numbered differently, a merged connector pin) pairs with the
    nearest transformed KiCad pad, which may be
    reused; extra KiCad copper is never a misfit.  An unmatched JLC pad that lands on
    nothing is a misfit when it sits at the periphery (a pin the footprint lacks) but
    only a warning when it contains the JLC pad centroid (an exposed pad the footprint
    lacks, which some packages number like a pin, as a MOSFET's drain pad); one of a
    paired number that lands on another terminal's copper is a misfit wherever it
    sits.  Custom-shaped KiCad pads have an unknown extent and are not judged.
    """
    report = FitReport(fit="fits")
    placed = [(pad, transformed(pad, placement)) for pad in kicad_all]
    groups: dict[str, list[tuple[float, float, float, float]]] = {}
    for pad, geom in placed:
        groups.setdefault(pad.number, []).append(geom)
    pairs = [
        (
            kicad[key],
            groups.get(kicad[key].number) or [transformed(kicad[key], placement)],
            jlc[key],
            True,
        )
        for key in kicad
    ]
    terminals: dict[str, Pad] = {}
    for key, pad in kicad.items():
        terminals.setdefault(jlc[key].number, pad)
    claimed = {pad.number for pad in kicad.values()}
    unclaimed = [item for item in placed if item[0].number not in claimed]
    for jlc_pad in jlc_rest:
        if not placed:
            break
        candidates = placed
        own = terminals.get(jlc_pad.number)
        if own is not None:
            geoms = groups.get(own.number) or [transformed(own, placement)]
            if not unclaimed or own.shape == "custom" or group_fit(geoms, jlc_pad)[0]:
                pairs.append((own, geoms, jlc_pad, False))
                continue
            candidates = unclaimed
        pad, geom = min(
            candidates,
            key=lambda item: math.hypot(item[1][0] - jlc_pad.x, item[1][1] - jlc_pad.y),
        )
        pairs.append((pad, [geom], jlc_pad, False))
    grades = [
        (2, 1.0) if kicad_pad.shape == "custom" else group_fit(geoms, jlc_pad)
        for kicad_pad, geoms, jlc_pad, _ in pairs
    ]
    if any(kicad_pad.shape == "custom" for kicad_pad, _, _, _ in pairs):
        report.notes.append("custom-shaped KiCad pad not checked for fit")
    report.overlap_min = min(overlap for _, overlap in grades)
    report.overlap_mean = sum(overlap for _, overlap in grades) / len(grades)
    jlc_pads = [jlc_pad for _, _, jlc_pad, _ in pairs]
    cx, cy = centroid(jlc_pads)
    mismatch = "count" if count_kicad != count_jlc else "pitch"
    missing_pins: list[str] = []
    astray: list[str] = []
    missing_central: list[str] = []
    tight: list[str] = []
    for (_, _, jlc_pad, matched), (grade, _) in zip(pairs, grades):
        if grade == 2:
            continue
        if grade == 1:
            tight.append(jlc_pad.number)
            continue
        if matched:
            report.fit = mismatch
            return report
        _, _, width, height = pad_geom(jlc_pad)
        central = abs(cx - jlc_pad.x) <= width / 2 and abs(cy - jlc_pad.y) <= height / 2
        if jlc_pad.number in terminals and (
            not central or group_fit([geom for _, geom in placed], jlc_pad)[0]
        ):
            astray.append(jlc_pad.number)
        elif central:
            missing_central.append(jlc_pad.number)
        else:
            missing_pins.append(jlc_pad.number)
    if missing_pins or astray:
        if missing_pins:
            report.notes.append(
                f"JLC pads {', '.join(sorted(missing_pins, key=_number_key))} land on no copper"
            )
        if astray:
            report.notes.append(
                f"JLC pads {', '.join(sorted(astray, key=_number_key))} land off their own terminal's copper"
            )
        report.fit = mismatch
        return report
    if missing_central:
        report.notes.append(
            f"JLC part has a central pad ({', '.join(sorted(missing_central, key=_number_key))}) your footprint lacks"
        )
        report.missing_central_pad = True
    if tight:
        report.notes.append(
            f"JLC pads {', '.join(sorted(tight, key=_number_key))} sit at the edge of your pads; check the placement preview"
        )
        report.fit = "fits_tight"
        return report
    matched_pairs = [(k, j) for k, _, j, matched in pairs if matched]
    if any(_area(k) >= SIZE_RATIO * _area(j) for k, j in matched_pairs):
        report.fit = "fits_larger_pads"
    elif any(_area(j) >= SIZE_RATIO * _area(k) for k, j in matched_pairs):
        report.notes.append("KiCad pads are smaller than JLC's land pattern")
    return report


def fits(report: FitReport) -> bool:
    """Return True when the graded pads fit, tight or not (any grade but count or pitch)."""
    return report.fit not in ("count", "pitch")


def fits_clearly_better(report: FitReport, other: FitReport) -> bool:
    """Return True when ``report``'s pairing fits clearly better than ``other``'s.

    A pairing fits clearly better when it fits and the other does not, or when both
    fit and its worst-pad overlap exceeds the other's by at least SHAPE_MARGIN.
    """
    if not fits(report):
        return False
    if not fits(other):
        return True
    return report.overlap_min >= other.overlap_min + SHAPE_MARGIN


def transformed_box(
    box: tuple[float, float, float, float], placement: Placement
) -> tuple[float, float, float, float]:
    """Return a KiCad-frame box after the placement, as the box of its moved corners."""
    xs, ys = [], []
    for x, y in (
        (box[0], box[1]),
        (box[2], box[1]),
        (box[2], box[3]),
        (box[0], box[3]),
    ):
        rx, ry = rotate(x, y, placement.rotation_deg)
        xs.append(rx + placement.offset_x)
        ys.append(ry + placement.offset_y)
    return (min(xs), min(ys), max(xs), max(ys))


def courtyard_excess(
    courtyard: tuple[float, float, float, float],
    body: tuple[float, float, float, float],
    placement: Placement,
) -> float:
    """Return how far the JLC body box overhangs the placed KiCad courtyard, at most.

    Both boxes end up in JLC's frame: the courtyard is moved by the placement that
    put the KiCad pads onto the JLC pads.  The result is the largest overhang on
    any of the four sides, 0.0 when the body sits inside the courtyard.
    """
    cx1, cy1, cx2, cy2 = transformed_box(courtyard, placement)
    bx1, by1, bx2, by2 = body
    return max(0.0, cx1 - bx1, bx2 - cx2, cy1 - by1, by2 - cy2)


def shape_alignment(kicad: list[Pad], jlc: list[Pad]) -> tuple[int, int]:
    """Align the two pad patterns ignoring names; return (math-frame rotation, JLC pads that land).

    Used only after a by-name alignment failed with equal pad counts: when the
    package fits at some angle but the numbers do not line up, the KiCad footprint's
    pin numbering differs from JLC's part, so a rotation read off the package shape
    alone would put the wrong pins on the wrong pads.
    """
    kx, ky = centroid(kicad)
    jx, jy = centroid(jlc)
    best: Optional[tuple[int, int, float]] = None
    for rotation in (0, 90, 180, 270):
        placed = []
        for pad in kicad:
            x, y = pad.x - kx, pad.y - ky
            _, _, width, height = pad_geom(pad)
            if rotation % 180 == 90:
                width, height = height, width
            rx, ry = rotate(x, y, rotation)
            placed.append((rx + jx, ry + jy, width, height))
        landed = 0
        distance = 0.0
        for jlc_pad in jlc:
            geom = min(
                placed, key=lambda g: math.hypot(g[0] - jlc_pad.x, g[1] - jlc_pad.y)
            )
            landed += pad_fit(geom, jlc_pad)[0] > 0
            distance += math.hypot(geom[0] - jlc_pad.x, geom[1] - jlc_pad.y)
        if best is None or (landed, -distance) > (best[1], -best[2]):
            best = (rotation, landed, distance)
    return best[0], best[1]
