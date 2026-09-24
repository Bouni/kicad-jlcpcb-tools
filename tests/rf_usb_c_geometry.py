"""Illustrative two-USB-C-socket pair topology, in PCB millimetres.

Both reversible data contacts are connected at each GCT USB4105 socket. The
long CPWG trunk stays together through symmetric F.Cu/B.Cu/F.Cu transitions.
Opposed sockets reverse contact ordering; a small B.Cu bridge joins J2's two
D+ contacts locally, instead of crossing and separating the long pair route.
The straight coupled windows have the nominal 8 mil edge gap. Connector
launches and via fanouts are not solved impedance structures, and this passive
layout coupon is not a USB-compliance reference design.
"""

from dataclasses import dataclass
from math import sqrt

Point = tuple[float, float]
WIDTH_NM = 270_000
GAP_NM = 203_200

# Fence generators use these centre-line windows, leaving the flared
# transitions and the connector launches clear of through-hole fence vias.
F_COUPLED_START: Point = (42.0, 64.4866)
F_COUPLED_END: Point = (184.0, 64.4866)
B_COUPLED_START: Point = (186.0, 64.4866)
B_COUPLED_END: Point = (214.0, 64.4866)


@dataclass(frozen=True)
class Route:
    """One contiguous 0.27 mm copper polyline, without implicit endpoint joins."""

    layer: str
    net: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class Via:
    """One 0.6/0.3 mm through via, including explicit local GND returns."""

    net: str
    point: Point


def _bridge_points(points: tuple[Point, ...], right: bool) -> tuple[Point, ...]:
    """Transform exact local footprint coordinates for the two opposed sockets."""
    if right:
        return tuple((round(225 + y, 4), round(65 - x, 4)) for x, y in points)
    return tuple((round(35 - y, 4), round(65 + x, 4)) for x, y in points)


def usb_routes() -> tuple[Route, ...]:
    """Keep the main pair coupled; confine the orientation bridge to J2."""
    plus_bridge = (
        (-0.25, -3.68),
        (-0.25, -4.51),
        (-0.15, -4.61),
        (0.65, -4.61),
        (0.75, -4.51),
        (0.75, -3.68),
    )
    minus_bridge = (
        (-0.75, -3.68),
        (-0.75, -2.85),
        (-0.65, -2.75),
        (0.15, -2.75),
        (0.25, -2.85),
        (0.25, -3.68),
    )
    bridges = (
        Route("F.Cu", "USB_D-", _bridge_points(minus_bridge, False)),
        Route("F.Cu", "USB_D+", _bridge_points(plus_bridge, False)),
        Route("F.Cu", "USB_D-", _bridge_points(minus_bridge, True)),
        Route(
            "F.Cu",
            "USB_D+",
            ((221.32, 64.25), (220.4, 64.25), (220.2, 64.05)),
        ),
        Route(
            "F.Cu",
            "USB_D+",
            ((220.4, 65.25), (220.2, 65.45)),
        ),
        Route("B.Cu", "USB_D+", ((220.2, 64.05), (220.2, 65.45))),
    )
    # True normal offset at the two 45-degree return-launch corners. Merely
    # translating a diagonal vertically would narrow its differential gap.
    corner_offset = ((WIDTH_NM + GAP_NM) / 2_000_000) * (sqrt(2) - 1)
    return bridges + (
        Route(
            "F.Cu",
            "USB_D-",
            (
                (38.68, 64.25),
                (184.5, 64.25),
                (184.7134, 64.0366),
                (185.0, 64.0366),
            ),
        ),
        Route(
            "B.Cu",
            "USB_D-",
            (
                (185.0, 64.0366),
                (185.2866, 64.0366),
                (185.5, 64.25),
                (214.5, 64.25),
                (214.7134, 64.0366),
                (215.0, 64.0366),
            ),
        ),
        Route(
            "F.Cu",
            "USB_D-",
            (
                (215.0, 64.0366),
                (215.2866, 64.0366),
                (215.5, 64.25),
                (216.0 + corner_offset, 64.25),
                (216.5 + corner_offset, 64.75),
                (221.32, 64.75),
            ),
        ),
        Route(
            "F.Cu",
            "USB_D+",
            (
                # The existing A6 bridge lead reaches this branching vertex.
                (39.51, 64.75),
                (41.0, 64.75),
                (41.0268, 64.7232),
                (184.5, 64.7232),
                (184.7134, 64.9366),
                (185.0, 64.9366),
            ),
        ),
        Route(
            "B.Cu",
            "USB_D+",
            (
                (185.0, 64.9366),
                (185.2866, 64.9366),
                (185.5, 64.7232),
                (214.5, 64.7232),
                (214.7134, 64.9366),
                (215.0, 64.9366),
            ),
        ),
        Route(
            "F.Cu",
            "USB_D+",
            (
                (215.0, 64.9366),
                (215.2866, 64.9366),
                (215.5, 64.7232),
                (216.0 - corner_offset, 64.7232),
                (216.5 - corner_offset, 65.2232),
                (218.0, 65.2232),
                (218.0268, 65.25),
                # Explicit branch to the duplicate-contact bridge, without
                # laying the same copper lead down twice.
                (220.4, 65.25),
                (221.32, 65.25),
            ),
        ),
    )


def usb_vias() -> tuple[Via, ...]:
    """Return aligned trunk pairs and the compact J2 contact-bridge vias."""
    return (
        Via("USB_D-", (185.0, 64.0366)),
        Via("USB_D+", (185.0, 64.9366)),
        Via("USB_D-", (215.0, 64.0366)),
        Via("USB_D+", (215.0, 64.9366)),
        Via("USB_D+", (220.2, 64.05)),
        Via("USB_D+", (220.2, 65.45)),
        Via("GND", (185.0, 62.9366)),
        Via("GND", (185.0, 66.0366)),
        Via("GND", (215.0, 62.9366)),
        Via("GND", (215.0, 66.0366)),
        Via("GND", (220.2, 62.95)),
        Via("GND", (220.2, 66.55)),
    )
