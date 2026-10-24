"""Prove complete copper fits within immutable native pad contours."""

from collections.abc import Callable
from typing import Optional

from .model import Contour, CopperFeature, CopperPolygon, Point
from .regions import _segment_distance


def _inside(point: Point, contour: Contour) -> bool:
    """Use the odd/even rule; positive capsule clearance handles boundary points."""
    x, y = point
    inside = False
    for start, end in zip(contour, contour[1:] + contour[:1]):
        if (start[1] > y) != (end[1] > y):
            intersection = start[0] + (y - start[1]) * (end[0] - start[0]) / (
                end[1] - start[1]
            )
            if x < intersection:
                inside = not inside
    return inside


def capsule_inside_polygons(
    start: Point, end: Point, radius_nm: int, polygons: tuple[CopperPolygon, ...]
) -> bool:
    """Require a capsule's center and full clearance to fit one native polygon.

    Endpoint-only containment is insufficient for concave pads and holes. The
    minimum distance of the whole centerline to every edge proves that no part
    of its copper crosses any boundary. Independent islands cannot bridge gaps.
    """
    for polygon in polygons:
        outer, *holes = polygon
        if not _inside(start, outer) or any(_inside(start, hole) for hole in holes):
            continue
        if all(
            _segment_distance(start, end, left, right) + 1e-6 >= radius_nm
            for contour in polygon
            for left, right in zip(contour, contour[1:] + contour[:1])
        ):
            return True
    return False


def polygon_inside_polygons(
    copper: CopperPolygon, polygons: tuple[CopperPolygon, ...]
) -> bool:
    """Prove an outward native stroke lies inside one inward pad polygon.

    Check complete edges against every pad boundary, not only vertices. A pad
    hole enclosed by the stroke must lie wholly within one of the stroke's
    own holes. Boundary contact is conservatively retained for complex pads;
    native curved approximations cannot prove exact tangency.
    """
    outer, *holes = copper
    for polygon in polygons:
        pad_outer, *pad_holes = polygon
        if not all(
            _inside(point, pad_outer)
            and not any(_inside(point, hole) for hole in pad_holes)
            for point in outer
        ):
            continue
        if any(
            _segment_distance(a, b, c, d) <= 1e-6
            for contour in (outer, *holes)
            for a, b in zip(contour, contour[1:] + contour[:1])
            for pad_contour in polygon
            for c, d in zip(pad_contour, pad_contour[1:] + pad_contour[:1])
        ):
            continue
        if any(
            _inside(hole[0], outer)
            and not any(_inside(hole[0], void) for void in holes)
            for hole in pad_holes
        ):
            continue
        return True
    return False


def feature_point_distance(
    point: tuple[float, float],
    feature: CopperFeature,
    consume: Optional[Callable[[], None]] = None,
) -> float:
    """Return signed distance to actual pad/via copper, including native holes.

    Negative values are inside copper. Rotation follows KiCad's board-coordinate
    convention. A polygon's voids never count as copper contact.
    """
    import math

    if feature.shape == "polygon":
        distances = []
        for polygon in feature.polygons:
            if consume is not None:
                for contour in polygon:
                    for _ in contour:
                        consume()
            outer, *holes = polygon
            inside = _inside(point, outer) and not any(
                _inside(point, hole) for hole in holes
            )
            boundary = min(
                _segment_distance(point, point, left, right)
                for contour in polygon
                for left, right in zip(contour, contour[1:] + contour[:1])
            )
            distances.append(-boundary if inside else boundary)
        return min(distances, default=float("inf"))
    angle = math.radians(float(feature.rotation_deg))
    dx, dy = point[0] - feature.center[0], point[1] - feature.center[1]
    x, y = (
        dx * math.cos(angle) - dy * math.sin(angle),
        dx * math.sin(angle) + dy * math.cos(angle),
    )
    half_x, half_y = feature.size_x_nm / 2, feature.size_y_nm / 2
    if feature.shape == "circle":
        return math.hypot(x, y) - half_x
    corner = (
        0.0
        if feature.shape == "rect"
        else float(feature.corner_radius_nm)
        if feature.shape == "roundrect"
        else min(half_x, half_y)
    )
    qx, qy = abs(x) - half_x + corner, abs(y) - half_y + corner
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - corner


def feature_bounds(
    feature: CopperFeature, consume: Optional[Callable[[], None]] = None
) -> tuple[float, float, float, float]:
    """Enclose actual copper in board coordinates, honoring offsets and rotation."""
    import math

    if feature.shape == "polygon":
        points = []
        for polygon in feature.polygons:
            for point in polygon[0]:
                if consume is not None:
                    consume()
                points.append(point)
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )
    angle = math.radians(float(feature.rotation_deg))
    x = (
        abs(math.cos(angle)) * feature.size_x_nm
        + abs(math.sin(angle)) * feature.size_y_nm
    ) / 2
    y = (
        abs(math.sin(angle)) * feature.size_x_nm
        + abs(math.cos(angle)) * feature.size_y_nm
    ) / 2
    return (
        feature.center[0] - x,
        feature.center[1] - y,
        feature.center[0] + x,
        feature.center[1] + y,
    )


def feature_convex_disks(
    feature: CopperFeature, consume: Callable[[], None]
) -> tuple[tuple[float, float, float], ...]:
    """Represent the actual flash hull as the convex hull of exact circular disks.

    Native polygon outer contours are already in board coordinates. Analytic
    flashes are rotated rounded rectangles, represented exactly by their core
    corners plus the native corner radius; circles need one disk.
    """
    import math

    if feature.shape == "polygon":
        points = []
        for polygon in feature.polygons:
            for point in polygon[0]:
                consume()
                points.append((float(point[0]), float(point[1]), 0.0))
        return tuple(points)
    half_x, half_y = feature.size_x_nm / 2, feature.size_y_nm / 2
    radius = (
        half_x
        if feature.shape == "circle"
        else 0.0
        if feature.shape == "rect"
        else float(feature.corner_radius_nm)
        if feature.shape == "roundrect"
        else min(half_x, half_y)
    )
    angle = math.radians(float(feature.rotation_deg))
    result = []
    for x in {-half_x + radius, half_x - radius}:
        for y in {-half_y + radius, half_y - radius}:
            consume()
            result.append(
                (
                    feature.center[0] + x * math.cos(angle) + y * math.sin(angle),
                    feature.center[1] - x * math.sin(angle) + y * math.cos(angle),
                    radius,
                )
            )
    return tuple(result)
