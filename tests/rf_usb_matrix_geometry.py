"""Parameterized USB-C launches around the matrix's unchanged controlled cores.

The two GCT USB4105 land patterns keep their real dimensions and opposed mating
faces. Both reversible data contacts connect to the same electrical nets. A
connector-local secondary-layer bridge resolves the right socket's reversed
contact order; both data nets genuinely use that secondary layer. These are
illustrative launches, not impedance-solved or USB-compliance reference designs.
"""

from dataclasses import dataclass
from math import hypot, sqrt

Point = tuple[int, int]
NM_PER_MM = 1_000_000
PAIR_GAP_NM = 203_200
VIA_HALF_PITCH_NM = 450_000
CONNECTOR_EXTENSION_NM = 12_000_000
CONNECTOR_EDGE_OFFSET_NM = 3_675_000
CONNECTOR_NOTCH_HALF_HEIGHT_NM = 6_500_000


@dataclass(frozen=True)
class USBLaunchTrace:
    """A real routed polyline with its native layer, width and USB net."""

    layer: str
    width_nm: int
    net: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class USBLaunchVia:
    """A plated through via, including local ground returns and fences."""

    point: Point
    net: str
    role: str


@dataclass(frozen=True)
class USBConnector:
    """Placement of an unscaled, embedded GCT USB4105 receptacle."""

    reference: str
    point: Point
    rotation: int


@dataclass(frozen=True)
class USBMatrixLaunches:
    """Connector geometry outside a separately declared controlled core."""

    connectors: tuple[USBConnector, ...]
    traces: tuple[USBLaunchTrace, ...]
    vias: tuple[USBLaunchVia, ...]
    # Open side notches end at each real footprint's PCB-edge datum.
    mating_edges: tuple[int, int]
    notch_y: tuple[int, int]


