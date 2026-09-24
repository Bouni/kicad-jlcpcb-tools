"""Native six-layer RF layout fixtures, not electrically certified coupons.

Dimensions intentionally exercise extraction, per-layer widths, references, zone
clearances and capture framing. The 50/90-ohm values are *requested* impedances;
no field solver has certified these illustrative geometries. Native zone fills
are deliberately absent: callers must use pcbnew.ZONE_FILLER before plotting so
KiCad generates real signal antipads and respects custom rules and CPWG keepouts.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from math import ceil, hypot, sqrt
from typing import Optional
from uuid import NAMESPACE_URL, uuid5

from impedance.model import (
    BoardSnapshot,
    Bounds,
    LayerSettings,
    Point,
    Specification,
    Trace,
)
from tests.rf_impedance_showcases import SMA, USB, _children, _footprint, _template
from tests.rf_usb_matrix_geometry import USBMatrixLaunches, usb_matrix_launches

NM_PER_MM = 1_000_000
COPPER_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
SIGNAL_LAYERS = ("F.Cu", "In2.Cu", "B.Cu")
GROUND_LAYERS = ("In1.Cu", "In3.Cu", "In4.Cu")
BOARD_BOUNDS: Bounds = (20_000_000, 20_000_000, 270_000_000, 200_000_000)
COPPER_THICKNESS_NM = 35_000
DIELECTRIC_THICKNESSES_NM = (200_000, 180_000, 180_000, 630_000, 200_000)
BOARD_THICKNESS_NM = 1_600_000
DK = 4.0
FREQUENCY_HZ = 2_000_000_000
FENCE_PITCH_NM = 800_000
VIA_DIAMETER_NM = 600_000
VIA_DRILL_NM = 300_000
TRANSITION_VIA_PITCH_NM = 900_000
RETURN_VIA_MAX_DISTANCE_NM = 1_000_000
GROUND_PAD_INSET_NM = 150_000
DIFFERENTIAL_SPACING_NM = 203_200  # Exactly 8 mil, not a rounded 0.2 mm.
SMA_LAUNCH_EXTENSION_NM = 12_000_000

# Requested same-layer track-to-GND-zone gap, not a universal RF formula.
# The generated native rules leave reference planes and via antipads intact.
NONCOPLANAR_CLEARANCE_FACTOR = 3
REFERENCE_LAYERS = {
    "F.Cu": ("In1.Cu",),
    "In2.Cu": ("In1.Cu", "In3.Cu"),
    "B.Cu": ("In4.Cu",),
}
REFERENCE_HEIGHTS_NM = {
    "F.Cu": (200_000,),
    "In2.Cu": (180_000, 180_000),
    "B.Cu": (200_000,),
}


@dataclass(frozen=True)
class TraceProfile:
    """Nominal widths/gaps for one family and one signal-layer position."""

    width_nm: int
    spacing_nm: Optional[int] = None
    ground_gap_nm: Optional[int] = None


TRACE_PROFILES = {
    "single_ended": {
        "outer": TraceProfile(350_000),
        "inner": TraceProfile(130_000),
    },
    "differential": {
        "outer": TraceProfile(300_000, DIFFERENTIAL_SPACING_NM),
        "inner": TraceProfile(110_000, DIFFERENTIAL_SPACING_NM),
    },
    "single_ended_coplanar": {
        "outer": TraceProfile(300_000, ground_gap_nm=200_000),
        "inner": TraceProfile(110_000, ground_gap_nm=200_000),
    },
    "differential_coplanar": {
        "outer": TraceProfile(270_000, DIFFERENTIAL_SPACING_NM, 200_000),
        "inner": TraceProfile(100_000, DIFFERENTIAL_SPACING_NM, 200_000),
    },
}


def _uuid(label: str) -> str:
    """Give every generated object a stable, repeatable native identity."""
    return str(uuid5(NAMESPACE_URL, f"jlcpcb-tools/rf-captures/{label}"))


def _mm(value: int) -> str:
    """Serialize nanometres exactly at KiCad's six decimal millimetre places."""
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    return f"{sign}{magnitude // NM_PER_MM}.{magnitude % NM_PER_MM:06d}"


def _point_mm(x: int, y: int) -> Point:
    """Express fixture positions in integer millimetres without float drift."""
    return x * NM_PER_MM, y * NM_PER_MM


def _polygon_text(points: tuple[Point, ...]) -> str:
    """Encode one native zone polygon without generating a fake fill cache."""
    return (
        "(polygon (pts " + " ".join(f"(xy {_mm(x)} {_mm(y)})" for x, y in points) + "))"
    )


