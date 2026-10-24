"""Bounded offline trace-to-trace diagnostics for native differential mates.

This is a dimensional spacing check, not an impedance calculation. Native arc
samples remain useful for drawing, but only native start/mid/end definitions are
used for measurement. An incomplete or ambiguous assessment never reports match.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import cached_property
import hashlib
import heapq
import json
import math
from typing import Literal, Optional

from .copper_geometry import feature_bounds, feature_point_distance
from .model import (
    BoardSnapshot,
    Bounds,
    Config,
    CopperFeature,
    Point,
    Section,
    Specification,
    Trace,
)
from .spacing_transitions import contact_seed, first_exit, landing_region

SPACING_CHECK_POLICY_REVISION = 4
SPACING_COMPARISON_BAND_NM = 1_000
SpacingVerdict = Literal[
    "match", "deviation", "undetermined", "needs_target", "not_applicable"
]
SpacingCoverage = Literal["complete", "partial", "none"]
_FPoint = tuple[float, float]
_Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class NmBounds:
    """Inclusive outward bounds in integer nanometres."""

    lower_nm: int
    upper_nm: int


@dataclass(frozen=True)
class SpacingCrossSection:
    """An authoritative native centerline-to-centerline measurement witness."""

    source_trace_id: str
    mate_trace_id: str
    source_point: Point
    mate_point: Point
    source_width_nm: int
    mate_width_nm: int


@dataclass(frozen=True)
class SpacingLocation:
    """A bounded physical source span and its associated native mate geometry."""

    source_trace_id: str
    mate_trace_ids: tuple[str, ...]
    source_interval: tuple[float, float]
    source_points: tuple[Point, ...]
    mate_points: tuple[Point, ...]
    bounds: Bounds
    mate_intervals: tuple[tuple[str, float, float], ...] = ()
    cross_sections: tuple[SpacingCrossSection, ...] = ()


@dataclass(frozen=True)
class SpacingIssue:
    """A dimensional witness or explicit limitation, located when possible."""

    code: str
    trace_ids: tuple[str, ...]
    position: Optional[Point]
    gap_nm: Optional[NmBounds]
    message: str
    location: Optional[SpacingLocation] = None
    members: tuple[SpacingLocation, ...] = ()


@dataclass(frozen=True)
class SpacingCheck:
    """Independent verdict and coverage for one pair/layer or displayed row."""

    declared_spacing_nm: Optional[int]
    comparison_band_nm: int
    verdict: SpacingVerdict
    coverage: SpacingCoverage
    minimum_gap_nm: Optional[NmBounds]
    maximum_gap_nm: Optional[NmBounds]
    assessed_length_nm: int
    unverified_length_nm: int
    issues: tuple[SpacingIssue, ...]
    reasons: tuple[str, ...]
    transition_length_nm: int = 0


@dataclass(frozen=True)
class SpacingLimits:
    """Deterministic work and absolute uncertainty limits, independent of target."""

    max_evaluations: int = 100_000
    max_intervals: int = 20_000
    precision_nm: float = 0.25


@dataclass(frozen=True)
class _Primitive:
    """An exact straight centerline or native circular arc."""

    trace: Trace
    start: Point
    end: Point
    center: Optional[tuple[Fraction, Fraction]] = None
    radius: float = 0.0
    angle: float = 0.0
    sweep: float = 0.0
    source_interval: tuple[float, float] = (0.0, 1.0)
    unverified_transition: bool = False

    @cached_property
    def length(self) -> float:
        """Return physical centerline length."""
        if self.center is not None:
            return self.radius * abs(self.sweep)
        return math.dist(self.start, self.end)

    def point(self, t: float) -> _FPoint:
        """Evaluate the native primitive in its forward parameterization."""
        if t == 0:
            return self.start
        if t == 1:
            return self.end
        if self.center is None:
            return (
                self.start[0] + (self.end[0] - self.start[0]) * t,
                self.start[1] + (self.end[1] - self.start[1]) * t,
            )
        angle = self.angle + t * self.sweep
        return (
            float(self.center[0]) + self.radius * math.cos(angle),
            float(self.center[1]) + self.radius * math.sin(angle),
        )

    def contains_angle(self, angle: float) -> bool:
        """Return whether a radial projection falls on this native sweep."""
        offset = (angle - self.angle) % math.tau
        if self.sweep < 0:
            offset = (self.angle - angle) % math.tau
        return offset <= abs(self.sweep)

    def distance(self, point: _FPoint) -> float:
        """Measure to the finite centerline, including native end caps."""
        if self.center is None:
            return _point_segment_distance(point, self.start, self.end)
        delta = (point[0] - float(self.center[0]), point[1] - float(self.center[1]))
        angle = math.atan2(delta[1], delta[0])
        if self.contains_angle(angle):
            return abs(math.hypot(*delta) - self.radius)
        return min(math.dist(point, self.start), math.dist(point, self.end))

    @cached_property
    def box(self) -> _Box:
        """Enclose the complete native primitive, including arc extrema."""
        points: list[_FPoint] = [self.start, self.end]
        if self.center is not None:
            for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
                if self.contains_angle(angle):
                    points.append(
                        (
                            float(self.center[0]) + self.radius * math.cos(angle),
                            float(self.center[1]) + self.radius * math.sin(angle),
                        )
                    )
        return (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )


@dataclass(frozen=True)
class _MeasuredTrace:
    """Target-independent extrema and witnesses for one source trace."""

    trace_id: str
    length_nm: int
    verified: bool
    minimum: Optional[NmBounds]
    maximum: Optional[NmBounds]
    witnesses: tuple[SpacingIssue, ...] = ()
    reasons: tuple[str, ...] = ()
    transition_length_nm: int = 0
    assessed_length_nm: Optional[int] = None


@dataclass(frozen=True)
class SpacingMeasurement:
    """Cached geometry that can be compared with several declared targets."""

    pair: tuple[str, str]
    layer: str
    traces: tuple[_MeasuredTrace, ...]
    reasons: tuple[str, ...] = ()


class _BudgetExhausted(Exception):
    """Internal deterministic stop, converted into unverified coverage."""


class _Budget:
    """Share distance evaluation work across one entire pair/layer."""

    def __init__(self, limits: SpacingLimits) -> None:
        self.limits = limits
        self.evaluations = 0
        self.intervals = 0
        self.trees: dict[tuple[int, ...], _Node] = {}

    def consume(self) -> None:
        """Account for an actual primitive distance evaluation."""
        self.evaluations += 1
        if self.evaluations > self.limits.max_evaluations:
            raise _BudgetExhausted


@dataclass(frozen=True)
class _Node:
    """Small width-aware spatial tree for nearest and topology queries."""

    box: _Box
    half_width: float
    primitives: tuple[_Primitive, ...] = ()
    children: tuple["_Node", ...] = ()


def _tree(primitives: tuple[_Primitive, ...], budget: _Budget) -> _Node:
    """Build balanced spatial leaves without an external geometry dependency."""
    key = tuple(id(primitive) for primitive in primitives)
    cached = budget.trees.get(key)
    if cached is not None:
        return cached
    boxes = []
    for primitive in primitives:
        budget.consume()
        boxes.append(primitive.box)
    box = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    width = max(p.trace.width_nm / 2 for p in primitives)
    if len(primitives) <= 8:
        result = _Node(box, width, primitives)
        budget.trees[key] = result
        return result
    axis = 0 if box[2] - box[0] >= box[3] - box[1] else 1
    ordered = sorted(
        primitives,
        key=lambda p: (p.box[axis] + p.box[axis + 2], p.trace.trace_id, p.start),
    )
    middle = len(ordered) // 2
    result = _Node(
        box,
        width,
        children=(
            _tree(tuple(ordered[:middle]), budget),
            _tree(tuple(ordered[middle:]), budget),
        ),
    )
    budget.trees[key] = result
    return result


def _box_distance(point: _FPoint, box: _Box) -> float:
    """Lower-bound distance to any point in an axis-aligned box."""
    return math.hypot(
        max(box[0] - point[0], 0, point[0] - box[2]),
        max(box[1] - point[1], 0, point[1] - box[3]),
    )


def _point_segment_distance(point: _FPoint, start: _FPoint, end: _FPoint) -> float:
    """Measure to a finite line segment, including a zero-length segment."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    denominator = dx * dx + dy * dy
    t = (
        0.0
        if denominator == 0
        else max(
            0.0,
            min(
                1.0,
                ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator,
            ),
        )
    )
    return math.hypot(point[0] - start[0] - t * dx, point[1] - start[1] - t * dy)


def _cross(a: _FPoint, b: _FPoint, c: _FPoint) -> float:
    """Return twice signed triangle area."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segment_distance(a: _FPoint, b: _FPoint, c: _FPoint, d: _FPoint) -> float:
    """Return the exact geometric minimum up to floating-point arithmetic."""
    ab_c, ab_d, cd_a, cd_b = (
        _cross(a, b, c),
        _cross(a, b, d),
        _cross(c, d, a),
        _cross(c, d, b),
    )
    if (ab_c > 0 > ab_d or ab_d > 0 > ab_c) and (cd_a > 0 > cd_b or cd_b > 0 > cd_a):
        return 0.0
    return min(
        _point_segment_distance(a, c, d),
        _point_segment_distance(b, c, d),
        _point_segment_distance(c, a, b),
        _point_segment_distance(d, a, b),
    )


def _primitives(trace: Trace) -> tuple[_Primitive, ...]:
    """Preserve native arcs; never reinterpret their render samples as lines."""
    if not trace.is_native_arc:
        return tuple(
            _Primitive(trace, a, b)
            for a, b in zip(trace.points, trace.points[1:])
            if a != b
        )
    midpoint, native_center, native_radius = (
        trace.native_arc_midpoint,
        trace.native_arc_center,
        trace.native_arc_radius_nm,
    )
    if midpoint is None or native_center is None or native_radius is None:
        return ()
    a, b, c = trace.points[0], midpoint, trace.points[-1]
    # KiCad snaps its computed center before calculating radius. A rational
    # circumcircle of the stored points is therefore not authoritative. Until
    # the endpoint discrepancy is bounded, assess only an exact native circle.
    squares = tuple(
        sum((value - origin) ** 2 for value, origin in zip(point, native_center))
        for point in (a, b, c)
    )
    if len(set(squares)) != 1 or not squares[0]:
        return ()
    center = tuple(Fraction(value) for value in native_center)
    radius = math.hypot(float(center[0]) - a[0], float(center[1]) - a[1])
    if (
        not math.isfinite(radius)
        or not math.isfinite(native_radius)
        or abs(native_radius - radius) > max(math.ulp(radius) * 8, 1e-9)
        or radius > 1e12
        or max(abs(v) for p in (a, b, c) for v in p) > 1e12
    ):
        return ()
    angles = tuple(
        math.atan2(p[1] - float(center[1]), p[0] - float(center[0])) for p in (a, b, c)
    )
    sweep = (angles[2] - angles[0]) % math.tau
    if (angles[1] - angles[0]) % math.tau > sweep:
        sweep -= math.tau
    if not sweep:
        return ()
    return (_Primitive(trace, a, c, center, radius, angles[0], sweep),)


def _slice_primitive(source: _Primitive, start: float, end: float) -> _Primitive:
    """Retain a native interval without changing the trace's identity."""
    if start == 0 and end == 1:
        return source
    lower, upper = source.source_interval
    return replace(
        source,
        start=source.point(start),
        end=source.point(end),
        angle=source.angle + source.sweep * start,
        sweep=source.sweep * (end - start),
        source_interval=(
            lower + (upper - lower) * start,
            lower + (upper - lower) * end,
        ),
    )


def _reverse_primitive(source: _Primitive) -> _Primitive:
    """Orient a route away from a contacted terminal endpoint."""
    return replace(
        source,
        start=source.end,
        end=source.start,
        angle=source.angle + source.sweep,
        sweep=-source.sweep,
        source_interval=tuple(reversed(source.source_interval)),
    )


@dataclass(frozen=True)
class _Terminal:
    """One degree-one connected route root with native copper contact."""

    net: str
    contacts: tuple[CopperFeature, ...]
    route: tuple[tuple[_Primitive, _Primitive], ...]
    direction: _FPoint


