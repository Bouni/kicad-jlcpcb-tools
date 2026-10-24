"""Exact local landing-copper regions used by differential-route clipping."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
from typing import Optional, Protocol

from .copper_geometry import feature_convex_disks, feature_point_distance
from .model import CopperFeature

EXIT_ALLOWANCE_PITCHES = 1.0
_FPoint = tuple[float, float]
_Disk = tuple[float, float, float]


class ContactCurve(Protocol):
    """Small native-curve contract shared without a spacing-engine import."""

    @property
    def length(self) -> float:
        """Return physical centerline extent."""
        ...

    def point(self, t: float) -> _FPoint:
        """Evaluate an exact native centerline."""
        ...


def first_exit(
    curve: ContactCurve,
    distance: Callable[[_FPoint], float],
    consume: Callable[[], None],
) -> Optional[float]:
    """Find the first signed-distance exit with a conservative sub-nm boundary.

    Signed distance to a closed region is 1-Lipschitz. A midpoint bound proves
    whole intervals inside; left-first traversal cannot skip a hole
    or later reentry. The caller shares a deterministic work budget.
    """
    consume()
    if distance(curve.point(0)) > 0:
        return 0.0

    pending = [(0.0, 1.0)]
    while pending:
        start, end = pending.pop()
        consume()
        middle = (start + end) / 2
        reach = curve.length * (end - start) / 2
        value = distance(curve.point(middle))
        if value + reach <= 0:
            continue
        if value - reach > 0 or reach <= 0.25:
            return start
        pending.append((middle, end))
        pending.append((start, middle))

    return None


def contact_exit(
    curve: ContactCurve,
    feature: CopperFeature,
    radius_nm: float,
    consume: Callable[[], None],
) -> Optional[float]:
    """Find the first native copper-contact exit, including polygon holes."""
    return first_exit(
        curve,
        lambda point: feature_point_distance(point, feature, consume) - radius_nm,
        consume,
    )


def contact_seed(
    curve: ContactCurve,
    distance: Callable[[_FPoint], float],
    consume: Callable[[], None],
) -> Optional[float]:
    """Find a proven interior contact without assuming a pad-center projection.

    Signed distance is 1-Lipschitz. Entirely separate intervals are pruned;
    sub-nm unresolved tangencies remain unexempt rather than inventing contact.
    """
    pending = [(0.0, 1.0)]
    while pending:
        start, end = pending.pop()
        consume()
        middle = (start + end) / 2
        value = distance(curve.point(middle))
        if value <= 0:
            return middle
        reach = curve.length * (end - start) / 2
        if value - reach > 0 or reach <= 0.25:
            continue
        pending.append((middle, end))
        pending.append((start, middle))
    return None


def _hull(
    points: Sequence[_FPoint], consume: Callable[[], None]
) -> tuple[_FPoint, ...]:
    """Discard redundant disk centers of one common radius before hull support."""
    ordered = sorted(set(points))
    if len(ordered) <= 2:
        return tuple(ordered)

    def cross(a: _FPoint, b: _FPoint, c: _FPoint) -> float:
        """Return the oriented twice-area."""
        consume()
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    lower: list[_FPoint] = []
    upper: list[_FPoint] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _support(
    disks: tuple[_Disk, ...], nx: float, ny: float, consume: Callable[[], None]
) -> float:
    """Account for every disk examined by one exact hull support query."""
    best = float("-inf")
    for x, y, radius in disks:
        consume()
        best = max(best, x * nx + y * ny + radius)
    return best


@dataclass(frozen=True)
class LandingRegion:
    """Exact convex hull of flashed copper swept by the software entry allowance."""

    disks: tuple[_Disk, ...]
    supports: tuple[tuple[float, float, float], ...]

    def distance(self, point: _FPoint, consume: Callable[[], None]) -> float:
        """Return signed convex-region distance without polygonizing native arcs."""
        best = float("-inf")
        for nx, ny, support in self.supports:
            consume()
            best = max(best, point[0] * nx + point[1] * ny - support)
        if self.supports and all(radius == 0 for _, _, radius in self.disks):
            # A native polygon hull has only flat faces. Their maximum signed
            # clearance has the exact containment sign and is 1-Lipschitz,
            # sufficient for the same conservative first-exit proof.
            return best
        # On a circular hull face, the extremal separating normal is radial.
        # All other extrema occur at the precomputed common-tangent normals.
        for x, y, _ in self.disks:
            consume()
            dx, dy = point[0] - x, point[1] - y
            extent = math.hypot(dx, dy)
            if extent:
                nx, ny = dx / extent, dy / extent
                support = _support(self.disks, nx, ny, consume)
                best = max(best, point[0] * nx + point[1] * ny - support)
        return best


def landing_region(
    features: tuple[CopperFeature, ...],
    direction: _FPoint,
    consume: Callable[[], None],
    *,
    radial_pitch_nm: float = 0.0,
) -> LandingRegion:
    """Sweep actual landing copper by one pitch in the established exit direction.

    A duplicated-pin bank has several exits before one routed pair exists. Its
    caller supplies the actual adjacent opposite-net pitch as a radial bound.
    """
    pitch = math.dist(features[0].center, features[1].center)
    shift = tuple(value * pitch * EXIT_ALLOWANCE_PITCHES for value in direction)
    groups: dict[float, list[_FPoint]] = {}
    for feature in features:
        for x, y, radius in feature_convex_disks(feature, consume):
            groups.setdefault(radius + radial_pitch_nm, []).extend(
                ((x, y), (x + shift[0], y + shift[1]))
            )
    disks = tuple(
        (x, y, radius)
        for radius, points in sorted(groups.items())
        for x, y in _hull(points, consume)
    )
    supports = []
    for index, (x, y, radius) in enumerate(disks):
        for other_x, other_y, other_radius in disks[index + 1 :]:
            consume()
            dx, dy = x - other_x, y - other_y
            extent = math.hypot(dx, dy)
            ratio = (other_radius - radius) / extent if extent else 2.0
            if abs(ratio) > 1:
                continue
            angle = math.atan2(dy, dx)
            offset = math.acos(max(-1.0, min(1.0, ratio)))
            for normal_angle in (angle - offset, angle + offset):
                nx, ny = math.cos(normal_angle), math.sin(normal_angle)
                support = _support(disks, nx, ny, consume)
                tied = x * nx + y * ny + radius
                if abs(support - tied) <= max(
                    1e-6, 8 * math.ulp(max(abs(support), abs(tied)))
                ):
                    supports.append((nx, ny, support))
    return LandingRegion(disks, tuple(supports))