def usb_matrix_launches(
    start: Point,
    end: Point,
    first_layer: str,
    last_layer: str,
    first_width_nm: int,
    last_width_nm: int,
    outer_width_nm: int,
    *,
    coplanar: bool,
) -> USBMatrixLaunches:
    """Join two physical USB sockets to a 2/120/150 mm controlled-core route.

    The core endpoints are on their ordinary coupled pitch. Launch transitions
    lie outside that span. A top-layer core takes a short, genuinely paired
    B.Cu detour at the right socket, so its B.Cu duplicate-contact bridge never
    leaves an incomplete differential pair in the native impedance selection.
    """
    x_start, center_y = start
    x_end, end_y = end
    if center_y != end_y:
        raise ValueError("USB matrix launch ports must share a horizontal centreline")
    half_outer = (outer_width_nm + PAIR_GAP_NM) // 2
    origin_y = center_y + 750_000 - half_outer
    left_x = x_start - CONNECTOR_EXTENSION_NM
    right_x = x_end + CONNECTOR_EXTENSION_NM
    connectors = (
        USBConnector("JUSB1", (left_x, origin_y), 270),
        USBConnector("JUSB2", (right_x, origin_y), 90),
    )
    routes: list[USBLaunchTrace] = []
    vias: list[USBLaunchVia] = []

    def route(layer: str, width: int, net: str, points: tuple[Point, ...]) -> None:
        """Keep junctions explicit without zero-length copper segments."""
        distinct = tuple(
            point
            for index, point in enumerate(points)
            if index == 0 or point != points[index - 1]
        )
        if len(distinct) > 1:
            routes.append(USBLaunchTrace(layer, width, net, distinct))

    def pair_vias(x: int) -> None:
        """Use aligned 0.9 mm-pitch signal vias with symmetric local returns."""
        for net, side in (("USB_D-", -1), ("USB_D+", 1)):
            vias.append(
                USBLaunchVia((x, center_y + side * VIA_HALF_PITCH_NM), net, "signal")
            )
            vias.append(USBLaunchVia((x, center_y + side * 1_350_000), "GND", "return"))

    def bridge(net: str, local_points: tuple[Point, ...], *, right: bool) -> None:
        """Transform contact bridges, not the physical footprint dimensions."""
        transformed = tuple(
            (right_x + y, origin_y - x) if right else (left_x - y, origin_y + x)
            for x, y in local_points
        )
        route("F.Cu", outer_width_nm, net, transformed)

    minus_bridge = (
        (-750_000, -3_680_000),
        (-750_000, -2_850_000),
        (-650_000, -2_750_000),
        (150_000, -2_750_000),
        (250_000, -2_850_000),
        (250_000, -3_680_000),
    )
    plus_bridge = (
        (-250_000, -3_680_000),
        (-250_000, -4_510_000),
        (-150_000, -4_610_000),
        (650_000, -4_610_000),
        (750_000, -4_510_000),
        (750_000, -3_680_000),
    )
    bridge("USB_D-", minus_bridge, right=False)
    bridge("USB_D+", plus_bridge, right=False)
    bridge("USB_D-", minus_bridge, right=True)

    # Left socket: join the duplicated contacts before the coupled launch.
    plus_start_y = origin_y - 250_000
    plus_target_y = center_y + half_outer
    plus_adjust = abs(plus_target_y - plus_start_y)
    left_ends: dict[str, tuple[Point, ...]] = {
        "USB_D-": ((left_x + 3_680_000, center_y - half_outer),),
        "USB_D+": (
            (left_x + 4_510_000, plus_start_y),
            (left_x + 6_000_000, plus_start_y),
            (left_x + 6_000_000 + plus_adjust, plus_target_y),
        ),
    }
    for net, side in (("USB_D-", -1), ("USB_D+", 1)):
        points = left_ends[net]
        if first_layer == "F.Cu":
            route(
                "F.Cu",
                outer_width_nm,
                net,
                (*points, (x_start, center_y + side * half_outer)),
            )
            continue
        via_x = x_start - 1_000_000
        flare = VIA_HALF_PITCH_NM - half_outer
        route(
            "F.Cu",
            outer_width_nm,
            net,
            (
                *points,
                (x_start - 2_000_000, center_y + side * half_outer),
                (x_start - 2_000_000 + flare, center_y + side * VIA_HALF_PITCH_NM),
                (via_x, center_y + side * VIA_HALF_PITCH_NM),
            ),
        )
        half_core = (first_width_nm + PAIR_GAP_NM) // 2
        flare = VIA_HALF_PITCH_NM - half_core
        route(
            first_layer,
            first_width_nm,
            net,
            (
                (via_x, center_y + side * VIA_HALF_PITCH_NM),
                (via_x + 250_000, center_y + side * VIA_HALF_PITCH_NM),
                (via_x + 250_000 + flare, center_y + side * half_core),
                (x_start, center_y + side * half_core),
            ),
        )
    if first_layer != "F.Cu":
        pair_vias(x_start - 1_000_000)

    secondary_layer = "B.Cu" if last_layer == "F.Cu" else last_layer
    secondary_width = outer_width_nm if last_layer == "F.Cu" else last_width_nm
    exit_via_x = x_end + 1_000_000
    return_via_x = x_end + 4_000_000 if last_layer == "F.Cu" else exit_via_x
    half_core = (last_width_nm + PAIR_GAP_NM) // 2
    for net, side in (("USB_D-", -1), ("USB_D+", 1)):
        flare = VIA_HALF_PITCH_NM - half_core
        route(
            last_layer,
            last_width_nm,
            net,
            (
                (x_end, center_y + side * half_core),
                (x_end + 250_000, center_y + side * half_core),
                (x_end + 250_000 + flare, center_y + side * VIA_HALF_PITCH_NM),
                (exit_via_x, center_y + side * VIA_HALF_PITCH_NM),
            ),
        )
        if last_layer == "F.Cu":
            flare = VIA_HALF_PITCH_NM - half_outer
            route(
                secondary_layer,
                secondary_width,
                net,
                (
                    (exit_via_x, center_y + side * VIA_HALF_PITCH_NM),
                    (exit_via_x + 250_000, center_y + side * VIA_HALF_PITCH_NM),
                    (exit_via_x + 250_000 + flare, center_y + side * half_outer),
                    (return_via_x - 250_000 - flare, center_y + side * half_outer),
                    (return_via_x - 250_000, center_y + side * VIA_HALF_PITCH_NM),
                    (return_via_x, center_y + side * VIA_HALF_PITCH_NM),
                ),
            )
    pair_vias(exit_via_x)
    if return_via_x != exit_via_x:
        pair_vias(return_via_x)

    # A true normal offset keeps the 45-degree paired bend at the stated gap.
    corner_offset = round(half_outer * (sqrt(2) - 1))
    bend_x = return_via_x + 1_000_000
    flare = VIA_HALF_PITCH_NM - half_outer
    for net, side in (("USB_D-", -1), ("USB_D+", 1)):
        shifted_y = center_y + 500_000 + side * half_outer
        points = (
            (return_via_x, center_y + side * VIA_HALF_PITCH_NM),
            (return_via_x + 250_000, center_y + side * VIA_HALF_PITCH_NM),
            (return_via_x + 250_000 + flare, center_y + side * half_outer),
            (bend_x - side * corner_offset, center_y + side * half_outer),
            (bend_x + 500_000 - side * corner_offset, shifted_y),
        )
        if net == "USB_D-":
            points += ((right_x - 3_680_000, origin_y - 250_000),)
        else:
            pad_y = origin_y + 250_000
            adjust = abs(pad_y - shifted_y)
            points += (
                (right_x - 6_000_000, shifted_y),
                (right_x - 6_000_000 + adjust, pad_y),
                (right_x - 4_600_000, pad_y),
                (right_x - 3_680_000, pad_y),
            )
        route("F.Cu", outer_width_nm, net, points)

    # Keep the polarity-order crossover beside the socket, not in the long pair.
    bridge_x = right_x - 4_800_000
    upper_bridge_y = origin_y - 950_000
    lower_bridge_y = origin_y + 450_000
    route(
        "F.Cu",
        outer_width_nm,
        "USB_D+",
        (
            (right_x - 3_680_000, origin_y - 750_000),
            (right_x - 4_600_000, origin_y - 750_000),
            (bridge_x, upper_bridge_y),
        ),
    )
    route(
        "F.Cu",
        outer_width_nm,
        "USB_D+",
        ((right_x - 4_600_000, origin_y + 250_000), (bridge_x, lower_bridge_y)),
    )
    route(
        secondary_layer,
        secondary_width,
        "USB_D+",
        ((bridge_x, upper_bridge_y), (bridge_x, lower_bridge_y)),
    )
    vias.extend(
        (
            USBLaunchVia((bridge_x, upper_bridge_y), "USB_D+", "signal"),
            USBLaunchVia((bridge_x, lower_bridge_y), "USB_D+", "signal"),
            USBLaunchVia((bridge_x, origin_y - 2_050_000), "GND", "return"),
            USBLaunchVia((bridge_x, origin_y + 1_550_000), "GND", "return"),
        )
    )

    if coplanar:
        # Fence the straight launch windows; pads and via fanouts stay clear.
        windows = [(left_x + 7_000_000, x_start - 2_000_000)]
        if last_layer == "F.Cu":
            windows.append((exit_via_x + 750_000, return_via_x - 750_000))
        offset = half_outer + outer_width_nm // 2 + 650_000
        for first_x, last_x in windows:
            x = first_x
            while x <= last_x:
                for side in (-1, 1):
                    point = (x, center_y + side * offset)
                    if all(
                        hypot(point[0] - via.point[0], point[1] - via.point[1])
                        >= 800_000
                        for via in vias
                    ):
                        vias.append(USBLaunchVia(point, "GND", "fence"))
                x += 800_000

    return USBMatrixLaunches(
        connectors,
        tuple(routes),
        tuple(vias),
        (left_x - CONNECTOR_EDGE_OFFSET_NM, right_x + CONNECTOR_EDGE_OFFSET_NM),
        (
            origin_y - CONNECTOR_NOTCH_HALF_HEIGHT_NM,
            origin_y + CONNECTOR_NOTCH_HALF_HEIGHT_NM,
        ),
    )