def _curve_direction(curve: _Primitive, parameter: float) -> _FPoint:
    """Return the physical outward tangent, independent of track segmentation."""
    if curve.center is None:
        dx, dy = curve.end[0] - curve.start[0], curve.end[1] - curve.start[1]
    else:
        angle = curve.angle + curve.sweep * parameter
        dx, dy = -math.sin(angle) * curve.sweep, math.cos(angle) * curve.sweep
    length = math.hypot(dx, dy)
    return (dx / length, dy / length)


def _terminal_branch_cuts(
    own: tuple[_Primitive, ...],
    endpoints: dict[Point, list[_Primitive]],
    budget: _Budget,
    *,
    features: tuple[CopperFeature, ...] = (),
) -> dict[int, list[float]]:
    """Stop at physical same-net junctions even when native tracks are unsplit.

    A spatial broad phase finds intersecting native stroke capsules. The exact
    native distance and a Lipschitz first-contact search handle straight/curved
    crossings and offset stroke contacts. Collinear segmentation and ordinary
    shared endpoint continuations do not create branches.
    """
    result: dict[int, list[float]] = {}

    def adjacent_stroke_neighbors(
        source: _Primitive,
    ) -> dict[int, tuple[Point, Point]]:
        """Keep short degree-two bends from manufacturing a third copper branch.

        Stroke capsules at a tiny native jog overlap the capsules on either
        side. That contact belongs to the same local path. A remote reentry or
        genuine branch has no degree-two connection within one stroke width.
        """
        nearby = {id(source): (source.start, source.start)}
        pending = [(source, point, 0.0, point) for point in (source.start, source.end)]
        while pending:
            current, point, distance, root = pending.pop()
            budget.consume()
            incident = endpoints[point]
            if len(incident) != 2:
                continue
            other = next(p for p in incident if p is not current)
            if id(other) in nearby:
                continue
            nearby[id(other)] = (root, point)
            length = distance + other.length
            if length <= max(source.trace.width_nm, other.trace.width_nm):
                pending.append(
                    (
                        other,
                        other.end if other.start == point else other.start,
                        length,
                        root,
                    )
                )
        return nearby

    def local_stroke_contact(
        source: _Primitive, other: _Primitive, joints: tuple[Point, Point]
    ) -> bool:
        """Prove the whole capsule contact belongs to the connected bend.

        Nearby endpoints alone cannot justify ignoring a long return leg. The
        first/last contact in each direction bounds the complete intersection
        of these convex straight stroke capsules. It must fit the joint's
        stroke neighborhood, or lie wholly inside one actual convex landing.
        """
        radius = (source.trace.width_nm + other.trace.width_nm) / 2
        spans = []
        for left, right in ((source, other), (other, source)):
            cuts = tuple(
                first_exit(
                    oriented,
                    lambda p, mate=right: radius - mate.distance(p),
                    budget.consume,
                )
                for oriented in (left, _reverse_primitive(left))
            )
            first, last = cuts
            if first is None or last is None:
                return True  # No contact to turn into a topology branch.
            spans.append((left.point(first), left.point(1 - last)))
        if all(
            math.dist(point, joint) <= radius + 0.5
            for span, joint in zip(spans, joints)
            for point in span
        ):
            return True
        return any(
            feature.net == source.trace.net
            and feature.shape != "polygon"
            and all(
                _contact_distance(point, feature, budget.consume)
                + primitive.trace.width_nm / 2
                <= 0
                for span, primitive in zip(spans, (source, other))
                for point in span
            )
            for feature in features
        )

    index = _tree(own, budget)
    for source in own:
        local_path: Optional[dict[int, tuple[Point, Point]]] = None
        pending = [index]
        while pending:
            budget.consume()
            node = pending.pop()
            a, b = source.box, node.box
            separation = math.hypot(
                max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0)
            )
            if separation > source.trace.width_nm / 2 + node.half_width:
                continue
            pending.extend(node.children)
            for other in node.primitives:
                budget.consume()
                if other is source or {source.start, source.end}.intersection(
                    (other.start, other.end)
                ):
                    continue
                radius = (source.trace.width_nm + other.trace.width_nm) / 2
                a, b = source.box, other.box
                if (
                    a[0] - b[2] > radius
                    or b[0] - a[2] > radius
                    or a[1] - b[3] > radius
                    or b[1] - a[3] > radius
                ):
                    continue
                if source.center is None and other.center is None:
                    dx, dy = (
                        source.end[0] - source.start[0],
                        source.end[1] - source.start[1],
                    )
                    if dx * (other.end[1] - other.start[1]) == dy * (
                        other.end[0] - other.start[0]
                    ) and dx * (other.start[1] - source.start[1]) == dy * (
                        other.start[0] - source.start[0]
                    ):
                        continue
                    if local_path is None:
                        local_path = adjacent_stroke_neighbors(source)
                    if id(other) in local_path and local_stroke_contact(
                        source, other, local_path[id(other)]
                    ):
                        continue
                if (
                    source.center is not None
                    and source.center == other.center
                    and source.radius == other.radius
                ):
                    # Native segmentation of one circular path cannot branch.
                    continue
                for oriented, reverse in (
                    (source, False),
                    (_reverse_primitive(source), True),
                ):
                    end = first_exit(
                        oriented,
                        lambda p, mate=other, width=radius: width - mate.distance(p),
                        budget.consume,
                    )
                    if end is not None:
                        result.setdefault(id(source), []).append(
                            1 - end if reverse else end
                        )
    return result


def _contact_distance(
    point: _FPoint, feature: CopperFeature, consume: Callable[[], None]
) -> float:
    """Account for every native pad query as well as its polygon edge work."""
    consume()
    return feature_point_distance(point, feature, consume)


