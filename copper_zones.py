"""Copy actionable locations for copper zones that contain no filled copper."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

Point = tuple[int, int]
Contour = tuple[Point, ...]
Bounds = tuple[int, int, int, int]


@dataclass(frozen=True)
class EmptyCopperZone:
    """Retain one empty zone layer without owning any native board objects."""

    zone_id: str
    net_name: str
    layer_id: int
    layer_name: str
    contours: tuple[Contour, ...]
    geometry_error: str = ""
    position: Optional[Point] = None  # noqa: UP045

    @property
    def label(self) -> str:
        """Keep the established net/layer wording for logs and export findings."""
        return f"{self.net_name or 'no net'} on {self.layer_name}"

    @property
    def bounds(self) -> Optional[Bounds]:  # noqa: UP045
        """Return board-coordinate extents of all intended boundaries."""
        points = [point for contour in self.contours for point in contour]
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def location_text(self) -> str:
        """Show absolute board XY in millimetres, including unavailable geometry."""
        # KiCad's PCB internal units are nanometres, independent of editor units.
        bounds = self.bounds
        if bounds is not None:
            left, top, right, bottom = (coordinate / 1_000_000 for coordinate in bounds)
            return (
                f"Board X {left:.3f} to {right:.3f} mm, Y {top:.3f} to {bottom:.3f} mm"
            )
        if self.position is not None:
            x, y = (coordinate / 1_000_000 for coordinate in self.position)
            return f"Board X {x:.3f} mm, Y {y:.3f} mm"
        return "Board location unavailable"


def _copy_point(point: Any) -> Point:
    """Reject unavailable coordinates instead of fabricating a fallback origin."""
    x, y = point.x, point.y
    if not isinstance(x, int) or not isinstance(y, int):
        raise TypeError("Zone outline coordinates are unavailable")
    return int(x), int(y)


def _copy_contour(chain: Any) -> Contour:
    """Copy a native line chain while checking it describes a usable boundary."""
    contour = tuple(
        _copy_point(chain.CPoint(index)) for index in range(chain.PointCount())
    )
    if len(set(contour)) < 3:
        raise ValueError("Zone outline has fewer than three distinct boundary points")
    return contour


def _copy_outline(zone: Any) -> tuple[Contour, ...]:
    """Copy every outer contour and hole; do not rely on the absent filled shape."""
    outline = zone.Outline()
    contours = []
    for index in range(outline.OutlineCount()):
        contours.append(_copy_contour(outline.COutline(index)))
        for hole in range(outline.HoleCount(index)):
            contours.append(_copy_contour(outline.CHole(index, hole)))
    if not contours:
        raise ValueError("Zone outline has no boundary points")
    return tuple(contours)


def _zone_identity(zone: Any, index: int) -> str:
    """Retain native identity, or distinguish pours in this transient board snapshot."""
    try:
        identity = str(zone.m_Uuid.AsString())
        if identity:
            return identity
    except Exception:
        pass
    return f"zone-{index + 1}"


def _zone_position(zone: Any) -> Optional[Point]:  # noqa: UP045
    """Capture actual native XY when an outline cannot be shown."""
    try:
        outline = zone.Outline()
        # ZONE::GetPosition returns a synthetic (0, 0) for absent vertices.
        if outline.OutlineCount() == 0 or outline.TotalVertices() == 0:
            return None
    except Exception:
        # A failing outline binding may still expose its actual first corner.
        pass
    try:
        return _copy_point(zone.GetPosition())
    except Exception:
        return None


def collect_empty_copper_zones(
    board: Any, is_copper_layer: Callable[[int], bool]
) -> list[EmptyCopperZone]:
    """Inspect current fills and copy intended outlines without mutating the board."""
    regions = []
    for index, zone in enumerate(board.Zones()):
        # Rule areas are keepouts and technical-layer zones are graphics.
        if zone.GetIsRuleArea():
            continue
        empty_layers = [
            layer
            for layer in zone.GetLayerSet().Seq()
            if is_copper_layer(layer) and zone.GetFilledPolysList(layer).Area() <= 0
        ]
        if not empty_layers:
            continue
        geometry_error = ""
        try:
            contours = _copy_outline(zone)
        except Exception as error:
            # Location failure must never hide the underlying missing copper.
            contours = ()
            geometry_error = f"Zone outline unavailable: {error}"
        position = _zone_position(zone) if not contours else None
        identity = _zone_identity(zone, index)
        net_name = str(zone.GetNetname() or "no net")
        for layer in empty_layers:
            regions.append(
                EmptyCopperZone(
                    zone_id=identity,
                    net_name=net_name,
                    layer_id=int(layer),
                    layer_name=str(board.GetLayerName(layer)),
                    contours=contours,
                    geometry_error=geometry_error,
                    position=position,
                )
            )
    return regions