def _rectangle(bounds: Bounds) -> tuple[Point, ...]:
    """Return the four corners of an axis-aligned envelope."""
    left, top, right, bottom = bounds
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _bounds(points: tuple[Point, ...]) -> Bounds:
    """Compute exact integer bounds for a native polygon."""
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


@dataclass(frozen=True)
class RouteLeg:
    """A nominal horizontal route centreline on a single physical layer."""

    layer: str
    width_nm: int
    spacing_nm: Optional[int]
    ground_gap_nm: Optional[int]
    reference_layers: tuple[str, ...]
    points: tuple[Point, ...]

    @property
    def length_mm(self) -> float:
        """Return the nominal route length, excluding symmetric pair fanout."""
        return (
            sum(
                hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(self.points, self.points[1:])
            )
            / NM_PER_MM
        )


def _profile_layer_settings(
    profiles: tuple[RouteLeg, ...],
) -> tuple[LayerSettings, ...]:
    """Collapse actual widths to one consistent reference/gap entry per layer."""
    by_layer: dict[str, LayerSettings] = {}
    for profile in profiles:
        settings = LayerSettings(
            profile.layer,
            profile.reference_layers,
            profile.spacing_nm,
            profile.ground_gap_nm,
        )
        if profile.layer in by_layer and by_layer[profile.layer] != settings:
            raise ValueError(
                f"Fixture layer {profile.layer} has conflicting reference or gap settings"
            )
        by_layer[profile.layer] = settings
    return tuple(by_layer[layer] for layer in COPPER_LAYERS if layer in by_layer)


@dataclass(frozen=True)
class NativeVia:
    """One physical plated through-via; fence vias can also serve returns."""

    point: Point
    net: str
    layers: tuple[str, ...] = COPPER_LAYERS
    diameter_nm: int = VIA_DIAMETER_NM
    drill_nm: int = VIA_DRILL_NM
    role: str = "fence"
    fence_row: Optional[int] = None
    fence_side: Optional[int] = None

    @property
    def x(self) -> int:
        """Expose the X coordinate for geometry assertions."""
        return self.point[0]

    @property
    def y(self) -> int:
        """Expose the Y coordinate for geometry assertions."""
        return self.point[1]


@dataclass(frozen=True)
class NativeKeepout:
    """A signal-layer zone-fill exclusion, never a reference-plane slot."""

    layer: str
    bounds: Bounds
    purpose: str
    clearance_nm: int
    polygon: tuple[Point, ...]


@dataclass(frozen=True)
class NativeZone:
    """A real native GND zone and the exclusions applicable to its layer."""

    layer: str
    net: str
    bounds: Bounds
    clearance_nm: int
    reference: bool
    keepouts: tuple[NativeKeepout, ...]
    pad_connection: str = "full"