def _clip_terminal_transitions(
    snapshot: BoardSnapshot,
    pair: tuple[str, str],
    layer: str,
    by_trace: dict[str, tuple[_Primitive, ...]],
    budget: _Budget,
) -> tuple[dict[str, tuple[_Primitive, ...]], dict[str, int]]:
    """Exclude only connected entry prefixes inside an explicit pad-pitch region.

    A unique contacted terminal on each net, with outgoing tangents at most
    90 degrees apart, establishes one landing pair (including symmetric +/-45
    degree entries). More opposed directions remain locally unverified.
    Unconnected nearby pads never supply pitch.
    Each prefix stops at its first region exit or topology branch, irrespective
    of later parallelism, target spacing, native segmentation, or later reentry.
    """
    features = tuple(
        f for f in snapshot.copper_features if f.layer == layer and f.net in pair
    )
    if not features:
        return by_trace, {}
    # A pad contact is a physical root even when KiCad stores one unsplit
    # straight/arc through it. Split at one proven contact point, never at a
    # proximity guess; the existing exact first-exit search handles its extent.
    contact_splits: dict[int, list[float]] = {}
    for feature in features:
        own = tuple(
            primitive
            for parts in by_trace.values()
            for primitive in parts
            if primitive.trace.net == feature.net
        )
        box = feature_bounds(feature, budget.consume)
        for primitive in own:
            budget.consume()
            half = primitive.trace.width_nm / 2
            other = primitive.box
            if any(
                (
                    other[2] < box[0] - half,
                    other[0] > box[2] + half,
                    other[3] < box[1] - half,
                    other[1] > box[3] + half,
                )
            ):
                continue
            if any(
                _contact_distance(point, feature, budget.consume) <= half
                for point in (primitive.start, primitive.end)
            ):
                continue
            point = _closest_point(feature.center, primitive)
            parameter = _native_parameter(primitive, point)
            lower, upper = primitive.source_interval
            local = (parameter - lower) / (upper - lower)
            if _contact_distance(point, feature, budget.consume) > half:
                seed = contact_seed(
                    primitive,
                    lambda p, pad=feature, radius=half: _contact_distance(
                        p, pad, budget.consume
                    )
                    - radius,
                    budget.consume,
                )
                if seed is None:
                    continue
                local = seed
            if 0 < local < 1:
                contact_splits.setdefault(id(primitive), []).append(local)
    if contact_splits:
        by_trace = {
            identity: tuple(
                part
                for primitive in parts
                for cuts in [sorted({0.0, 1.0, *contact_splits.get(id(primitive), ())})]
                for start, end in zip(cuts, cuts[1:])
                for part in [
                    _slice_primitive(primitive, start, end)
                    if start or end != 1
                    else primitive
                ]
            )
            for identity, parts in by_trace.items()
        }
    all_primitives = tuple(p for values in by_trace.values() for p in values)
    excluded: dict[int, list[tuple[float, float]]] = {}
    ambiguous: dict[int, list[tuple[float, float]]] = {}
    terminals: list[_Terminal] = []

    def record(
        original: _Primitive,
        oriented: _Primitive,
        end: float,
        destination: dict[int, list[tuple[float, float]]],
    ) -> None:
        """Translate one initial oriented prefix back to native direction."""
        if end > 0:
            lower, upper = original.source_interval
            start, finish = oriented.source_interval
            interval = tuple(
                sorted(
                    (
                        (start - lower) / (upper - lower),
                        (start + (finish - start) * end - lower) / (upper - lower),
                    )
                )
            )
            destination.setdefault(id(original), []).append(interval)

    grouped_contacts: set[str] = set()
    branched_bank_nets: set[str] = set()

    def pad_pitch(first: CopperFeature, second: CopperFeature) -> float:
        """Bound bank-association work independently of footprint pin count."""
        budget.consume()
        return math.dist(first.center, second.center)

    footprints: dict[str, list[CopperFeature]] = {}
    for feature in features:
        if feature.footprint_id and any(
            primitive.trace.net == feature.net
            and _contact_distance(point, feature, budget.consume)
            <= primitive.trace.width_nm / 2
            for primitive in all_primitives
            for point in (primitive.start, primitive.end)
        ):
            footprints.setdefault(feature.footprint_id, []).append(feature)
    for pads in footprints.values():
        distances = [
            pad_pitch(first, second)
            for index, first in enumerate(pads)
            for second in pads[index + 1 :]
            if first.net != second.net and first.center != second.center
        ]
        if not distances:
            continue
        pitch = min(distances)
        neighbors = {
            index: {
                j
                for j, second in enumerate(pads)
                if first.net != second.net
                and abs(pad_pitch(first, second) - pitch) <= 1.0
            }
            for index, first in enumerate(pads)
        }
        unvisited = set(neighbors)
        while unvisited:
            pending = [min(unvisited)]
            bank = []
            while pending:
                index = pending.pop()
                budget.consume()
                if index not in unvisited:
                    continue
                unvisited.remove(index)
                bank.append(pads[index])
                pending.extend(neighbors[index] & unvisited)
            if len(bank) < 3 or len({pad.net for pad in bank}) != 2:
                continue
            # The authoritative owner and adjacent opposite-net pitch define a
            # local pin bank. Distant banks in one footprint cannot enlarge it.
            # Its duplicated-pin interconnect may leave in several directions,
            # so use one actual adjacent pitch around the real bank copper.
            region = landing_region(
                tuple(bank), (0.0, 0.0), budget.consume, radial_pitch_nm=pitch
            )
            for net in pair:
                endpoints: dict[Point, list[_Primitive]] = {}
                for primitive in all_primitives:
                    if primitive.trace.net == net:
                        for point in (primitive.start, primitive.end):
                            endpoints.setdefault(point, []).append(primitive)
                contacts = tuple(pad for pad in bank if pad.net == net)
                starts = [
                    (primitive, point)
                    for point, incident in endpoints.items()
                    for primitive in incident
                    if any(
                        _contact_distance(point, pad, budget.consume)
                        <= primitive.trace.width_nm / 2
                        for pad in contacts
                    )
                ]
                seen: set[tuple[int, Point]] = set()
                exits: set[Point] = set()
                while starts:
                    primitive, point = starts.pop()
                    budget.consume()
                    if (id(primitive), point) in seen:
                        continue
                    seen.add((id(primitive), point))
                    oriented = (
                        primitive
                        if primitive.start == point
                        else _reverse_primitive(primitive)
                    )
                    end = first_exit(
                        oriented,
                        lambda p, area=region: area.distance(p, budget.consume),
                        budget.consume,
                    )
                    record(primitive, oriented, 1.0 if end is None else end, excluded)
                    if end is None:
                        starts.extend(
                            (other, oriented.end)
                            for other in endpoints[oriented.end]
                            if other is not primitive
                        )
                    else:
                        exits.add(tuple(round(v) for v in oriented.point(end)))
                if len(exits) > 1:
                    branched_bank_nets.add(net)
            grouped_contacts.update(pad.feature_id for pad in bank)

    for net in pair:
        endpoints: dict[Point, list[_Primitive]] = {}
        for primitive in all_primitives:
            budget.consume()
            if primitive.trace.net == net:
                for endpoint in (primitive.start, primitive.end):
                    endpoints.setdefault(endpoint, []).append(primitive)
        own = tuple(p for p in all_primitives if p.trace.net == net)
        branch_cuts = (
            _terminal_branch_cuts(own, endpoints, budget, features=features)
            if own
            else {}
        )
        component_by_point: dict[Point, int] = {}
        for point in endpoints:
            if point in component_by_point:
                continue
            component = len(component_by_point)
            pending_points = [point]
            while pending_points:
                current = pending_points.pop()
                budget.consume()
                if current in component_by_point:
                    continue
                component_by_point[current] = component
                pending_points.extend(
                    endpoint
                    for part in endpoints[current]
                    for endpoint in (part.start, part.end)
                    if endpoint not in component_by_point
                )
        contact_roots: dict[str, set[Point]] = {}
        for feature in features:
            if feature.net != net or feature.feature_id in grouped_contacts:
                continue
            contacted = [
                point
                for point, incident in endpoints.items()
                if _contact_distance(point, feature, budget.consume)
                <= max(p.trace.width_nm for p in incident) / 2
            ]
            if contacted:
                closest: dict[int, float] = {}
                for point in contacted:
                    component = component_by_point[point]
                    closest[component] = min(
                        closest.get(component, float("inf")),
                        math.dist(point, feature.center),
                    )
                contact_roots[feature.feature_id] = {
                    point
                    for point in contacted
                    if math.dist(point, feature.center)
                    <= closest[component_by_point[point]] + 0.001
                }
        for endpoint, incident in endpoints.items():
            budget.consume()
            if len(incident) not in (1, 2):
                continue
            if len(incident) == 2 and not any(f.net != net for f in features):
                # One isolated testpoint supplies no differential landing pitch.
                continue
            contacts = []
            for feature in features:
                budget.consume()
                if feature.net == net and endpoint in contact_roots.get(
                    feature.feature_id, ()
                ):
                    contacts.append(feature)
            if not contacts:
                continue
            for seed in incident:
                route = []
                primitive, point = seed, endpoint
                seen: set[int] = set()
                while id(primitive) not in seen:
                    budget.consume()
                    seen.add(id(primitive))
                    oriented = (
                        primitive
                        if primitive.start == point
                        else _reverse_primitive(primitive)
                    )
                    cuts = branch_cuts.get(id(primitive), ())
                    if cuts:
                        cut = (
                            min(cuts)
                            if oriented.start == primitive.start
                            else 1 - max(cuts)
                        )
                        if cut > 0:
                            route.append(
                                (primitive, _slice_primitive(oriented, 0, cut))
                            )
                        break
                    route.append((primitive, oriented))
                    point = oriented.end
                    neighbors = endpoints[point]
                    if len(neighbors) != 2:
                        break
                    primitive = next(p for p in neighbors if p is not primitive)
                if not route:
                    continue
                direction = _curve_direction(route[0][1], 0)
                for original, oriented in route:
                    end = first_exit(
                        oriented,
                        lambda p,
                        half=original.trace.width_nm / 2,
                        pads=tuple(contacts): min(
                            _contact_distance(p, f, budget.consume) for f in pads
                        )
                        - half,
                        budget.consume,
                    )
                    record(original, oriented, 1.0 if end is None else end, excluded)
                    if end is not None:
                        direction = _curve_direction(oriented, end)
                        break
                terminals.append(
                    _Terminal(net, tuple(contacts), tuple(route), direction)
                )

    candidates: dict[int, list[int]] = {i: [] for i in range(len(terminals))}
    for i, first in enumerate(terminals):
        for j, second in enumerate(terminals[i + 1 :], i + 1):
            budget.consume()
            if (
                first.net == second.net
                or len(first.contacts) != 1
                or len(second.contacts) != 1
            ):
                continue
            # Compatible outward directions distinguish opposite board ends.
            # Require a transverse landing pair, and fail closed if more than
            # one connected terminal could supply that pair's pitch.
            dot = sum(a * b for a, b in zip(first.direction, second.direction))
            if dot < -1e-9:
                continue
            dx = first.direction[0] + second.direction[0]
            dy = first.direction[1] + second.direction[1]
            extent = math.hypot(dx, dy)
            direction = (dx / extent, dy / extent)
            delta = tuple(
                b - a
                for a, b in zip(first.contacts[0].center, second.contacts[0].center)
            )
            pitch = math.hypot(*delta)
            if (
                not pitch
                or abs(delta[0] * direction[0] + delta[1] * direction[1]) > pitch / 2
            ):
                continue
            candidates[i].append(j)
            candidates[j].append(i)
    for i, terminal in enumerate(terminals):
        if len(terminal.contacts) != 1:
            continue
        owner = terminal.contacts[0].footprint_id
        same_owner = [
            j
            for j in candidates[i]
            if owner and terminals[j].contacts[0].footprint_id == owner
        ]
        if same_owner:
            # A known native landing pair takes precedence over a geometrically
            # compatible contact elsewhere along the same routed nets.
            candidates[i] = same_owner
    reciprocal = {
        i: [j for j in possible if i in candidates[j]]
        for i, possible in candidates.items()
    }
    candidates = reciprocal
    handled: set[int] = set()
    for i, terminal in enumerate(terminals):
        if i in handled:
            continue
        possible = candidates[i]
        if len(possible) == 1 and candidates[possible[0]] == [i]:
            j = possible[0]
            mate = terminals[j]
            dx, dy = tuple(a + b for a, b in zip(terminal.direction, mate.direction))
            extent = math.hypot(dx, dy)
            region = landing_region(
                (terminal.contacts[0], mate.contacts[0]),
                (dx / extent, dy / extent),
                budget.consume,
            )
            for entry in (terminal, mate):
                for original, oriented in entry.route:
                    end = first_exit(
                        oriented,
                        lambda p, area=region: area.distance(p, budget.consume),
                        budget.consume,
                    )
                    record(original, oriented, 1.0 if end is None else end, excluded)
                    if end is not None:
                        break
            handled.update((i, j))
        else:
            # No associated pair means no justified pitch. Limit uncertainty to
            # the contacted flash's immediate extent instead of hiding routing.
            for original, oriented in terminal.route:
                end = first_exit(
                    oriented,
                    lambda p, half=original.trace.width_nm, pads=terminal.contacts: min(
                        _contact_distance(p, f, budget.consume) for f in pads
                    )
                    - half,
                    budget.consume,
                )
                record(original, oriented, 1.0 if end is None else end, ambiguous)
                if end is not None:
                    break
    result: dict[str, tuple[_Primitive, ...]] = {}
    lengths: dict[str, int] = {}
    for trace_id, values in by_trace.items():
        kept = []
        removed = 0.0
        for primitive in values:
            if primitive.trace.net in branched_bank_nets:
                ambiguous.setdefault(id(primitive), []).append((0.0, 1.0))
            intervals = excluded.get(id(primitive), ())
            unknown = ambiguous.get(id(primitive), ())
            cuts = sorted(
                {
                    0.0,
                    1.0,
                    *(
                        value
                        for interval in (*intervals, *unknown)
                        for value in interval
                    ),
                }
            )
            for start, end in zip(cuts, cuts[1:]):
                budget.consume()
                middle = (start + end) / 2
                if any(lower <= middle <= upper for lower, upper in intervals):
                    removed += (end - start) * primitive.length
                else:
                    part = (
                        _slice_primitive(primitive, start, end)
                        if start or end != 1
                        else primitive
                    )
                    unverified = any(
                        lower <= middle <= upper for lower, upper in unknown
                    )
                    kept.append(
                        replace(part, unverified_transition=unverified)
                        if unverified != part.unverified_transition
                        else part
                    )
        result[trace_id] = tuple(kept)
        lengths[trace_id] = round(removed)
    return result, lengths


def _nearest(
    point: _FPoint, source: _Primitive, node: _Node, budget: _Budget
) -> tuple[float, tuple[_Primitive, ...]]:
    """Find edge gap with conservative spatial pruning and nearest-leg ties."""
    source_half = source.trace.width_nm / 2
    pending = [(float("-inf"), 0, node)]
    best = float("inf")
    nearest: list[_Primitive] = []
    serial = 0
    while pending:
        budget.consume()
        lower, _, current = heapq.heappop(pending)
        if lower > best + 0.01:
            continue
        for primitive in current.primitives:
            budget.consume()
            value = (
                primitive.distance(point) - source_half - primitive.trace.width_nm / 2
            )
            if value < best - 0.001:
                best, nearest = value, [primitive]
            elif abs(value - best) <= 0.001:
                best = min(best, value)
                nearest.append(primitive)
        for child in current.children:
            serial += 1
            bound = _box_distance(point, child.box) - source_half - child.half_width
            heapq.heappush(pending, (bound, serial, child))
    return best, tuple(nearest)


def _competing(primitives: tuple[_Primitive, ...]) -> bool:
    """Adjacent endpoint candidates are continuous; disjoint nearest legs are not."""
    return any(
        not {a.start, a.end}.intersection((b.start, b.end))
        and a.trace.trace_id != b.trace.trace_id
        for i, a in enumerate(primitives)
        for b in primitives[i + 1 :]
    )


def _as_bounds(lower: float, upper: float) -> NmBounds:
    """Round outward, keeping arithmetic uncertainty separate from the band."""
    return NmBounds(math.floor(lower), math.ceil(upper))


def _decimal_bounds(value: Decimal) -> NmBounds:
    """Outward-round a high-precision analytic dimensional result."""
    return NmBounds(
        int(value.to_integral_value(rounding="ROUND_FLOOR")),
        int(value.to_integral_value(rounding="ROUND_CEILING")),
    )


def _parallel_gap(
    source: _Primitive,
    mates: tuple[_Primitive, ...],
    budget: _Budget,
    *,
    corner_tolerance_nm: float = 0.0,
) -> Optional[NmBounds]:
    """Prove constant spacing over a line, including segmented parallel mates."""
    if source.center is not None:
        return None
    dx, dy = source.end[0] - source.start[0], source.end[1] - source.start[1]
    square = dx * dx + dy * dy
    groups: dict[tuple[float, int], list[tuple[float, float]]] = {}
    axis_distances: dict[tuple[float, int], Decimal] = {}
    other_paths = []
    for mate in mates:
        budget.consume()
        if mate.center is not None or dx * (mate.end[1] - mate.start[1]) != dy * (
            mate.end[0] - mate.start[0]
        ):
            other_paths.append(mate)
            continue
        normal = dx * (mate.start[1] - source.start[1]) - dy * (
            mate.start[0] - source.start[0]
        )
        values = tuple(
            (point[0] - source.start[0]) * dx + (point[1] - source.start[1]) * dy
            for point in (mate.start, mate.end)
        )
        if min(values) >= square or max(values) <= 0:
            other_paths.append(mate)
            continue
        key = (normal, mate.trace.width_nm)
        groups.setdefault(key, []).append((min(values), max(values)))
        if dx == 0 or dy == 0:
            axis = 0 if dx == 0 else 1
            axis_distances[key] = abs(
                Decimal(str(mate.start[axis])) - Decimal(str(source.start[axis]))
            )
    if not groups:
        return None
    with localcontext() as context:
        context.prec = 64
        denominator = Decimal(square).sqrt() if dx and dy else Decimal(abs(dx or dy))
        distances = {
            key: (
                axis_distances[key]
                if key in axis_distances
                else Decimal(abs(key[0])) / denominator
            )
            - Decimal(source.trace.width_nm + key[1]) / 2
            for key in groups
        }
        best = min(distances.values())
        best_groups = [key for key, value in distances.items() if value == best]
        # Equal nearest legs on opposite sides have ambiguous correspondence.
        if len(best_groups) != 1:
            return None
        intervals = sorted(groups[best_groups[0]])
        covered = 0
        for start, end in intervals:
            budget.consume()
            if start > covered:
                break
            covered = max(covered, end)
        if covered < square:
            return None
        proven_lower = float(best)
        for mate in other_paths:
            budget.consume()
            if mate.center is None:
                lower = _segment_distance(
                    source.start, source.end, mate.start, mate.end
                )
            else:
                box, other = source.box, mate.box
                lower = math.hypot(
                    max(box[0] - other[2], other[0] - box[2], 0),
                    max(box[1] - other[3], other[1] - box[3], 0),
                )
            lower -= (source.trace.width_nm + mate.trace.width_nm) / 2
            if lower < float(best) - corner_tolerance_nm - 0.001:
                return None
            proven_lower = min(proven_lower, lower)
        if corner_tolerance_nm and proven_lower < float(best):
            return NmBounds(math.floor(proven_lower), _decimal_bounds(best).upper_nm)
        return _decimal_bounds(best)


