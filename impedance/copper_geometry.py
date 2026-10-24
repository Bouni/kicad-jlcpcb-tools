"""Prove complete copper fits within immutable native pad contours."""

from .model import Contour, CopperPolygon, Point
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
