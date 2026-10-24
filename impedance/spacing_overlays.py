"""Project confirmed spacing regions onto an immutable PCB capture."""

from dataclasses import dataclass, replace
import math
from typing import TYPE_CHECKING, Optional

from .spacing_checks import (
    SpacingCrossSection,
    SpacingIssue,
    _format_bounds,
    _format_nm,
    _native_parameter,
    _Primitive,
    _primitives,
    _slice_primitive,
)

if TYPE_CHECKING:
    from .spacing_review import SpacingReviewEntry

ViewportNm = tuple[float, float, float, float]
PhysicalPoint = tuple[float, float]


@dataclass(frozen=True)
class SpacingGuide:
    """An exact diagnostic path in board nanometres, independent of rasterization."""

    kind: str
    start: PhysicalPoint
    end: PhysicalPoint
    center: Optional[PhysicalPoint] = None
    sweep: float = 0.0
    section: Optional[SpacingCrossSection] = None
    facing_edge: Optional["SpacingGuide"] = None


def _gap_guide(
    section: SpacingCrossSection, target: Optional[int] = None
) -> Optional[SpacingGuide]:
    """Place a ruler from the true facing edge using the selected measurement."""
    source, mate = section.source_point, section.mate_point
    distance = math.dist(source, mate)
    if not distance or not math.isfinite(distance):
        return None
    normal = tuple((b - a) / distance for a, b in zip(source, mate))
    start = tuple(a + n * section.source_width_nm / 2 for a, n in zip(source, normal))
    end = (
        tuple(a + n * target for a, n in zip(start, normal))
        if target is not None
        else tuple(b - n * section.mate_width_nm / 2 for b, n in zip(mate, normal))
    )
    return SpacingGuide(
        "target-gap" if target is not None else "measured-gap",
        start,
        end,
        section=section,
    )


def _guide_curve(
    primitive: _Primitive, offset: float, normal: PhysicalPoint
) -> SpacingGuide:
    """Offset a certified straight/radial sidewall without extending its span."""
    if primitive.center is None:
        return SpacingGuide(
            "target-edge",
            tuple(a + offset * n for a, n in zip(primitive.start, normal)),
            tuple(a + offset * n for a, n in zip(primitive.end, normal)),
        )
    center = tuple(float(value) for value in primitive.center)
    radius = primitive.radius + offset
    return SpacingGuide(
        "target-edge",
        tuple(
            c + radius * (a - c) / primitive.radius
            for a, c in zip(primitive.start, center)
        ),
        tuple(
            c + radius * (a - c) / primitive.radius
            for a, c in zip(primitive.end, center)
        ),
        center,
        primitive.sweep,
    )


def _certified_guides(
    source: _Primitive,
    mate: _Primitive,
    sections: tuple[SpacingCrossSection, ...],
    target: int,
    viewport: ViewportNm,
) -> tuple[SpacingGuide, ...]:
    """Draw continuous offsets only for witnessed parallel or concentric pairs."""
    from .spacing_review import _visible_intervals

    sections = tuple(
        section
        for section in sections
        if source.distance(section.source_point) <= 2
        and mate.distance(section.mate_point) <= 2
    )
    if len(sections) < 2:
        return ()
    parameters = tuple(
        _native_parameter(source, section.source_point) for section in sections
    )
    mate_parameters = tuple(
        _native_parameter(mate, section.mate_point) for section in sections
    )
    lower, upper = min(parameters), max(parameters)
    if upper - lower < 1e-12:
        return ()
    source = _slice_primitive(source, lower, upper)
    mate = _slice_primitive(mate, min(mate_parameters), max(mate_parameters))
    first = sections[0]
    separation = math.dist(first.source_point, first.mate_point)
    if not separation:
        return ()
    normal = tuple(
        (b - a) / separation for a, b in zip(first.source_point, first.mate_point)
    )
    if source.center is None and mate.center is None:
        dx, dy = source.end[0] - source.start[0], source.end[1] - source.start[1]
        mx, my = mate.end[0] - mate.start[0], mate.end[1] - mate.start[1]
        if (
            not mate.length
            or abs(dx * my - dy * mx) > source.length * mate.length * 1e-9
        ):
            return ()
        if any(
            abs(
                dx * (section.mate_point[0] - section.source_point[0])
                + dy * (section.mate_point[1] - section.source_point[1])
            )
            > source.length * 2
            for section in sections
        ):
            return ()
        guides = [
            _guide_curve(source, first.source_width_nm / 2 + target, normal),
            _guide_curve(
                mate, first.mate_width_nm / 2 + target, tuple(-n for n in normal)
            ),
        ]
    elif source.center is not None and source.center == mate.center:
        center = tuple(float(value) for value in source.center)
        # Equal centers alone do not establish radial pairing: a finite mate
        # arc may have supplied an endpoint outside its angular overlap.
        if any(
            math.dist(
                section.mate_point,
                tuple(
                    c + (a - c) * mate.radius / source.radius
                    for a, c in zip(section.source_point, center)
                ),
            )
            > 2
            for section in sections
        ):
            return ()
        # Even radial samples can straddle an unsampled angular gap. The two
        # certified finite sweeps must cover the same complete angular span.
        tolerance = 2 / min(source.radius, mate.radius)
        source_start = source.angle + min(0.0, source.sweep)
        mate_start = mate.angle + min(0.0, mate.sweep)
        offset = (source_start - mate_start + math.pi) % math.tau - math.pi
        if (
            abs(offset) > tolerance
            or abs(abs(source.sweep) - abs(mate.sweep)) > tolerance
        ):
            return ()
        sign = 1 if mate.radius > source.radius else -1
        if (
            source.radius + sign * (first.source_width_nm / 2 + target) <= 0
            or mate.radius - sign * (first.mate_width_nm / 2 + target) <= 0
        ):
            return ()
        guides = [
            _guide_curve(source, sign * (first.source_width_nm / 2 + target), normal),
            _guide_curve(mate, -sign * (first.mate_width_nm / 2 + target), normal),
        ]
    else:
        return ()
    # One canonical source supplies the required-clearance envelope. A filled
    # band is permitted only after the same parallel/radial certification used
    # for the exact target edges above, never over arbitrary bends.
    if source.trace.trace_id < mate.trace.trace_id:
        sign = 1 if source.center is None or mate.radius > source.radius else -1
        guides[0] = replace(
            guides[0],
            facing_edge=_guide_curve(source, sign * first.source_width_nm / 2, normal),
        )
    # The same analytic correspondence remains valid inside a clipped tile.
    # Place one measured tick there rather than requiring an endpoint sample to
    # fall inside every close-up of a long straight or circular route.
    for start, end in _visible_intervals(source, viewport):
        if end - start < 1e-12:
            continue
        point = source.point((start + end) / 2)
        if source.center is None:
            other = tuple(a + n * separation for a, n in zip(point, normal))
        else:
            center = tuple(float(value) for value in source.center)
            other = tuple(
                c + (a - c) * mate.radius / source.radius for a, c in zip(point, center)
            )
        ruler = _gap_guide(replace(first, source_point=point, mate_point=other))
        if ruler is not None:
            guides.append(ruler)
    return tuple(guides)