def _corner_extended_mates(
    source: _Primitive,
    mates: tuple[_Primitive, ...],
    own: tuple[_Primitive, ...],
    budget: _Budget,
) -> tuple[_Primitive, ...]:
    """Complete only analytically corresponding mitered sidewalls.

    The two adjacent support lines must have the same signed normal offset
    before and after the same turn. Their intersections define an offset miter;
    a finite rounded endpoint is not the facing sidewall in that corner. No
    arbitrary extrapolation is made at route ends, divergent bends or branches.
    Extended primitives are used for this constant-gap proof only. Locations
    and rendered witnesses always retain the real shared sidewall intervals.
    """
    if source.center is not None:
        return mates
    own_endpoints: dict[Point, list[_Primitive]] = {}
    mate_endpoints: dict[Point, list[_Primitive]] = {}
    for primitives, endpoints in ((own, own_endpoints), (mates, mate_endpoints)):
        for primitive in primitives:
            budget.consume()
            for point in (primitive.start, primitive.end):
                endpoints.setdefault(point, []).append(primitive)
    direction = _curve_direction(source, 0)
    result = []
    for mate in mates:
        budget.consume()
        if mate.center is not None:
            result.append(mate)
            continue
        mdir = _curve_direction(mate, 0)
        if abs(direction[0] * mdir[1] - direction[1] * mdir[0]) > 1e-12:
            result.append(mate)
            continue
        start, end = mate.start, mate.end
        for endpoint, sign in ((source.start, -1), (source.end, 1)):
            source_neighbors = [
                p
                for p in own_endpoints.get(endpoint, ())
                if p is not source
                and (
                    p.trace.trace_id != source.trace.trace_id
                    or min(max(p.source_interval), max(source.source_interval))
                    <= max(min(p.source_interval), min(source.source_interval))
                )
            ]
            if len(source_neighbors) != 1:
                continue
            neighbor = source_neighbors[0]
            if (
                neighbor.center is not None
                or neighbor.trace.width_nm != source.trace.width_nm
            ):
                continue
            outgoing = _curve_direction(
                neighbor
                if neighbor.start == endpoint
                else _reverse_primitive(neighbor),
                0,
            )
            incoming = (direction[0] * sign, direction[1] * sign)
            turn = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
            if abs(turn) < 1e-12:
                continue
            for mate_endpoint in (mate.start, mate.end):
                continuation = [
                    p for p in mate_endpoints[mate_endpoint] if p is not mate
                ]
                if len(continuation) != 1:
                    continue
                other = continuation[0]
                if (
                    other.center is not None
                    or other.trace.width_nm != mate.trace.width_nm
                ):
                    continue
                mout = _curve_direction(
                    other
                    if other.start == mate_endpoint
                    else _reverse_primitive(other),
                    0,
                )
                if math.dist(outgoing, mout) > 1e-12:
                    continue
                # A corner with this source orientation must arrive at the
                # candidate mate endpoint, rather than leave it along this leg.
                inward = mate.end if mate_endpoint == mate.start else mate.start
                if (mate_endpoint[0] - inward[0]) * incoming[0] + (
                    mate_endpoint[1] - inward[1]
                ) * incoming[1] <= 0:
                    continue
                delta = (mate_endpoint[0] - endpoint[0], mate_endpoint[1] - endpoint[1])
                before = incoming[0] * delta[1] - incoming[1] * delta[0]
                after = outgoing[0] * delta[1] - outgoing[1] * delta[0]
                # Integer native vertices may round a diagonal miter by <1 nm.
                if abs(before - after) > 1.0:
                    continue
                projection = (endpoint[0] - mate_endpoint[0]) * direction[0] + (
                    endpoint[1] - mate_endpoint[1]
                ) * direction[1]
                if projection * sign <= 0:
                    continue
                extended = (
                    mate_endpoint[0] + projection * direction[0],
                    mate_endpoint[1] + projection * direction[1],
                )
                if mate_endpoint == mate.start:
                    start = extended
                else:
                    end = extended
        result.append(replace(mate, start=start, end=end))
    return tuple(result)


def _radius_decimal(primitive: _Primitive) -> Decimal:
    """Reconstruct a circle radius from its exact rational center."""
    assert primitive.center is not None
    square = sum(
        (Fraction(value) - center) ** 2
        for value, center in zip(primitive.trace.points[0], primitive.center)
    )
    return (Decimal(square.numerator) / Decimal(square.denominator)).sqrt()


def _concentric_gap(
    source: _Primitive, mates: tuple[_Primitive, ...], budget: _Budget
) -> Optional[NmBounds]:
    """Prove constant radial spacing where a mate covers a native arc sweep."""
    if source.center is None:
        return None
    for mate in mates:
        budget.consume()
        if mate.center != source.center:
            return None
    with localcontext() as context:
        context.prec = 64
        radius = _radius_decimal(source)
        gaps = [
            (
                abs(radius - _radius_decimal(mate))
                - Decimal(source.trace.width_nm + mate.trace.width_nm) / 2,
                mate,
            )
            for mate in mates
        ]
        best = min(gap for gap, _ in gaps)
        candidates = [mate for gap, mate in gaps if gap == best]
        # Physical sweep coverage is independent of native track direction.
        # Adjacent segments on one radial path can jointly cover the source.
        radial_paths = {
            (_radius_decimal(mate), mate.trace.width_nm) for mate in candidates
        }
        if len(radial_paths) != 1:
            return None
        origin = source.angle + min(0.0, source.sweep)
        extent = abs(source.sweep)
        intervals = []
        for mate in candidates:
            budget.consume()
            start = (mate.angle + min(0.0, mate.sweep) - origin) % math.tau
            for shift in (0.0, -math.tau):
                lower, upper = start + shift, start + shift + abs(mate.sweep)
                if upper >= 0 and lower <= extent:
                    intervals.append((max(0.0, lower), min(extent, upper)))
        covered = 0.0
        for start, end in sorted(intervals):
            if start > covered:
                return None
            covered = max(covered, end)
        if covered >= extent:
            return _decimal_bounds(best)
    return None


def _shared_curved_contact(a: _Primitive, b: _Primitive, shared: Point) -> bool:
    """Conservatively exclude a second contact beyond a common endpoint."""
    if a.center is None and b.center is None:
        return False
    if a.center is None:
        a, b = b, a
    assert a.center is not None
    if b.center is None:
        other = b.end if b.start == shared else b.start
        direction = (other[0] - shared[0], other[1] - shared[1])
        radial = (Fraction(shared[0]) - a.center[0], Fraction(shared[1]) - a.center[1])
        dot = radial[0] * direction[0] + radial[1] * direction[1]
        square = direction[0] ** 2 + direction[1] ** 2
        # A tangent or outward ray cannot reenter the circle. For an inward
        # secant, its second full-circle intersection must be outside the line.
        return dot < 0 and -2 * dot < square
    if a.center == b.center:
        a_direction = (1 if a.sweep > 0 else -1) * (1 if a.start == shared else -1)
        b_direction = (1 if b.sweep > 0 else -1) * (1 if b.start == shared else -1)
        return a_direction == b_direction or abs(a.sweep) + abs(b.sweep) > math.tau
    radial_a = (Fraction(shared[0]) - a.center[0], Fraction(shared[1]) - a.center[1])
    radial_b = (Fraction(shared[0]) - b.center[0], Fraction(shared[1]) - b.center[1])
    # Distinct tangent circles have only one contact. Other curved contacts
    # need a sweep-aware two-circle proof, so remain explicitly unverified.
    return radial_a[0] * radial_b[1] != radial_a[1] * radial_b[0]


def _topology_reasons(
    primitives: tuple[_Primitive, ...],
    budget: _Budget,
    *,
    connectivity: tuple[_Primitive, ...] = (),
) -> tuple[str, ...]:
    """Detect branches and interior contacts without an all-pairs route scan."""
    endpoints: dict[Point, list[_Primitive]] = {}
    for primitive in primitives:
        budget.consume()
        for point in (primitive.start, primitive.end):
            endpoints.setdefault(point, []).append(primitive)
    if any(len(incident) > 2 for incident in endpoints.values()):
        return ("Branched same-net routing has ambiguous pair correspondence.",)
    if not primitives:
        return ()
    # Native identity alone cannot establish which disconnected island continues
    # the pair. A nearest-leg switch can occur between every chosen sample, so
    # correspondence must not depend on observing an exact equidistant point.
    connected = {0}
    pending_indices = [0]
    primitive_indices = {
        id(primitive): index for index, primitive in enumerate(primitives)
    }
    by_endpoint = {
        point: tuple(primitive_indices[id(p)] for p in incident)
        for point, incident in endpoints.items()
    }
    while pending_indices:
        budget.consume()
        index = pending_indices.pop()
        primitive = primitives[index]
        for endpoint in (primitive.start, primitive.end):
            for neighbor in by_endpoint[endpoint]:
                if neighbor not in connected:
                    connected.add(neighbor)
                    pending_indices.append(neighbor)
    if len(connected) != len(primitives):
        # Exempt inline pad contact can separate two assessed portions of the
        # same physical native route. Its removed copper remains a connectivity
        # bridge, without participating in spacing or branch verdicts.
        native_endpoints: dict[Point, list[_Primitive]] = {}
        for part in connectivity:
            budget.consume()
            for point in (part.start, part.end):
                native_endpoints.setdefault(point, []).append(part)
        pending_native = [
            part
            for part in connectivity
            if part.trace.trace_id == primitives[0].trace.trace_id
        ]
        native_seen: set[int] = set()
        native_ids: set[str] = set()
        while pending_native:
            part = pending_native.pop()
            budget.consume()
            if id(part) in native_seen:
                continue
            native_seen.add(id(part))
            native_ids.add(part.trace.trace_id)
            for point in (part.start, part.end):
                pending_native.extend(
                    neighbor
                    for neighbor in native_endpoints[point]
                    if id(neighbor) not in native_seen
                )
        if not {part.trace.trace_id for part in primitives}.issubset(native_ids):
            return (
                "Disconnected same-net route components have unverified pair correspondence.",
            )
    tree = _tree(primitives, budget)
    for point in endpoints:
        pending = [tree]
        while pending:
            budget.consume()
            node = pending.pop()
            if _box_distance(point, node.box) > 0.001:
                continue
            pending.extend(node.children)
            for other in node.primitives:
                budget.consume()
                if point in (other.start, other.end):
                    continue
                box = other.box
                if (
                    box[0] - point[0] > 0.001
                    or point[0] - box[2] > 0.001
                    or box[1] - point[1] > 0.001
                    or point[1] - box[3] > 0.001
                ):
                    continue
                if other.distance(point) <= 0.001:
                    return (
                        "Interior same-net junction has ambiguous pair correspondence.",
                    )
    for primitive in primitives:
        box = primitive.box
        pending = [tree]
        while pending:
            budget.consume()
            node = pending.pop()
            if any(
                (
                    box[2] < node.box[0],
                    node.box[2] < box[0],
                    box[3] < node.box[1],
                    node.box[3] < box[1],
                )
            ):
                continue
            pending.extend(node.children)
            for other in node.primitives:
                budget.consume()
                if (other.trace.trace_id, other.start) <= (
                    primitive.trace.trace_id,
                    primitive.start,
                ):
                    continue
                shared = {primitive.start, primitive.end}.intersection(
                    (other.start, other.end)
                )
                if shared:
                    if any(
                        _shared_curved_contact(primitive, other, point)
                        for point in shared
                    ):
                        return (
                            "Adjacent curved same-net geometry has an unverified second contact.",
                        )
                    continue
                other_box = other.box
                if any(
                    (
                        box[2] < other_box[0],
                        other_box[2] < box[0],
                        box[3] < other_box[1],
                        other_box[3] < box[1],
                    )
                ):
                    continue
                if primitive.center is not None or other.center is not None:
                    return (
                        "Nonadjacent curved same-net geometry has unverified crossing correspondence.",
                    )
                if (
                    _segment_distance(
                        primitive.start, primitive.end, other.start, other.end
                    )
                    <= 0.001
                ):
                    return (
                        "Interior same-net crossing has ambiguous pair correspondence.",
                    )
    return ()