@dataclass(frozen=True)
class RFCase:
    """A complete same-net RF route, optionally paired and changing layers."""

    name: str
    paired: bool
    coplanar: bool
    legs: tuple[RouteLeg, ...]
    target_ohms: str

    @property
    def length_mm(self) -> float:
        """Return nominal centreline length: 2, 120, or 150 mm."""
        return sum(leg.length_mm for leg in self.legs)

    @property
    def kind(self) -> str:
        """Use the same impedance-kind names as production configuration."""
        return ("differential" if self.paired else "single_ended") + (
            "_coplanar" if self.coplanar else ""
        )

    @property
    def title(self) -> str:
        """Provide a gallery label which does not claim solved impedance."""
        description = self.name.replace("-", " ")
        if self.paired:
            return "USB " + description + " (controlled core + USB-C launches)"
        return description + " (nominal test geometry)"

    @property
    def net_class(self) -> str:
        """Name the native engineering class without claiming solved impedance."""
        return "USB differential 90 ohm" if self.paired else "RF single-ended 50 ohm"

    @property
    def net_names(self) -> tuple[str, ...]:
        """Keep every member of the electrical route in the same intent class."""
        return ("USB_D-", "USB_D+") if self.paired else ("RF_SE",)

    @property
    def sma_launches(self) -> bool:
        """Keep physical connectors separate from the two-millimetre stress case."""
        return not self.paired and self.length_mm > 2

    @property
    def launch_type(self) -> str:
        """Describe the actual embedded launch component, including short cores."""
        if self.paired:
            return USB.split(":", 1)[1]
        return SMA.split(":", 1)[1] if self.sma_launches else "compact_testpoint_stress"

    @property
    def launch_count(self) -> int:
        """Both USB and single-ended examples have two physical terminations."""
        return 2

    @property
    def board_bounds(self) -> Bounds:
        """Retain the common contextual board envelope, including USB side notches."""
        return BOARD_BOUNDS

    def usb_launches(self) -> Optional[USBMatrixLaunches]:
        """Provide real same-net USB launch copper outside the controlled core."""
        if not self.paired:
            return None
        first, last = self.legs[0], self.legs[-1]
        return usb_matrix_launches(
            first.points[0],
            last.points[-1],
            first.layer,
            last.layer,
            first.width_nm,
            last.width_nm,
            TRACE_PROFILES[self.kind]["outer"].width_nm,
            coplanar=self.coplanar,
        )

    @property
    def signal_profiles(self) -> tuple[RouteLeg, ...]:
        """List every actual signal layer/width, including visible USB launches."""
        profiles = {(leg.layer, leg.width_nm): leg for leg in self.legs}
        launches = self.usb_launches()
        if launches is not None:
            for trace in launches.traces:
                key = (trace.layer, trace.width_nm)
                if key not in profiles:
                    profiles[key] = RouteLeg(
                        trace.layer,
                        trace.width_nm,
                        DIFFERENTIAL_SPACING_NM,
                        200_000 if self.coplanar else None,
                        REFERENCE_LAYERS[trace.layer],
                        (trace.points[0], trace.points[-1]),
                    )
        return tuple(
            profiles[key]
            for key in sorted(
                profiles, key=lambda item: (COPPER_LAYERS.index(item[0]), item[1])
            )
        )

    def _member_points(self, leg_index: int, member: int) -> tuple[Point, ...]:
        """Fan pairs symmetrically to clear pads and common transition vias."""
        leg = self.legs[leg_index]
        if not self.paired:
            points = list(leg.points)
            if self.sma_launches and leg_index == 0:
                points.insert(0, (points[0][0] - SMA_LAUNCH_EXTENSION_NM, points[0][1]))
            if self.sma_launches and leg_index == len(self.legs) - 1:
                points.append((points[-1][0] + SMA_LAUNCH_EXTENSION_NM, points[-1][1]))
            return tuple(points)
        direction = -1 if member == 0 else 1
        half_pitch = (leg.width_nm + (leg.spacing_nm or 0)) // 2
        start, end = leg.points
        start_offset = half_pitch if leg_index == 0 else TRANSITION_VIA_PITCH_NM // 2
        end_offset = (
            half_pitch
            if leg_index == len(self.legs) - 1
            else TRANSITION_VIA_PITCH_NM // 2
        )
        # A 45-degree fanout changes X by the same amount as the pitch offset.
        # These short transition regions are not claimed as solved cross-sections.
        points = [(start[0], start[1] + direction * start_offset)]
        if start_offset != half_pitch:
            points.append(
                (
                    start[0] + abs(start_offset - half_pitch),
                    start[1] + direction * half_pitch,
                )
            )
        if end_offset != half_pitch:
            points.append(
                (end[0] - abs(end_offset - half_pitch), end[1] + direction * half_pitch)
            )
        points.append((end[0], end[1] + direction * end_offset))
        return tuple(points)

    def traces(self) -> tuple[Trace, ...]:
        """Return every native signal track, with stable IDs and exact nets."""
        traces = []
        for leg_index, leg in enumerate(self.legs):
            for member in range(2 if self.paired else 1):
                net = self.net_names[member]
                points = self._member_points(leg_index, member)
                for piece, (start, end) in enumerate(zip(points, points[1:])):
                    traces.append(
                        Trace(
                            _uuid(f"{self.name}/track/{leg_index}/{net}/{piece}"),
                            leg.layer,
                            net,
                            leg.width_nm,
                            (start, end),
                        )
                    )
        launches = self.usb_launches()
        if launches is not None:
            for route_index, route in enumerate(launches.traces):
                for piece, (start, end) in enumerate(
                    zip(route.points, route.points[1:])
                ):
                    traces.append(
                        Trace(
                            _uuid(f"{self.name}/usb-launch/{route_index}/{piece}"),
                            route.layer,
                            route.net,
                            route.width_nm,
                            (start, end),
                        )
                    )
        return tuple(traces)

    def specifications(self) -> tuple[Specification, ...]:
        """Match the route's class across all core and connector-launch layers."""
        return (self.netclass_specification(),)

    def netclass_specification(self) -> Specification:
        """Describe one class intent with independently configured layer references."""
        return Specification(
            spec_id=f"{self.name}/netclass",
            label=self.net_class,
            target_ohms=self.target_ohms,
            kind=self.kind,
            net_class=self.net_class,
            layer_settings=_profile_layer_settings(self.signal_profiles),
        )

    def snapshot(self) -> BoardSnapshot:
        """Expose only the RF route's fixture metadata for class-mode UI tests."""
        memberships = tuple((net, (self.net_class,)) for net in self.net_names)
        return BoardSnapshot(
            COPPER_LAYERS,
            self.traces(),
            self.board_text(),
            net_classes=(self.net_class,),
            net_class_memberships=memberships,
            net_class_context_digest=sha256(
                repr((self.net_class, memberships)).encode("utf-8")
            ).hexdigest(),
            differential_pairs=(("USB_D-", "USB_D+"),) if self.paired else (),
        )

    def _transition_signal_vias(self) -> tuple[NativeVia, ...]:
        """Make signal centres coincide exactly on both connected layers."""
        result = []
        for leg_index in range(len(self.legs) - 1):
            for member in range(2 if self.paired else 1):
                point = self._member_points(leg_index, member)[-1]
                net = self.net_names[member]
                result.append(NativeVia(point, net, role="signal"))
        return tuple(result)

    def _fence_vias(self) -> tuple[NativeVia, ...]:
        """Fence complete core/SMA copper on one grid, with safe pad-clearance ramps."""
        if not self.coplanar:
            return ()
        result = []
        paths = tuple(self._member_points(index, 0) for index in range(len(self.legs)))
        start, end = paths[0][0][0], paths[-1][-1][0]
        # Launch pads and layer-change vias are wider than the routed copper.
        # Let their ordinary antipads locally enlarge the coplanar channel.
        exclusions = [
            (
                *self._member_points(leg_index, member)[endpoint],
                VIA_DIAMETER_NM + 200_000,
            )
            for leg_index in range(len(self.legs))
            for endpoint in (0, -1)
            for member in range(2 if self.paired else 1)
        ]
        if self.sma_launches:
            # Read the pinned footprint, not a via-sized approximation of its pads.
            for pad in _children(_template(SMA), "pad"):
                pad_x, pad_y = (
                    round(float(value) * NM_PER_MM)
                    for value in _children(pad, "at")[0][1:3]
                )
                diameter = round(float(_children(pad, "size")[0][1]) * NM_PER_MM)
                drill = round(float(_children(pad, "drill")[0][1]) * NM_PER_MM)
                minimum_centres = (drill + VIA_DRILL_NM) // 2 + 250_000
                if pad[1] == '"1"':
                    minimum_centres = max(
                        minimum_centres, (diameter + VIA_DIAMETER_NM) // 2 + 200_000
                    )
                exclusions.extend(
                    (x + pad_x, y + pad_y, minimum_centres)
                    for x, y in (paths[0][0], paths[-1][-1])
                )
        x = start
        while x <= end:
            leg_index = next(
                index
                for index, points in enumerate(paths)
                if points[0][0] <= x <= points[-1][0]
            )
            leg = self.legs[leg_index]
            for side, member in ((-1, 0), (1, 1 if self.paired else 0)):
                points = self._member_points(leg_index, member)
                first, second = next(
                    (a, b) for a, b in zip(points, points[1:]) if a[0] <= x <= b[0]
                )
                y = first[1] + round(
                    (second[1] - first[1]) * (x - first[0]) / (second[0] - first[0])
                )
                offset = (
                    leg.width_nm // 2
                    + (leg.ground_gap_nm or 0)
                    + VIA_DIAMETER_NM // 2
                    + GROUND_PAD_INSET_NM
                )
                y += side * offset
                for pad_x, pad_y, minimum_centres in exclusions:
                    delta_x = abs(x - pad_x)
                    if delta_x < minimum_centres:
                        minimum_y = ceil(sqrt(minimum_centres**2 - delta_x**2)) + 2
                        if abs(y - pad_y) < minimum_y:
                            y = pad_y + side * minimum_y
                result.append(
                    NativeVia(
                        (x, y),
                        "GND",
                        fence_row=0,
                        fence_side=side,
                    )
                )
            x += FENCE_PITCH_NM
        if self.sma_launches:
            # The pad's circular antipad can otherwise make a diagonal fence step
            # exceed the 2 GHz pitch. A two-pass outward envelope preserves the
            # existing X grid and holes while limiting each Y step to half its pitch.
            for side in (-1, 1):
                indices = [
                    index for index, via in enumerate(result) if via.fence_side == side
                ]
                for ordered in (indices, indices[::-1]):
                    for previous, current in zip(ordered, ordered[1:]):
                        y = side * max(
                            side * result[current].y,
                            side * result[previous].y - FENCE_PITCH_NM // 2,
                        )
                        result[current] = replace(
                            result[current], point=(result[current].x, y)
                        )
        return tuple(result)

    def vias(self) -> tuple[NativeVia, ...]:
        """Include stitched fences, symmetric returns and actual signal vias."""
        signal_vias = self._transition_signal_vias()
        fences = self._fence_vias()
        if self.coplanar:
            return_indices = set()
            for signal in signal_vias:
                for side in (-1, 1):
                    if self.paired and side != (-1 if signal.net == "USB_D-" else 1):
                        continue
                    candidates = [
                        (hypot(via.x - signal.x, via.y - signal.y), index)
                        for index, via in enumerate(fences)
                        if via.fence_row == 0 and via.fence_side == side
                    ]
                    if candidates:
                        distance, index = min(candidates)
                        if distance <= RETURN_VIA_MAX_DISTANCE_NM:
                            return_indices.add(index)
            grounds = tuple(
                NativeVia(
                    via.point,
                    via.net,
                    role="return" if index in return_indices else "fence",
                    fence_row=via.fence_row,
                    fence_side=via.fence_side,
                )
                for index, via in enumerate(fences)
            )
        else:
            grounds_list = []
            for leg in self.legs[:-1]:
                x, y = leg.points[-1]
                offset = 800_000 + (TRANSITION_VIA_PITCH_NM // 2 if self.paired else 0)
                grounds_list.extend(
                    NativeVia((x, y + side * offset), "GND", role="return")
                    for side in (-1, 1)
                )
            grounds = tuple(grounds_list)
        # Stitch otherwise isolated same-layer pours into all reference planes.
        corner_stitches = tuple(
            NativeVia(_point_mm(x, y), "GND", role="stitch")
            for x, y in ((35, 35), (255, 35), (35, 185), (255, 185))
        )
        launches = self.usb_launches()
        launch_vias = (
            tuple(NativeVia(via.point, via.net, role=via.role) for via in launches.vias)
            if launches is not None
            else ()
        )
        return (*signal_vias, *grounds, *corner_stitches, *launch_vias)

    def keepouts(self) -> tuple[NativeKeepout, ...]:
        """Retain CPWG corridors; native rules set noncoplanar pour clearance."""
        if not self.coplanar:
            return ()
        result = []
        for index, leg in enumerate(self.legs):
            clearance = leg.ground_gap_nm or 0
            radius = leg.width_nm // 2 + clearance
            upper = self._member_points(index, 0)
            lower = self._member_points(index, 1 if self.paired else 0)
            top = tuple((x, y - radius) for x, y in upper)
            bottom = tuple((x, y + radius) for x, y in reversed(lower))
            # Square end caps extend the corridor to protect launch copper.
            polygon = (
                (top[0][0] - clearance, top[0][1]),
                *top,
                (top[-1][0] + clearance, top[-1][1]),
                (bottom[0][0] + clearance, bottom[0][1]),
                *bottom,
                (bottom[-1][0] - clearance, bottom[-1][1]),
            )
            result.append(
                NativeKeepout(
                    leg.layer,
                    _bounds(polygon),
                    "coplanar-gap",
                    clearance,
                    polygon,
                )
            )
        return tuple(result)

    def zones(self) -> tuple[NativeZone, ...]:
        """Provide three intact ground planes and GND pours on signal layers."""
        exclusions = self.keepouts()
        return tuple(
            NativeZone(
                layer,
                "GND",
                BOARD_BOUNDS,
                200_000,
                layer in GROUND_LAYERS,
                tuple(item for item in exclusions if item.layer == layer),
            )
            for layer in COPPER_LAYERS
        )

    def board_text(self) -> str:
        """Generate a loadable board; fill its real zones before capture."""
        return board_text(self)


def _leg(kind: str, layer: str, start: Point, end: Point) -> RouteLeg:
    """Select the fixture profile from physical, not numerical, layer order."""
    profile = TRACE_PROFILES[kind]["inner" if layer == "In2.Cu" else "outer"]
    return RouteLeg(
        layer,
        profile.width_nm,
        profile.spacing_nm,
        profile.ground_gap_nm,
        REFERENCE_LAYERS[layer],
        (start, end),
    )


def _cases() -> tuple[RFCase, ...]:
    """Build 24 short/long layer cases plus four complete transition cases."""
    cases = []
    for paired in (False, True):
        for coplanar in (False, True):
            family = "differential" if paired else "single-ended"
            kind = ("differential" if paired else "single_ended") + (
                "_coplanar" if coplanar else ""
            )
            for position, layer in zip(("top", "inner", "bottom"), SIGNAL_LAYERS):
                topology = (
                    ("buried-cpwg" if position == "inner" else "cpwg")
                    if coplanar
                    else ("stripline" if position == "inner" else "microstrip")
                )
                for length in (2, 120):
                    start = _point_mm(215, 145) if length == 2 else _point_mm(60, 100)
                    end = (start[0] + length * NM_PER_MM, start[1])
                    cases.append(
                        RFCase(
                            f"{family}-{topology}-{position}-{length}mm",
                            paired,
                            coplanar,
                            (_leg(kind, layer, start, end),),
                            "90" if paired else "50",
                        )
                    )
            cases.append(
                RFCase(
                    f"{family}-{'cpwg' if coplanar else 'noncoplanar'}-transition-150mm",
                    paired,
                    coplanar,
                    tuple(
                        _leg(
                            kind,
                            layer,
                            _point_mm(60 + index * 50, 100),
                            _point_mm(110 + index * 50, 100),
                        )
                        for index, layer in enumerate(SIGNAL_LAYERS)
                    ),
                    "90" if paired else "50",
                )
            )
    return tuple(cases)


RF_CASES = _cases()


def _stackup() -> str:
    """Serialize explicit copper/dielectric dimensions rather than assumptions."""
    layers = []
    for index, layer in enumerate(COPPER_LAYERS):
        layers.append(
            f'(layer "{layer}" (type "copper") (thickness {_mm(COPPER_THICKNESS_NM)}))'
        )
        if index < len(DIELECTRIC_THICKNESSES_NM):
            layers.append(
                f'(layer "dielectric {index + 1}" '
                f'(type "{"prepreg" if index % 2 == 0 else "core"}") '
                f"(thickness {_mm(DIELECTRIC_THICKNESSES_NM[index])}) "
                f'(material "FR4 nominal fixture") (epsilon_r {DK}) (loss_tangent 0.02))'
            )
    return "(stackup " + " ".join(layers) + '(copper_finish "ENIG"))'


def _native_zone(case: RFCase, zone: NativeZone) -> str:
    """Ask KiCad for actual solid connected copper, not drawn rectangles."""
    return (
        f'(zone (net 4) (net_name "GND") (layer "{zone.layer}") '
        f'(uuid "{_uuid(f"{case.name}/zone/{zone.layer}")}") '
        f'(name "{"Reference plane" if zone.reference else "Signal-layer GND pour"}") '
        "(hatch edge 0.5) (connect_pads yes (clearance 0.2)) "
        "(min_thickness 0.05) (filled_areas_thickness no) "
        "(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)) "
        f"{_polygon_text(_rectangle(zone.bounds))})"
    )


def _native_keepout(case: RFCase, index: int, keepout: NativeKeepout) -> str:
    """Keep tracks/vias/pads legal inside a copper-pour-only exclusion."""
    return (
        f'(zone (net 0) (net_name "") (layer "{keepout.layer}") '
        f'(uuid "{_uuid(f"{case.name}/keepout/{index}")}") '
        f'(name "{keepout.purpose}") (hatch edge 0.5) '
        "(connect_pads (clearance 0)) (min_thickness 0.05) "
        "(keepout (tracks allowed) (vias allowed) (pads allowed) "
        "(copperpour not_allowed) (footprints allowed)) "
        "(fill (thermal_gap 0.3) (thermal_bridge_width 0.3)) "
        f"{_polygon_text(keepout.polygon)})"
    )


def _landmarks(case: RFCase) -> list[str]:
    """Place actual components, silkscreen, fab details and nearby routing."""
    items = []
    for index, (x, y) in enumerate(((28, 28), (262, 28), (262, 192), (28, 192))):
        items.append(
            f'(footprint "RFContext:Mount" (layer "F.Cu") (at {x} {y}) '
            f'(uuid "{_uuid(f"{case.name}/mount/{index}")}") '
            '(pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) '
            '(layers "*.Cu" "*.Mask")))'
        )
    for index, (ref, x, y, side) in enumerate(
        (
            ("U1", 62, 68, "F"),
            ("U2", 155, 165 if case.paired else 137, "F"),
            ("J1", 232, 55 if case.paired else 137, "B"),
            ("U3", 128, 55, "F"),
        )
    ):
        mirror = " (justify mirror)" if side == "B" else ""
        pads = " ".join(
            f'(pad "{row * 4 + column + 1}" thru_hole rect '
            f"(at {column * 2.54:.2f} {row * 7.62:.2f}) (size 1.7 2) (drill 0.8) "
            '(layers "*.Cu" "*.Mask") (net 4 "GND"))'
            for row in range(2)
            for column in range(4)
        )
        items.append(
            f'(footprint "RFContext:{ref}" (layer "{side}.Cu") (at {x} {y}) '
            f'(uuid "{_uuid(f"{case.name}/landmark/{index}")}") '
            f'(fp_text reference "{ref}" (at 3.8 -3) (layer "{side}.SilkS") '
            f"(effects (font (size 1.5 1.5) (thickness 0.2)){mirror})) "
            f'(fp_text value "RF_CONTEXT" (at 3.8 11) (layer "{side}.Fab") '
            f"(effects (font (size 1 1) (thickness 0.15)){mirror})) "
            f"(fp_rect (start -2 -1.5) (end 10 9.2) "
            f'(stroke (width 0.2) (type default)) (fill none) (layer "{side}.SilkS")) '
            f"(fp_rect (start -1.5 -1) (end 9.5 8.7) "
            f'(stroke (width 0.1) (type default)) (fill none) (layer "{side}.Fab")) '
            f"{pads})"
        )
    for layer_index, layer in enumerate(SIGNAL_LAYERS):
        for line in range(3):
            # USB connector-edge notches open at y ~= 100 or 145. Keep the
            # unrelated context copper and components outside those openings.
            context_rows = (78, 118, 178) if case.paired else (84, 112, 128)
            y = context_rows[line] + layer_index * 2
            start = (42, y)
            corner_x = 245 if line == 0 else 225 - line * 15
            points = (start, (corner_x - 3, y), (corner_x, y + 3), (corner_x, y + 8))
            for piece, (first, second) in enumerate(zip(points, points[1:])):
                items.append(
                    f"(segment (start {first[0]} {first[1]}) "
                    f"(end {second[0]} {second[1]}) (width 0.25) "
                    f'(layer "{layer}") (net {5 + layer_index * 3 + line}) '
                    f'(uuid "{_uuid(f"{case.name}/context/{layer}/{line}/{piece}")}"))'
                )
    for index, side in enumerate(("F", "B")):
        mirror = " (justify mirror)" if side == "B" else ""
        items.append(
            f'(gr_text "RF CAPTURE FIXTURE / 250 x 180 mm" (at 145 {42 + index * 135}) '
            f'(layer "{side}.SilkS") '
            f"(effects (font (size 2 2) (thickness 0.25)){mirror}))"
        )
    return items


def _launches(case: RFCase) -> list[str]:
    """Use two real USB-C sockets for every paired core, including 2 mm cores."""
    result = []
    usb = case.usb_launches()
    if usb is not None:
        codes = _net_codes(case)
        signal_pins = {
            "A6": "USB_D+",
            "B6": "USB_D+",
            "A7": "USB_D-",
            "B7": "USB_D-",
            "A1": "GND",
            "B1": "GND",
            "A12": "GND",
            "B12": "GND",
            "SH": "GND",
        }
        for connector in usb.connectors:
            assigned = {
                **signal_pins,
                **dict.fromkeys(("A4", "B9"), connector.reference + "_UNUSED_VBUS_1"),
                **dict.fromkeys(("A9", "B4"), connector.reference + "_UNUSED_VBUS_2"),
            }
            x, y = connector.point
            result.append(
                _footprint(
                    USB,
                    connector.reference,
                    (x / NM_PER_MM, y / NM_PER_MM, connector.rotation),
                    assigned,
                    case.name,
                    net_codes=codes,
                    value="USB-C",
                    value_at=(-6.5 if connector.rotation == 270 else 6.5, 0),
                )
            )
        return result
    for launch_index, (leg_index, endpoint) in enumerate(
        ((0, 0), (len(case.legs) - 1, -1))
    ):
        for member in range(1):
            net = "RF_SE"
            net_id = 1
            x, y = case._member_points(leg_index, member)[endpoint]
            if case.sma_launches:
                result.append(
                    _footprint(
                        SMA,
                        f"JRF{launch_index * (2 if case.paired else 1) + member + 1}",
                        (x / NM_PER_MM, y / NM_PER_MM, 0),
                        {"1": net, "2": "GND"},
                        case.name,
                        net_codes={"RF_SE": 1, "RF_P": 2, "RF_N": 3, "GND": 4},
                    )
                )
                continue
            label_x = -2 if launch_index == 0 else 2
            label_y = (
                (2 if member else -2)
                if case.paired
                else (-2 if launch_index == 0 else 2)
            )
            result.append(
                f'(footprint "RFContext:Testpoint" (layer "F.Cu") '
                f"(at {_mm(x)} {_mm(y)}) "
                f'(uuid "{_uuid(f"{case.name}/launch/{launch_index}/{member}")}") '
                f'(fp_text reference "TP{launch_index * 2 + member + 1}" '
                f'(at {label_x} {label_y}) (layer "F.SilkS") '
                "(effects (font (size 0.8 0.8) (thickness 0.15)))) "
                '(pad "1" thru_hole circle (at 0 0) (size 0.6 0.6) (drill 0.3) '
                f'(layers "*.Cu" "*.Mask") (net {net_id} "{net}")))'
            )
    return result


def _net_codes(case: RFCase) -> dict[str, int]:
    """Give actual USB data and isolated unused VBUS lands native net identities."""
    codes = {"": 0, "RF_SE": 1, "GND": 4}
    codes.update({"USB_D-": 2, "USB_D+": 3} if case.paired else {"RF_P": 2, "RF_N": 3})
    codes.update({f"CONTEXT_{index + 1}": 5 + index for index in range(9)})
    if case.paired:
        codes.update(
            {
                f"JUSB{connector}_UNUSED_VBUS_{land}": (
                    14 + (connector - 1) * 2 + land - 1
                )
                for connector in (1, 2)
                for land in (1, 2)
            }
        )
    return codes


def _outline(case: RFCase) -> list[str]:
    """Leave usable mating-face openings without shrinking the board context."""
    usb = case.usb_launches()
    if usb is None:
        return [
            "(gr_rect (start 20 20) (end 270 200) (stroke (width 0.15) (type default)) "
            '(fill none) (layer "Edge.Cuts"))'
        ]
    left, top, right, bottom = case.board_bounds
    mouth_left, mouth_right = usb.mating_edges
    notch_top, notch_bottom = usb.notch_y
    points = (
        (left, top),
        (right, top),
        (right, notch_top),
        (mouth_right, notch_top),
        (mouth_right, notch_bottom),
        (right, notch_bottom),
        (right, bottom),
        (left, bottom),
        (left, notch_bottom),
        (mouth_left, notch_bottom),
        (mouth_left, notch_top),
        (left, notch_top),
    )
    return [
        f"(gr_line (start {_mm(start[0])} {_mm(start[1])}) "
        f"(end {_mm(end[0])} {_mm(end[1])}) (stroke (width 0.15) (type default)) "
        f'(layer "Edge.Cuts") (uuid "{_uuid(f"{case.name}/edge/{index}")}"))'
        for index, (start, end) in enumerate(zip(points, (*points[1:], points[0])))
    ]


def board_text(case: RFCase) -> str:
    """Serialize the complete native board; no external library assets needed."""
    layer_entries = " ".join(
        f'({layer_id} "{layer}" {"power" if layer in GROUND_LAYERS else "signal"})'
        for layer_id, layer in zip((0, 1, 2, 3, 4, 31), COPPER_LAYERS)
    )
    technical_layers = (
        '(35 "F.Paste" user) (36 "B.SilkS" user "b.silkscreen") '
        '(37 "F.SilkS" user "f.silkscreen") '
        '(38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) '
        '(47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user)'
    )
    net_ids = _net_codes(case)
    items = [
        '(kicad_pcb (version 20240108) (generator "jlcpcb_rf_capture_fixture")',
        f'(general (thickness {_mm(BOARD_THICKNESS_NM)})) (paper "A3")',
        f"(layers {layer_entries} {technical_layers})",
        f"(setup {_stackup()} (pad_to_mask_clearance 0))",
        " ".join(
            f'(net {code} "{net}")'
            for net, code in sorted(net_ids.items(), key=lambda item: item[1])
        ),
        *_outline(case),
        *_landmarks(case),
        *_launches(case),
    ]
    if case.paired:
        items.append(
            f'(gr_text "USB 90 ohm / {case.length_mm:g} mm controlled core + USB-C launches / 8 mil pair gap" '
            '(at 145 46) (layer "F.SilkS") (effects (font (size 1.2 1.2) (thickness 0.18))))'
        )
    for trace in case.traces():
        start, end = trace.points
        items.append(
            f"(segment (start {_mm(start[0])} {_mm(start[1])}) "
            f"(end {_mm(end[0])} {_mm(end[1])}) (width {_mm(trace.width_nm)}) "
            f'(layer "{trace.layer}") (net {net_ids[trace.net]}) '
            f'(uuid "{trace.trace_id}"))'
        )
    for index, via in enumerate(case.vias()):
        items.append(
            f"(via (at {_mm(via.x)} {_mm(via.y)}) "
            f"(size {_mm(via.diameter_nm)}) (drill {_mm(via.drill_nm)}) "
            f'(layers "{via.layers[0]}" "{via.layers[-1]}") (net {net_ids[via.net]}) '
            f'(uuid "{_uuid(f"{case.name}/via/{index}")}"))'
        )
    items.extend(_native_zone(case, zone) for zone in case.zones())
    items.extend(
        _native_keepout(case, index, keepout)
        for index, keepout in enumerate(case.keepouts())
    )
    return "\n".join((*items, ")", ""))