def spacing_guide_paths(entry: "SpacingReviewEntry") -> tuple[SpacingGuide, ...]:
    """Use exact measured correspondences; never guess a mate from the full route."""
    target = entry.target_spacing_nm
    if target is None or target <= 0:
        return ()
    primitives = {trace.trace_id: _primitives(trace) for trace in entry.traces}
    guides = []
    for member in entry.members:
        pairs = {}
        for section in member.cross_sections:
            pairs.setdefault(
                (section.source_trace_id, section.mate_trace_id), []
            ).append(section)
        for (source_id, mate_id), sections in pairs.items():
            certified = tuple(
                guide
                for source in primitives.get(source_id, ())
                for mate in primitives.get(mate_id, ())
                for guide in _certified_guides(
                    source, mate, tuple(sections), target, entry.viewport_nm
                )
            )
            if certified:
                guides.extend(certified)
            else:
                # Unproven varying geometry gets a local target ruler at the
                # actual measured cross-section, never a speculative full path.
                for section in sections:
                    for desired in (target, None):
                        guide = _gap_guide(section, desired)
                        if guide is not None:
                            guides.append(guide)
    unique = {}
    for guide in guides:
        endpoints = tuple(
            sorted(
                tuple(round(value) for value in point)
                for point in (guide.start, guide.end)
            )
        )
        midpoint = None
        if guide.center is not None:
            radius = math.dist(guide.start, guide.center)
            angle = (
                math.atan2(
                    guide.start[1] - guide.center[1], guide.start[0] - guide.center[0]
                )
                + guide.sweep / 2
            )
            midpoint = (
                round(guide.center[0] + radius * math.cos(angle)),
                round(guide.center[1] + radius * math.sin(angle)),
            )
        # Opposite semicircles share endpoints, center and absolute sweep. The
        # physical midpoint separates them while remaining reversal invariant.
        key = (
            guide.kind,
            endpoints,
            guide.center,
            round(abs(guide.sweep), 10),
            midpoint,
        )
        previous = unique.get(key)
        if (
            previous is not None
            and previous.facing_edge is not None
            and guide.facing_edge is None
        ):
            guide = replace(guide, facing_edge=previous.facing_edge)
        unique[key] = guide
    left, top, width, height = entry.viewport_nm
    result, ticks = [], []
    for guide in unique.values():
        if guide.kind == "target-edge":
            result.append(guide)
        elif all(
            left <= point[0] <= left + width and top <= point[1] <= top + height
            for point in (guide.start, guide.end)
        ):
            ticks.append(guide)
    # Keep at most three separated measured rulers in one image. Target edges
    # stay continuous; local fallback target rulers stay with their witness.
    if entry.representative is not None:
        # The concern selector has chosen the authoritative worst cross-section.
        # Certified straight/arc guides can also generate a convenient central
        # tick, but that must not replace the explicit measured and target pair.
        for target_gap in (None, target):
            guide = _gap_guide(entry.representative, target_gap)
            if guide is not None:
                result.append(guide)
        return tuple(result)
    measured = [guide for guide in ticks if guide.kind == "measured-gap"]
    measured.sort(key=lambda guide: (guide.start, guide.end))
    if len(measured) > 3:
        measured = [measured[0], measured[len(measured) // 2], measured[-1]]
    result.extend(measured)
    result.extend(
        guide
        for guide in ticks
        if guide.kind == "target-gap"
        and any(math.dist(guide.start, tick.start) < 1 for tick in measured)
    )
    return tuple(result)


def spacing_issue_label(issue: SpacingIssue, declared_spacing_nm: Optional[int]) -> str:
    """Describe an actionable region without requiring board coordinates."""
    label = {"too_tight": "Too tight", "too_wide": "Too wide"}.get(
        issue.code, "Spacing issue"
    )
    if issue.gap_nm is not None:
        label += " · " + _format_bounds(issue.gap_nm)
    if declared_spacing_nm is not None:
        label += "; target " + _format_nm(declared_spacing_nm) + " mm"
    return label


def valid_viewport(viewport_nm: object) -> bool:
    """Require a real physical capture rectangle before placing annotations."""
    return (
        isinstance(viewport_nm, tuple)
        and len(viewport_nm) == 4
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in viewport_nm
        )
        and viewport_nm[2] > 0
        and viewport_nm[3] > 0
    )