def _interval_bounds(
    source: _Primitive,
    mates: tuple[_Primitive, ...],
    start: float,
    end: float,
    samples: tuple[float, float, float],
    budget: _Budget,
    error: float,
) -> tuple[float, float]:
    """Enclose every gap between samples, never using samples as coverage proof."""
    length = source.length * (end - start)
    low, high = min(samples) - length / 4, max(samples) + length / 4
    if source.center is None and all(mate.center is None for mate in mates):
        for _ in range(4 * len(mates)):
            budget.consume()
        a, b = source.point(start), source.point(end)
        # Distance to a convex segment is convex along a straight source.
        low = min(
            _segment_distance(a, b, mate.start, mate.end)
            - (source.trace.width_nm + mate.trace.width_nm) / 2
            for mate in mates
        )
        high = min(
            max(mate.distance(a), mate.distance(b))
            - (source.trace.width_nm + mate.trace.width_nm) / 2
            for mate in mates
        )
    return low - error, high + error


def _closest_point(point: _FPoint, primitive: _Primitive) -> _FPoint:
    """Return a finite native mate centerline projection for overlay alignment."""
    if primitive.center is not None:
        angle = math.atan2(
            point[1] - float(primitive.center[1]), point[0] - float(primitive.center[0])
        )
        if primitive.contains_angle(angle):
            return (
                float(primitive.center[0]) + primitive.radius * math.cos(angle),
                float(primitive.center[1]) + primitive.radius * math.sin(angle),
            )
        return min(
            (primitive.start, primitive.end),
            key=lambda candidate: math.dist(point, candidate),
        )
    dx, dy = (
        primitive.end[0] - primitive.start[0],
        primitive.end[1] - primitive.start[1],
    )
    square = dx * dx + dy * dy
    t = (
        max(
            0.0,
            min(
                1.0,
                (
                    (point[0] - primitive.start[0]) * dx
                    + (point[1] - primitive.start[1]) * dy
                )
                / square,
            ),
        )
        if square
        else 0.0
    )
    return primitive.point(t)


def _ray_crosses_box(origin: _FPoint, direction: _FPoint, box: _Box) -> bool:
    """Test the finite source bounding box against a radial projection ray."""
    lower, upper = 0.0, float("inf")
    for axis in (0, 1):
        if abs(direction[axis]) < 1e-15:
            if not box[axis] <= origin[axis] <= box[axis + 2]:
                return False
        else:
            ends = sorted(
                (
                    (box[axis] - origin[axis]) / direction[axis],
                    (box[axis + 2] - origin[axis]) / direction[axis],
                )
            )
            lower, upper = max(lower, ends[0]), min(upper, ends[1])
            if lower > upper:
                return False
    return True


def _projected_arc_extrema(mate: _Primitive, box: _Box) -> tuple[_FPoint, ...]:
    """Bound only the finite radial projection of a source box onto a native arc.

    A radial projection's axis extrema occur on cardinal rays, at a finite arc
    endpoint, or at a projected box corner. If the source box contains the
    circle center, all sweep directions are possible and its full arc applies.
    """
    assert mate.center is not None
    center = tuple(float(value) for value in mate.center)
    corners = ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3]))
    points = [_closest_point(point, mate) for point in corners]
    if box[0] <= center[0] <= box[2] and box[1] <= center[1] <= box[3]:
        other = mate.box
        return ((other[0], other[1]), (other[2], other[3]))
    for angle in (
        0.0,
        math.pi / 2,
        math.pi,
        3 * math.pi / 2,
        mate.angle,
        mate.angle + mate.sweep,
    ):
        direction = (math.cos(angle), math.sin(angle))
        if mate.contains_angle(angle) and _ray_crosses_box(center, direction, box):
            points.append(
                (
                    center[0] + mate.radius * direction[0],
                    center[1] + mate.radius * direction[1],
                )
            )
    return tuple(points)


def _native_parameter(primitive: _Primitive, point: _FPoint) -> float:
    """Map an on-primitive projection to its complete native trace interval."""
    if primitive.center is None:
        dx, dy = (
            primitive.end[0] - primitive.start[0],
            primitive.end[1] - primitive.start[1],
        )
        parameter = (
            (point[0] - primitive.start[0]) * dx + (point[1] - primitive.start[1]) * dy
        ) / (dx * dx + dy * dy)
    else:
        angle = math.atan2(
            point[1] - float(primitive.center[1]), point[0] - float(primitive.center[0])
        )
        offset = (
            (angle - primitive.angle)
            if primitive.sweep > 0
            else (primitive.angle - angle)
        ) % math.tau
        parameter = offset / abs(primitive.sweep)
        if math.dist(point, primitive.start) <= 1e-5:
            parameter = 0.0
        elif math.dist(point, primitive.end) <= 1e-5:
            parameter = 1.0
    lower, upper = primitive.source_interval
    return lower + (upper - lower) * max(0.0, min(1.0, parameter))


def _projection_samples(source: _Primitive, mate: _Primitive) -> tuple[_FPoint, ...]:
    """Include all stationary directions of the physical closest projection."""
    parameters = {0.0, 1.0}
    if source.center is not None and source.sweep:
        angles = []
        if mate.center is None:
            angle = math.atan2(mate.end[1] - mate.start[1], mate.end[0] - mate.start[0])
            angles.extend((angle, angle + math.pi))
        else:
            dx, dy = (
                float(source.center[0] - mate.center[0]),
                float(source.center[1] - mate.center[1]),
            )
            extent = math.hypot(dx, dy)
            if extent >= source.radius and extent:
                angle = math.atan2(dy, dx)
                offset = math.acos(-source.radius / extent)
                angles.extend((angle - offset, angle + offset))
        for angle in angles:
            offset = (
                (angle - source.angle) if source.sweep > 0 else (source.angle - angle)
            ) % math.tau
            parameter = offset / abs(source.sweep)
            if 0 < parameter < 1:
                parameters.add(parameter)
    return tuple(_closest_point(source.point(t), mate) for t in sorted(parameters))


def _location(
    source: _Primitive, mates: tuple[_Primitive, ...], start: float, end: float
) -> SpacingLocation:
    """Retain a compact native interval and nearest mate line for a visual mark."""
    parameters = {start, (start + end) / 2, end}
    sliced = _slice_primitive(source, start, end)
    if source.center is not None:
        for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
            offset = (
                (angle - source.angle) % math.tau
                if source.sweep > 0
                else (source.angle - angle) % math.tau
            )
            parameter = offset / abs(source.sweep)
            if start < parameter < end:
                parameters.add(parameter)
    samples = tuple(source.point(t) for t in sorted(parameters))
    source_points = tuple(tuple(round(v) for v in point) for point in samples)
    cross_sections = tuple(
        SpacingCrossSection(
            source.trace.trace_id,
            mate.trace.trace_id,
            source_point,
            tuple(round(v) for v in _closest_point(point, mate)),
            source.trace.width_nm,
            mate.trace.width_nm,
        )
        for point, source_point in zip(samples, source_points)
        for mate in [
            min(
                mates,
                key=lambda candidate: candidate.distance(point)
                - candidate.trace.width_nm / 2,
            )
        ]
    )
    mate_points = tuple(section.mate_point for section in cross_sections)
    # A native arc can extend beyond all three ordinary samples. Include exact
    # extrema, and conservatively enclose every finite mate projection of its
    # axis-aligned box rather than trusting a sampled visual witness.
    box = sliced.box
    corners = ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3]))
    extra: list[_FPoint] = []
    for mate in mates:
        if mate.center is None:
            extra.extend(_closest_point(point, mate) for point in corners)
        elif source.center == mate.center:
            projected = replace(
                mate,
                angle=sliced.angle,
                sweep=sliced.sweep,
                start=_closest_point(sliced.start, mate),
                end=_closest_point(sliced.end, mate),
            )
            other = projected.box
            extra.extend(((other[0], other[1]), (other[2], other[3])))
        elif start != end:
            extra.extend(_projected_arc_extrema(mate, box))
    points = (*source_points, *mate_points, *extra, *corners)
    padding = math.ceil(
        max(source.trace.width_nm, *(mate.trace.width_nm for mate in mates)) / 2
    )
    lower, upper = source.source_interval
    return SpacingLocation(
        source.trace.trace_id,
        tuple(sorted({mate.trace.trace_id for mate in mates})),
        tuple(sorted((lower + (upper - lower) * start, lower + (upper - lower) * end))),
        source_points,
        mate_points,
        (
            math.floor(min(p[0] for p in points)) - padding,
            math.floor(min(p[1] for p in points)) - padding,
            math.ceil(max(p[0] for p in points)) + padding,
            math.ceil(max(p[1] for p in points)) + padding,
        ),
        tuple(
            (mate.trace.trace_id, min(parameters), max(parameters))
            for mate in mates
            for parameters in [
                tuple(
                    _native_parameter(mate, point)
                    for point in _projection_samples(sliced, mate)
                )
            ]
        ),
        cross_sections,
    )


def _candidate_mates(
    source: _Primitive,
    mates: tuple[_Primitive, ...],
    budget: _Budget,
    *,
    index: Optional[_Node] = None,
) -> tuple[_Primitive, ...]:
    """Prune using a proven whole-span bound and the existing mate spatial tree.

    One actual midpoint-nearest native primitive supplies a conservative upper
    bound; it need not be the nearest path over the rest of the source. Tree
    bounds then retain every mate that could be closer anywhere on the span.
    """
    seeds = mates
    if index is not None and len(mates) > 8:
        _, seeds = _nearest(source.point(0.5), source, index, budget)
    upper = float("inf")
    for mate in seeds:
        budget.consume()
        bound = (
            max(mate.distance(source.start), mate.distance(source.end))
            - (source.trace.width_nm + mate.trace.width_nm) / 2
        )
        if source.center is not None or mate.center is not None:
            bound += source.length / 2
        upper = min(upper, bound)
    box = source.box

    def lower(other: _Box, width: float) -> float:
        """Bound centerline-box separation minus both native half widths."""
        return (
            math.hypot(
                max(box[0] - other[2], other[0] - box[2], 0),
                max(box[1] - other[3], other[1] - box[3], 0),
            )
            - source.trace.width_nm / 2
            - width
        )

    retained = []
    pending = [index] if index is not None and len(mates) > 8 else []
    if pending:
        while pending:
            node = pending.pop()
            budget.consume()
            if lower(node.box, node.half_width) > upper + 0.01:
                continue
            pending.extend(node.children)
            for mate in node.primitives:
                budget.consume()
                if lower(mate.box, mate.trace.width_nm / 2) <= upper + 0.01:
                    retained.append(mate)
        return tuple(
            sorted(retained, key=lambda p: (p.trace.trace_id, p.source_interval))
        )
    for mate in mates:
        budget.consume()
        if lower(mate.box, mate.trace.width_nm / 2) <= upper + 0.01:
            retained.append(mate)
    return tuple(retained)


def _constant_path_mates(
    source: _Primitive, mates: tuple[_Primitive, ...], budget: _Budget
) -> tuple[_Primitive, ...]:
    """Retain only the analytic nearest path that establishes constant spacing.

    Spatial boxes for concentric circles overlap even when their radial paths
    are far apart. A candidate's box therefore cannot identify the witness path.
    Segmented nearest paths remain complete; nonoverlapping continuations do not
    enlarge the physical issue location.
    """
    with localcontext() as context:
        context.prec = 64
        records = []
        if source.center is not None:
            radius = _radius_decimal(source)
            origin = source.angle + min(0.0, source.sweep)
            extent = abs(source.sweep)
            for mate in mates:
                budget.consume()
                gap = (
                    abs(radius - _radius_decimal(mate))
                    - Decimal(source.trace.width_nm + mate.trace.width_nm) / 2
                )
                start = (mate.angle + min(0.0, mate.sweep) - origin) % math.tau
                overlaps = any(
                    min(extent, start + shift + abs(mate.sweep))
                    > max(0.0, start + shift)
                    for shift in (0.0, -math.tau)
                )
                records.append((gap, mate, overlaps))
        else:
            dx, dy = source.end[0] - source.start[0], source.end[1] - source.start[1]
            square = dx * dx + dy * dy
            denominator = (
                Decimal(square).sqrt() if dx and dy else Decimal(abs(dx or dy))
            )
            for mate in mates:
                budget.consume()
                if mate.center is not None or dx * (
                    mate.end[1] - mate.start[1]
                ) != dy * (mate.end[0] - mate.start[0]):
                    continue
                normal = dx * (mate.start[1] - source.start[1]) - dy * (
                    mate.start[0] - source.start[0]
                )
                gap = (
                    Decimal(abs(normal)) / denominator
                    - Decimal(source.trace.width_nm + mate.trace.width_nm) / 2
                )
                projections = tuple(
                    (
                        (point[0] - source.start[0]) * dx
                        + (point[1] - source.start[1]) * dy
                    )
                    / square
                    for point in (mate.start, mate.end)
                )
                overlaps = min(1.0, max(projections)) > max(0.0, min(projections))
                records.append((gap, mate, overlaps))
        nearest = min((gap for gap, _, overlaps in records if overlaps), default=None)
        return tuple(
            mate for gap, mate, overlaps in records if gap == nearest and overlaps
        )


def _measure_primitive(
    source: _Primitive,
    mates: tuple[_Primitive, ...],
    index: _Node,
    budget: _Budget,
    own: tuple[_Primitive, ...] = (),
) -> tuple[NmBounds, NmBounds, tuple[SpacingIssue, ...], tuple[str, ...]]:
    """Bound route extrema by adaptive global optimization with witness retention."""
    native_mates = mates
    mates = _candidate_mates(source, mates, budget, index=index)
    constant = _parallel_gap(source, mates, budget) or _concentric_gap(
        source, mates, budget
    )
    if constant is None and own:
        corner_mates = _corner_extended_mates(source, native_mates, own, budget)
        if corner_mates != native_mates:
            constant = _parallel_gap(
                source, corner_mates, budget, corner_tolerance_nm=1.0
            )
    if constant is not None:
        location_mates = _constant_path_mates(source, mates, budget)
        intervals = [(location_mates, 0.0, 1.0)]
        if source.center is None:
            dx, dy = source.end[0] - source.start[0], source.end[1] - source.start[1]
            square = dx * dx + dy * dy
            intervals = []
            for mate in location_mates:
                parameters = tuple(
                    (
                        (point[0] - source.start[0]) * dx
                        + (point[1] - source.start[1]) * dy
                    )
                    / square
                    for point in (mate.start, mate.end)
                )
                lower, upper = max(0.0, min(parameters)), min(1.0, max(parameters))
                if lower < upper:
                    intervals.append(((mate,), lower, upper))
        witnesses = tuple(
            SpacingIssue(
                "measured",
                (source.trace.trace_id,),
                tuple(round(v) for v in source.point(start)),
                constant,
                "Constant facing-sidewall spacing.",
                _location(source, candidates, start, end),
            )
            for candidates, start, end in intervals
        )
        return constant, constant, witnesses, ()
    index = _tree(mates, budget)
    error = 0.001
    for primitive in (source, *mates):
        budget.consume()
        error = max(
            error,
            max(abs(v) for v in (*primitive.start, *primitive.end)) * 1e-13,
            primitive.radius * 1e-13,
        )
    values: dict[float, tuple[float, tuple[_Primitive, ...]]] = {}
    reasons: set[str] = set()
    observed_low, observed_high = float("inf"), float("-inf")

    def evaluate(t: float) -> float:
        """Cache target-independent samples and detect competing mate legs."""
        nonlocal observed_low, observed_high
        if t not in values:
            values[t] = _nearest(source.point(t), source, index, budget)
            observed_low = min(observed_low, values[t][0])
            observed_high = max(observed_high, values[t][0])
        return values[t][0]

    pending: list[tuple[float, float, float, float]] = []
    retained: list[tuple[float, float, float, float]] = []
    exhausted = False
    try:
        first = (evaluate(0), evaluate(0.5), evaluate(1))
        lo, hi = _interval_bounds(source, mates, 0, 1, first, budget, error)
        pending.append((0, 1, lo, hi))
        while pending:
            start, end, lo, hi = pending.pop()
            precision = budget.limits.precision_nm
            if (
                lo >= observed_low - precision
                and hi <= observed_high + precision
                and hi - lo <= 500
            ):
                retained.append((start, end, lo, hi))
                continue
            budget.intervals += 1
            if budget.intervals > budget.limits.max_intervals:
                pending.append((start, end, lo, hi))
                raise _BudgetExhausted
            middle = (start + end) / 2
            for left, right in ((start, middle), (middle, end)):
                samples = (
                    evaluate(left),
                    evaluate((left + right) / 2),
                    evaluate(right),
                )
                lower, upper = _interval_bounds(
                    source, mates, left, right, samples, budget, error
                )
                pending.append((left, right, lower, upper))
    except _BudgetExhausted:
        exhausted = True
        reasons.add(
            "Spacing measurement work limit reached; remaining geometry is unverified."
        )
    if not values:
        raise _BudgetExhausted
    # Unfinished intervals have no established global bounds, but the measured
    # points remain valid tight witnesses. Their union cannot prove a wide gap.
    if exhausted:
        lo = -(source.trace.width_nm + max(m.trace.width_nm for m in mates)) / 2
        hi = max(value[0] for value in values.values()) + source.length
    else:
        lo = min(item[2] for item in retained)
        hi = max(item[3] for item in retained)
    minimum_t = min(values, key=lambda t: (values[t][0], t))
    maximum_t = max(values, key=lambda t: (values[t][0], -t))
    minimum = _as_bounds(lo, values[minimum_t][0] + error)
    maximum = _as_bounds(values[maximum_t][0] - error, hi)
    witnesses = tuple(
        SpacingIssue(
            "measured",
            tuple(
                dict.fromkeys(
                    (source.trace.trace_id, *(p.trace.trace_id for p in values[t][1]))
                )
            ),
            tuple(round(v) for v in source.point(t)),
            _as_bounds(values[t][0] - error, values[t][0] + error),
            "Measured edge-to-edge spacing witness.",
            _location(source, values[t][1], t, t),
        )
        for t in dict.fromkeys((minimum_t, maximum_t))
    )
    if not exhausted:
        witnesses = tuple(
            SpacingIssue(
                "measured",
                tuple(
                    dict.fromkeys(
                        (
                            source.trace.trace_id,
                            *(p.trace.trace_id for p in values[(start + end) / 2][1]),
                        )
                    )
                ),
                tuple(round(v) for v in source.point((start + end) / 2)),
                _as_bounds(lower, upper),
                "Measured edge-to-edge spacing.",
                _location(source, values[(start + end) / 2][1], start, end),
            )
            for start, end, lower, upper in sorted(retained)
        )
    return minimum, maximum, witnesses, tuple(sorted(reasons))


def _merge_bounds(bounds: Sequence[NmBounds], *, minimum: bool) -> Optional[NmBounds]:
    """Combine outward extrema intervals without inventing precision."""
    if not bounds:
        return None
    operation = min if minimum else max
    return NmBounds(
        operation(value.lower_nm for value in bounds),
        operation(value.upper_nm for value in bounds),
    )


def _trace_length(trace: Trace) -> int:
    """Use native arc length where available, render length for unverified fallback."""
    primitives = _primitives(trace)
    return (
        round(sum(p.length for p in primitives))
        if primitives
        else round(sum(math.dist(a, b) for a, b in zip(trace.points, trace.points[1:])))
    )


def measure_pair_spacing(
    snapshot: BoardSnapshot,
    pair: tuple[str, str],
    layer: str,
    *,
    limits: SpacingLimits = SpacingLimits(),
) -> SpacingMeasurement:
    """Measure both native mates on one layer, independent of target or stackup."""
    pair = tuple(sorted(pair))
    if (
        len(pair) != 2
        or pair[0] == pair[1]
        or snapshot.differential_pair_error
        or pair not in tuple(tuple(sorted(p)) for p in snapshot.differential_pairs)
    ):
        return SpacingMeasurement(
            pair,
            layer,
            (),
            ("No unambiguous authoritative native differential pair is available.",),
        )
    if sum(net in p for p in snapshot.differential_pairs for net in pair) != 2:
        return SpacingMeasurement(
            pair, layer, (), ("Native differential pair metadata is ambiguous.",)
        )
    selected = tuple(
        sorted(
            (
                trace
                for trace in snapshot.traces
                if trace.layer == layer and trace.net in pair
            ),
            key=lambda trace: trace.trace_id,
        )
    )
    budget = _Budget(limits)
    try:
        by_trace = {}
        for trace in selected:
            budget.consume()
            parts = _primitives(trace)
            total = sum(p.length for p in parts)
            cursor = 0.0
            normalized = []
            for primitive in parts:
                end = cursor + primitive.length / total
                interval = (cursor, end)
                normalized.append(
                    replace(primitive, source_interval=interval)
                    if interval != primitive.source_interval
                    else primitive
                )
                cursor = end
            by_trace[trace.trace_id] = tuple(normalized)
        # Distance queries use the actual native mate copper. Clipping a mate's
        # entry prefix would invent an end cap and change the physical gap for
        # an outside source whose normal mate projection is still in that area.
        native_by_trace = by_trace
        by_trace, transitions = _clip_terminal_transitions(
            snapshot, pair, layer, by_trace, budget
        )
        primitives = {
            net: tuple(
                p
                for trace in selected
                if trace.net == net
                for p in native_by_trace[trace.trace_id]
            )
            for net in pair
        }
        missing = {
            net: tuple(
                trace
                for trace in selected
                if trace.net == net
                and not by_trace[trace.trace_id]
                and not transitions.get(trace.trace_id)
            )
            for net in pair
        }
        assessed = {
            net: tuple(
                p for parts in by_trace.values() for p in parts if p.trace.net == net
            )
            for net in pair
        }
        # Entirely covered disconnected launch stubs are not routed mate paths.
        # Retain native entry geometry only for components that continue to an
        # assessed or locally unverified source span.
        if snapshot.copper_features:
            for net in pair:
                endpoints: dict[Point, list[_Primitive]] = {}
                for primitive in primitives[net]:
                    budget.consume()
                    for point in (primitive.start, primitive.end):
                        endpoints.setdefault(point, []).append(primitive)
                retained_ids = {part.trace.trace_id for part in assessed[net]}
                pending = [
                    part
                    for part in primitives[net]
                    if part.trace.trace_id in retained_ids
                ]
                connected: set[int] = set()
                while pending:
                    part = pending.pop()
                    budget.consume()
                    if id(part) in connected:
                        continue
                    connected.add(id(part))
                    for point in (part.start, part.end):
                        pending.extend(
                            p for p in endpoints[point] if id(p) not in connected
                        )
                primitives[net] = tuple(
                    p for p in primitives[net] if id(p) in connected
                )
        topology = {
            net: _topology_reasons(assessed[net], budget, connectivity=primitives[net])
            for net in pair
        }
        indexes = {
            net: _tree(primitives[net], budget) for net in pair if primitives[net]
        }
    except _BudgetExhausted:
        reason = (
            "Spacing measurement work limit reached; remaining geometry is unverified."
        )
        return SpacingMeasurement(
            pair,
            layer,
            tuple(
                _MeasuredTrace(
                    trace.trace_id,
                    round(
                        sum(
                            math.dist(a, b)
                            for a, b in zip(trace.points, trace.points[1:])
                        )
                    ),
                    False,
                    None,
                    None,
                    reasons=(reason,),
                )
                for trace in selected
            ),
            (reason,),
        )
    measured: list[_MeasuredTrace] = []
    for trace in selected:
        mate_net = pair[1] if trace.net == pair[0] else pair[0]
        all_own = by_trace[trace.trace_id]
        own = tuple(part for part in all_own if not part.unverified_transition)
        unknown_transition = any(part.unverified_transition for part in all_own)
        mates = primitives[mate_net]
        reasons = list(dict.fromkeys((*topology[trace.net], *topology[mate_net])))
        if not own and not transitions.get(trace.trace_id) and not unknown_transition:
            reasons.append(
                "Native arc geometry is unavailable or unstable."
                if trace.is_native_arc
                else "Zero-length trace has no assessable route extent."
            )
        if not mates and own:
            reasons.append("No assessable native mate geometry exists on this layer.")
        if missing[mate_net]:
            reasons.append(
                "Some mate geometry is unavailable; a match or wide-gap proof is incomplete."
            )
        results = []
        try:
            if mates:
                for primitive in own:
                    results.append(
                        _measure_primitive(
                            primitive,
                            mates,
                            indexes[mate_net],
                            budget,
                            primitives[trace.net],
                        )
                    )
        except _BudgetExhausted:
            reasons.append(
                "Spacing measurement work limit reached; remaining geometry is unverified."
            )
        for result in results:
            reasons.extend(result[3])
        minimum = _merge_bounds([result[0] for result in results], minimum=True)
        maximum = _merge_bounds([result[1] for result in results], minimum=False)
        if minimum is not None and missing[mate_net]:
            floor = (
                -(
                    trace.width_nm
                    + max(t.width_nm for t in selected if t.net == mate_net)
                    + 1
                )
                // 2
            )
            minimum = NmBounds(floor, minimum.upper_nm)
            maximum = NmBounds(floor, maximum.upper_nm) if maximum is not None else None
        witnesses = tuple(issue for result in results for issue in result[2])
        if missing[mate_net]:
            witnesses = tuple(
                replace(issue, gap_nm=NmBounds(floor, issue.gap_nm.upper_nm))
                if issue.gap_nm is not None
                else issue
                for issue in witnesses
            )
        known_verified = (
            bool(results) or bool(transitions.get(trace.trace_id))
        ) and not reasons
        if unknown_transition:
            reasons.append(
                "Terminal transition boundary is unverified on local route portions."
            )
        measured.append(
            _MeasuredTrace(
                trace.trace_id,
                max(0, _trace_length(trace) - transitions.get(trace.trace_id, 0)),
                (bool(results) or bool(transitions.get(trace.trace_id)))
                and not reasons,
                minimum,
                maximum,
                witnesses,
                tuple(dict.fromkeys(reasons)),
                transitions.get(trace.trace_id, 0),
                round(sum(part.length for part in own)) if known_verified else 0,
            )
        )
    reasons = () if selected else ("No routed pair geometry exists on this layer.",)
    return SpacingMeasurement(pair, layer, tuple(measured), reasons)


def _compact_points(points: tuple[Point, ...]) -> tuple[Point, ...]:
    """Bound overlay payload size; exact native intervals and boxes remain intact."""
    if len(points) <= 129:
        return points
    step = math.ceil((len(points) - 1) / 128)
    return (*points[:-1:step], points[-1])


def _merge_issues(issues: Sequence[SpacingIssue]) -> tuple[SpacingIssue, ...]:
    """Group physical witnesses with indexed, bounded interval/endpoint unions.

    Original member intervals remain intact. Compact lookup ranges avoid scanning
    every retained leaf for each reciprocal or adjacent witness; exceeding the
    detailed reciprocal budget falls back to constant-work continuity lookups,
    preserving complete continuous concerns without dropping any witnesses.
    """
    ordered = sorted(
        issues,
        key=lambda issue: (
            issue.code,
            issue.location.source_trace_id if issue.location else "",
            issue.location.source_interval if issue.location else (0.0, 0.0),
        ),
    )
    merged: list[SpacingIssue] = []
    members: list[list[SpacingLocation]] = []
    member_sets: list[set[SpacingLocation]] = []
    source_ranges: list[dict[str, list[tuple[float, float]]]] = []
    mate_ranges: list[dict[str, list[tuple[float, float]]]] = []
    paired_ranges: list[
        dict[tuple[str, str], list[tuple[float, float, float, float]]]
    ] = []
    sources: dict[tuple[str, str], set[int]] = {}
    mates: dict[tuple[str, str], set[int]] = {}
    source_ends: dict[tuple[str, Point], set[int]] = {}
    mate_ends: dict[tuple[str, Point], set[int]] = {}
    latest: dict[tuple[str, str], tuple[int, tuple[float, float]]] = {}
    work = 0

    def overlaps(first: tuple[float, float], second: tuple[float, float]) -> bool:
        """Compare closed native intervals with parameter-rounding allowance."""
        return first[0] <= second[1] + 1e-12 and second[0] <= first[1] + 1e-12

    def union(ranges: list[tuple[float, float]], interval: tuple[float, float]) -> None:
        """Maintain a compact closed-interval union for one native trace."""
        nonlocal work
        kept = []
        for previous in ranges:
            work += 1
            if overlaps(previous, interval):
                interval = (
                    min(interval[0], previous[0]),
                    max(interval[1], previous[1]),
                )
            else:
                kept.append(previous)
        ranges[:] = sorted((*kept, interval))

    def nearby(
        index: dict[tuple[str, Point], set[int]], code: str, points: tuple[Point, ...]
    ) -> set[int]:
        """Find endpoint groups within the existing inclusive one-nm allowance."""
        result: set[int] = set()
        for x, y in (points[0], points[-1]):
            for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
                result.update(index.get((code, (x + dx, y + dy)), ()))
        return result

    def retain(index: int, code: str, additions: tuple[SpacingLocation, ...]) -> None:
        """Index each exact member once, and merge only continuous paired ranges."""
        nonlocal work
        for location in additions:
            if location in member_sets[index]:
                continue
            member_sets[index].add(location)
            members[index].append(location)
            key = (code, location.source_trace_id)
            previous = latest.get(key)
            extent = location.source_interval
            if (
                previous is not None
                and previous[0] == index
                and overlaps(previous[1], extent)
            ):
                extent = (
                    min(previous[1][0], extent[0]),
                    max(previous[1][1], extent[1]),
                )
            latest[key] = (index, extent)
            sources.setdefault(key, set()).add(index)
            if work < 100_000:
                union(
                    source_ranges[index].setdefault(location.source_trace_id, []),
                    location.source_interval,
                )
            for identity in location.mate_trace_ids:
                mates.setdefault((code, identity), set()).add(index)
            for point in (location.source_points[0], location.source_points[-1]):
                source_ends.setdefault((code, point), set()).add(index)
            for point in (location.mate_points[0], location.mate_points[-1]):
                mate_ends.setdefault((code, point), set()).add(index)
            if work >= 100_000:
                continue
            for identity, lower, upper in location.mate_intervals:
                union(mate_ranges[index].setdefault(identity, []), (lower, upper))
                key = (location.source_trace_id, identity)
                ranges = paired_ranges[index].setdefault(key, [])
                source_interval, mate_interval = (
                    location.source_interval,
                    (lower, upper),
                )
                kept = []
                for previous in ranges:
                    work += 1
                    if overlaps(previous[:2], source_interval) and overlaps(
                        previous[2:], mate_interval
                    ):
                        source_interval = (
                            min(source_interval[0], previous[0]),
                            max(source_interval[1], previous[1]),
                        )
                        mate_interval = (
                            min(mate_interval[0], previous[2]),
                            max(mate_interval[1], previous[3]),
                        )
                    else:
                        kept.append(previous)
                ranges[:] = sorted((*kept, (*source_interval, *mate_interval)))

    for issue in ordered:
        location = issue.location
        incoming = issue.members or ((location,) if location is not None else ())
        if location is None:
            merged.append(replace(issue, members=incoming))
            members.append(list(incoming))
            member_sets.append(set(incoming))
            source_ranges.append({})
            mate_ranges.append({})
            paired_ranges.append({})
            continue
        adjacent_candidates = nearby(
            source_ends, issue.code, location.source_points
        ).intersection(nearby(mate_ends, issue.code, location.mate_points))
        found = None
        same = adjacent = False
        if work >= 100_000:
            previous = latest.get((issue.code, location.source_trace_id))
            if previous is not None and overlaps(previous[1], location.source_interval):
                found, same = previous[0], True
            elif adjacent_candidates:
                found, adjacent = min(adjacent_candidates), True
            else:
                reciprocal_endpoints = nearby(
                    mate_ends, issue.code, location.source_points
                ).intersection(nearby(source_ends, issue.code, location.mate_points))
                if reciprocal_endpoints:
                    found = min(reciprocal_endpoints)
        else:
            candidates = set(sources.get((issue.code, location.source_trace_id), ()))
            for identity in location.mate_trace_ids:
                candidates.update(
                    sources.get((issue.code, identity), set()).intersection(
                        mates.get((issue.code, location.source_trace_id), ())
                    )
                )
            candidates.update(adjacent_candidates)
            for index in sorted(candidates):
                work += 1
                if work >= 100_000:
                    break
                same = any(
                    overlaps(interval, location.source_interval)
                    for interval in source_ranges[index].get(
                        location.source_trace_id, ()
                    )
                )
                reciprocal = False
                if not same:
                    for right in incoming:
                        for identity, lower, upper in right.mate_intervals:
                            for left in paired_ranges[index].get(
                                (identity, right.source_trace_id), ()
                            ):
                                work += 1
                                if overlaps(left[:2], (lower, upper)) and overlaps(
                                    left[2:], right.source_interval
                                ):
                                    reciprocal = True
                                    break
                            if reciprocal or work >= 100_000:
                                break
                        if reciprocal or work >= 100_000:
                            break
                adjacent = index in adjacent_candidates
                if same or reciprocal or adjacent:
                    found = index
                    break
        if found is None and work >= 100_000:
            # Crossing the detailed-work boundary during this very witness
            # must not split an otherwise continuous native interval.
            previous = latest.get((issue.code, location.source_trace_id))
            if previous is not None and overlaps(previous[1], location.source_interval):
                found, same = previous[0], True
            elif adjacent_candidates:
                found, adjacent = min(adjacent_candidates), True
            else:
                reciprocal_endpoints = nearby(
                    mate_ends, issue.code, location.source_points
                ).intersection(nearby(source_ends, issue.code, location.mate_points))
                if reciprocal_endpoints:
                    found = min(reciprocal_endpoints)
        if found is None:
            found = len(merged)
            merged.append(replace(issue, members=()))
            members.append([])
            member_sets.append(set())
            source_ranges.append({})
            mate_ranges.append({})
            paired_ranges.append({})
            retain(found, issue.code, incoming)
            continue
        previous = merged[found]
        other = previous.location
        assert other is not None
        chosen = previous
        if previous.gap_nm is not None and issue.gap_nm is not None:
            if (
                issue.code == "too_tight"
                and issue.gap_nm.upper_nm < previous.gap_nm.upper_nm
            ) or (
                issue.code == "too_wide"
                and issue.gap_nm.lower_nm > previous.gap_nm.lower_nm
            ):
                chosen = issue
        retain(found, issue.code, incoming)
        combined = replace(
            other,
            mate_trace_ids=tuple(
                sorted(set(other.mate_trace_ids + location.mate_trace_ids))
            ),
            source_interval=(
                min(other.source_interval[0], location.source_interval[0]),
                max(other.source_interval[1], location.source_interval[1]),
            )
            if other.source_trace_id == location.source_trace_id
            else other.source_interval,
            source_points=_compact_points(other.source_points + location.source_points)
            if adjacent or same
            else other.source_points,
            mate_points=_compact_points(other.mate_points + location.mate_points)
            if adjacent or same
            else other.mate_points,
            bounds=(
                min(other.bounds[0], location.bounds[0]),
                min(other.bounds[1], location.bounds[1]),
                max(other.bounds[2], location.bounds[2]),
                max(other.bounds[3], location.bounds[3]),
            ),
            mate_intervals=tuple(
                (identity, start, end)
                for identity, intervals in sorted(mate_ranges[found].items())
                for start, end in intervals
            ),
        )
        merged[found] = replace(
            chosen,
            trace_ids=tuple(sorted(set(previous.trace_ids + issue.trace_ids))),
            location=combined,
            members=(),
        )
    result = []
    for index, issue in enumerate(merged):
        location = issue.location
        if location is not None:
            ranges: dict[str, list[tuple[float, float]]] = {}
            for member in members[index]:
                for identity, lower, upper in member.mate_intervals:
                    ranges.setdefault(identity, []).append((lower, upper))
            compact = []
            for identity, intervals in sorted(ranges.items()):
                current = None
                for interval in sorted(intervals):
                    if current is not None and overlaps(current, interval):
                        current = (current[0], max(current[1], interval[1]))
                    else:
                        if current is not None:
                            compact.append((identity, *current))
                        current = interval
                if current is not None:
                    compact.append((identity, *current))
            location = replace(
                location,
                mate_intervals=tuple(compact),
                cross_sections=tuple(
                    dict.fromkeys(
                        section
                        for member in members[index]
                        for section in member.cross_sections
                    )
                ),
            )
        result.append(replace(issue, location=location, members=tuple(members[index])))
    return tuple(
        sorted(
            result,
            key=lambda issue: (
                issue.location.bounds if issue.location else (0, 0, 0, 0),
                issue.code,
                issue.trace_ids,
            ),
        )
    )


def compare_pair_spacing(
    measurement: SpacingMeasurement,
    declared_spacing_nm: Optional[int],
    *,
    comparison_band_nm: int = SPACING_COMPARISON_BAND_NM,
) -> SpacingCheck:
    """Compare bounded extrema using an inclusive band; preserve incomplete evidence."""
    minimum = _merge_bounds(
        [trace.minimum for trace in measurement.traces if trace.minimum is not None],
        minimum=True,
    )
    maximum = _merge_bounds(
        [trace.maximum for trace in measurement.traces if trace.maximum is not None],
        minimum=False,
    )
    assessed = sum(
        trace.assessed_length_nm
        if trace.assessed_length_nm is not None
        else trace.length_nm
        if trace.verified
        else 0
        for trace in measurement.traces
    )
    unverified = sum(
        trace.length_nm
        - (
            trace.assessed_length_nm
            if trace.assessed_length_nm is not None
            else trace.length_nm
            if trace.verified
            else 0
        )
        for trace in measurement.traces
    )
    transition = sum(trace.transition_length_nm for trace in measurement.traces)
    complete = (
        bool(measurement.traces)
        and all(trace.verified for trace in measurement.traces)
        and not measurement.reasons
    )
    coverage: SpacingCoverage = (
        "complete" if complete else "partial" if minimum is not None else "none"
    )
    reasons = tuple(
        dict.fromkeys(
            (
                *measurement.reasons,
                *(reason for trace in measurement.traces for reason in trace.reasons),
            )
        )
    )
    witnesses = tuple(
        issue for trace in measurement.traces for issue in trace.witnesses
    )
    if transition and assessed == 0 and unverified == 0 and not reasons:
        return SpacingCheck(
            declared_spacing_nm,
            comparison_band_nm,
            "not_applicable",
            "none",
            None,
            None,
            0,
            0,
            (),
            (),
            transition,
        )
    if type(declared_spacing_nm) is not int or declared_spacing_nm <= 0:
        return SpacingCheck(
            None,
            comparison_band_nm,
            "needs_target",
            coverage,
            minimum,
            maximum,
            assessed,
            unverified,
            (),
            reasons,
            transition,
        )
    low, high = (
        declared_spacing_nm - comparison_band_nm,
        declared_spacing_nm + comparison_band_nm,
    )
    issues = []
    for witness in witnesses:
        gap = witness.gap_nm
        if gap is not None and gap.upper_nm < low:
            issues.append(
                replace(
                    witness,
                    code="too_tight",
                    message="Measured spacing is below the declared comparison band.",
                )
            )
        elif gap is not None and gap.lower_nm > high:
            issues.append(
                replace(
                    witness,
                    code="too_wide",
                    message="Measured spacing is above the declared comparison band.",
                )
            )
    definite = (minimum is not None and minimum.upper_nm < low) or (
        maximum is not None and maximum.lower_nm > high
    )
    within = (
        minimum is not None
        and maximum is not None
        and minimum.lower_nm >= low
        and maximum.upper_nm <= high
    )
    verdict: SpacingVerdict = (
        "deviation"
        if definite or issues
        else "match"
        if complete and within
        else "undetermined"
    )
    if complete and not within and not definite and not issues:
        reasons = (*reasons, "Measurement uncertainty crosses the comparison boundary.")
    return SpacingCheck(
        declared_spacing_nm,
        comparison_band_nm,
        verdict,
        coverage,
        minimum,
        maximum,
        assessed,
        unverified,
        _merge_issues(issues),
        reasons,
        transition,
    )


class SpacingContext:
    """Bound target-independent caches to one immutable board snapshot."""

    def __init__(self, snapshot: BoardSnapshot) -> None:
        self.snapshot = snapshot
        self._measurements: dict[tuple[tuple[str, str], str], SpacingMeasurement] = {}
        self._checks: dict[tuple[object, ...], SpacingCheck] = {}

    def measurement(self, pair: tuple[str, str], layer: str) -> SpacingMeasurement:
        """Measure each pair/layer once, reusing all source widths."""
        key = (tuple(sorted(pair)), layer)
        if key not in self._measurements:
            self._measurements[key] = measure_pair_spacing(self.snapshot, key[0], layer)
        return self._measurements[key]

    def check(
        self,
        pair: tuple[str, str],
        layer: str,
        declared_spacing_nm: Optional[int],
        *,
        source_ids: Optional[frozenset[str]] = None,
        comparison_band_nm: int = SPACING_COMPARISON_BAND_NM,
    ) -> SpacingCheck:
        """Reuse exact target/row comparisons within this immutable snapshot.

        A row containing every source is the same check as its diagnostic pair.
        Keep filtered width rows independent, and bound interactive target edits.
        """
        measurement = self.measurement(pair, layer)
        selected = tuple(
            trace
            for trace in measurement.traces
            if source_ids is None or trace.trace_id in source_ids
        )
        ids = (
            None
            if len(selected) == len(measurement.traces)
            else tuple(trace.trace_id for trace in selected)
        )
        key = (
            tuple(sorted(pair)),
            layer,
            type(declared_spacing_nm).__name__,
            declared_spacing_nm,
            ids,
            comparison_band_nm,
            SPACING_CHECK_POLICY_REVISION,
        )
        if key not in self._checks:
            if len(self._checks) >= 512:
                self._checks.pop(next(iter(self._checks)))
            projected = (
                measurement if ids is None else replace(measurement, traces=selected)
            )
            self._checks[key] = compare_pair_spacing(
                projected, declared_spacing_nm, comparison_band_nm=comparison_band_nm
            )
        return self._checks[key]


def spacing_checks_for_sections(
    config: Config,
    snapshot: BoardSnapshot,
    sections: Sequence[Section],
    *,
    context: Optional[SpacingContext] = None,
) -> dict[str, SpacingCheck]:
    """Project full-pair measurements onto eligible rows, including draft intent."""
    if context is None or context.snapshot is not snapshot:
        context = SpacingContext(snapshot)
    specs = {spec.spec_id: spec for spec in config.specifications}
    result = {}
    for section in sections:
        spec = specs.get(section.spec_id)
        if (
            spec is None
            or not spec.kind.startswith("differential")
            or section.exclusion_reason
        ):
            continue
        pairs = tuple(
            tuple(sorted(pair))
            for pair in snapshot.differential_pairs
            if set(section.net_names).intersection(pair)
        )
        pair = pairs[0] if len(pairs) == 1 else tuple(section.net_names[:2])
        target = next(
            (
                settings.spacing_nm
                for settings in spec.layer_settings
                if settings.layer == section.layer
            ),
            None,
        )
        result[section.section_id] = context.check(
            pair,
            section.layer,
            target,
            source_ids=frozenset(trace.trace_id for trace in section.traces),
        )
    return result


def spacing_input_digest(
    snapshot: BoardSnapshot, spec: Specification, layer: str
) -> str:
    """Hash raw complete native inputs cheaply, without performing measurement."""
    members = {
        net
        for net, classes in snapshot.net_class_memberships
        if spec.net_class in classes
    }
    pairs = sorted(
        tuple(sorted(pair))
        for pair in snapshot.differential_pairs
        if members.intersection(pair)
    )
    nets = {net for pair in pairs for net in pair}
    records = sorted(
        (
            trace.trace_id,
            trace.layer,
            trace.net,
            trace.width_nm,
            min(trace.points, tuple(reversed(trace.points))),
            trace.is_native_arc,
            trace.native_arc_midpoint,
            trace.native_arc_center,
            trace.native_arc_radius_nm,
        )
        for trace in snapshot.traces
        if trace.layer == layer and trace.net in nets
    )
    payload = (
        SPACING_CHECK_POLICY_REVISION,
        spec.net_class,
        layer,
        pairs,
        records,
        sorted(
            (
                feature.feature_id,
                feature.net,
                feature.layer,
                feature.kind,
                feature.shape,
                feature.center,
                feature.size_x_nm,
                feature.size_y_nm,
                feature.rotation_deg,
                feature.corner_radius_nm,
                feature.polygons,
                feature.footprint_id,
                feature.pad_number,
            )
            for feature in snapshot.copper_features
            if feature.net in nets and feature.layer == layer
        ),
        snapshot.net_class_context_digest,
        snapshot.net_class_error,
        snapshot.differential_pair_error,
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def spacing_check_label(check: SpacingCheck) -> str:
    """Return a compact accessible status for workbook and editor labels."""
    labels = {
        "match": "Match",
        "deviation": "Deviation",
        "undetermined": "Unverified",
        "needs_target": "Needs target",
        "not_applicable": "Terminal transitions",
    }
    text = labels[check.verdict]
    return text + " · partial" if check.coverage == "partial" else text


def _format_nm(value: int, *, signed: bool = False) -> str:
    """Format dimensions, including overlap, zero length and signed deviations."""
    with localcontext() as context:
        context.prec = max(40, len(str(abs(value))) + 12)
        result = format(Decimal(value) / Decimal(1_000_000), "f")
    result = result.rstrip("0").rstrip(".") if "." in result else result
    return "+" + result if signed and value > 0 else result


def _format_bounds(bounds: NmBounds, *, signed: bool = False) -> str:
    """Format exact or bounded dimensional values in millimetres."""
    if bounds.lower_nm == bounds.upper_nm:
        return _format_nm(bounds.lower_nm, signed=signed) + " mm"
    return f"{_format_nm(bounds.lower_nm, signed=signed)}–{_format_nm(bounds.upper_nm, signed=signed)} mm"


def spacing_check_details(check: SpacingCheck) -> str:
    """Return shared plain-text details with bounded locations and limitations."""
    lines = ["Pair spacing: " + spacing_check_label(check)]
    if check.declared_spacing_nm is not None:
        lines.append(f"Declared: {_format_nm(check.declared_spacing_nm)} mm")
    qualifier = " (assessed portions)" if check.coverage != "complete" else ""
    if check.minimum_gap_nm is not None:
        lines.append(f"Minimum{qualifier}: {_format_bounds(check.minimum_gap_nm)}")
    if check.maximum_gap_nm is not None:
        lines.append(f"Maximum{qualifier}: {_format_bounds(check.maximum_gap_nm)}")
    if check.declared_spacing_nm is not None:
        for label, value in (
            ("Minimum deviation", check.minimum_gap_nm),
            ("Maximum deviation", check.maximum_gap_nm),
        ):
            if value is not None:
                lines.append(
                    f"{label}: {_format_bounds(NmBounds(value.lower_nm - check.declared_spacing_nm, value.upper_nm - check.declared_spacing_nm), signed=True)}"
                )
    lines.extend(
        (
            f"Coverage: {check.coverage.capitalize()}",
            f"Assessed length: {_format_nm(check.assessed_length_nm)} mm; unverified: {_format_nm(check.unverified_length_nm)} mm",
            f"Comparison band: ±{_format_nm(check.comparison_band_nm)} mm (dimensional threshold)",
        )
    )
    if check.transition_length_nm:
        lines.append(
            f"Terminal transition length: {_format_nm(check.transition_length_nm)} mm (excluded)"
        )
    for number, issue in enumerate(check.issues, 1):
        label = "Too tight" if issue.code == "too_tight" else "Too wide"
        value = (
            _format_bounds(issue.gap_nm) if issue.gap_nm is not None else "Unverified"
        )
        lines.append(f"{number} · {label} · {value}")
    lines.extend(check.reasons)
    return "\n".join(lines)


def spacing_check_summary(check: SpacingCheck) -> str:
    """Describe actionable nontransition findings without raw board coordinates."""
    if check.issues:
        count = len(check.issues)
        return (
            f"{count} routed spacing issue"
            + ("s" if count != 1 else "")
            + (" · partial coverage" if check.coverage != "complete" else "")
        )
    if check.verdict == "not_applicable":
        return "Terminal transitions only"
    if check.verdict == "match":
        value = (
            ""
            if check.minimum_gap_nm is None
            else " · " + _format_bounds(check.minimum_gap_nm)
        )
        return "Routed spacing matches" + value
    return spacing_check_label(check)
