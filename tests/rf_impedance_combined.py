"""Two openable six-layer example boards, each containing fourteen RF circuits.

The noncoplanar and CPWG banks share one physical board and its six GND pours.
Every circuit has independent signal nets and two real connectors. Nominal
2/120/150 mm controlled cores remain distinct from their connector launches.
These geometries illustrate the plugin workflow; they are not solver-qualified
impedance coupons or USB-compliance reference designs.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
import json

from impedance.model import BoardSnapshot, Bounds, Specification, Trace
from tests.rf_impedance_fixtures import (
    BOARD_THICKNESS_NM,
    COPPER_LAYERS,
    GROUND_LAYERS,
    NM_PER_MM,
    RF_CASES,
    NativeKeepout,
    NativeVia,
    RFCase,
    RouteLeg,
    _mm,
    _native_keepout,
    _polygon_text,
    _profile_layer_settings,
    _rectangle,
    _stackup,
    _uuid,
)
from tests.rf_impedance_showcases import SMA, USB, _footprint

COMBINED_BOUNDS: Bounds = (20_000_000, 20_000_000, 250_000_000, 350_000_000)


class _ConnectorCase(RFCase):
    """Keep SMA connectors on short single-ended cores as well as long ones."""

    @property
    def sma_launches(self) -> bool:
        """The two combined boards have no testpoint-only connector exceptions."""
        return not self.paired


@dataclass(frozen=True)
class CombinedCircuit:
    """One positioned circuit, with unique signal nets on the shared board."""

    name: str
    number: int
    source: RFCase

    @property
    def paired(self) -> bool:
        """Identify USB differential versus single-ended RF circuitry."""
        return self.source.paired

    @property
    def coplanar(self) -> bool:
        """Select the bank's same-layer ground-pour policy."""
        return self.source.coplanar

    @property
    def kind(self) -> str:
        """Expose the production impedance-kind identifier."""
        return self.source.kind

    @property
    def target_ohms(self) -> str:
        """Retain the requested 50/90-ohm target, not a solved result."""
        return self.source.target_ohms

    @property
    def title(self) -> str:
        """Label core geometry separately from the real connector launches."""
        prefix = "USB " if self.paired else "RF "
        topology = self.name.removeprefix("differential-").removeprefix("single-ended-")
        return (
            f"{prefix}{self.number:02d}: {topology.replace('-', ' ')} core + launches"
        )

    @property
    def length_mm(self) -> float:
        """Keep the controlled core's nominal length, excluding launches."""
        return self.source.length_mm

    @property
    def legs(self) -> tuple[RouteLeg, ...]:
        """Return the positioned controlled-core legs only."""
        return self.source.legs

    @property
    def signal_profiles(self) -> tuple[RouteLeg, ...]:
        """Include all actual signal layers/widths used by core and launches."""
        return self.source.signal_profiles

    @property
    def net_class(self) -> str:
        """Give each bank one engineering class shared by its seven circuits."""
        family = "USB 90 ohm" if self.paired else "RF 50 ohm"
        return family + (" CPWG" if self.coplanar else " noncoplanar")

    @property
    def net_names(self) -> tuple[str, ...]:
        """Native USB +/- suffixes identify each independent differential pair."""
        if self.paired:
            return (f"USB{self.number:02d}_D-", f"USB{self.number:02d}_D+")
        return (f"RF{self.number:02d}",)

    @property
    def launch_type(self) -> str:
        """Report actual unscaled KiCad connector library geometry."""
        return (USB if self.paired else SMA).split(":", 1)[1]

    @property
    def launch_count(self) -> int:
        """Every circuit, including a 2 mm core, has two physical connectors."""
        return 2

    @property
    def board_bounds(self) -> Bounds:
        """All circuits belong to the same physical board envelope."""
        return COMBINED_BOUNDS

    def _net_map(self) -> dict[str, str]:
        """Translate local reusable geometry to independent native signal nets."""
        return dict(zip(self.source.net_names, self.net_names))

    def traces(self) -> tuple[Trace, ...]:
        """Keep every real core/launch segment and its stable native identity."""
        names = self._net_map()
        return tuple(
            replace(trace, net=names[trace.net]) for trace in self.source.traces()
        )

    def vias(self) -> tuple[NativeVia, ...]:
        """Retain route fences/returns, replacing old standalone corner stitches."""
        names = self._net_map()
        result = tuple(
            replace(via, net=names.get(via.net, via.net))
            for via in self.source.vias()
            if via.role != "stitch"
        )
        center_x = (self.legs[0].points[0][0] + self.legs[-1].points[-1][0]) // 2
        center_y = self.legs[0].points[0][1]
        return result + tuple(
            NativeVia(
                (center_x + side * 5_000_000, center_y + side * 5_000_000),
                "GND",
                role="stitch",
            )
            for side in (-1, 1)
        )

    def keepouts(self) -> tuple[NativeKeepout, ...]:
        """Keep CPWG corridors only; project rules control noncoplanar pours."""
        return self.source.keepouts()

    def specifications(self) -> tuple[Specification, ...]:
        """Match the circuit snapshot by its engineering class across all layers."""
        return (
            Specification(
                f"{self.name}/netclass",
                self.title,
                self.target_ohms,
                self.kind,
                self.net_class,
                layer_settings=_profile_layer_settings(self.signal_profiles),
            ),
        )

    def snapshot(self) -> BoardSnapshot:
        """Provide the circuit's real signal copper and native pair metadata."""
        memberships = tuple((net, (self.net_class,)) for net in self.net_names)
        traces = self.traces()
        return BoardSnapshot(
            COPPER_LAYERS,
            traces,
            sha256(repr((self.name, traces)).encode("utf-8")).hexdigest(),
            net_classes=(self.net_class,),
            net_class_memberships=memberships,
            net_class_context_digest=sha256(
                repr(memberships).encode("utf-8")
            ).hexdigest(),
            differential_pairs=(self.net_names,) if self.paired else (),
        )

    def connector_reference(self, side: int) -> str:
        """Give every physical connector a unique Board Setup-visible reference."""
        return f"J{'USB' if self.paired else 'RF'}{self.number:02d}{'AB'[side]}"


@dataclass(frozen=True)
class CombinedBoard:
    """One shared board carrying independent noncoplanar and CPWG banks."""

    name: str
    title: str
    paired: bool
    target_ohms: str
    circuits: tuple[CombinedCircuit, ...]
    board_bounds: Bounds = COMBINED_BOUNDS

    @property
    def net_classes(self) -> dict[str, tuple[str, ...]]:
        """Return exactly two engineering classes with all their member nets."""
        result: dict[str, tuple[str, ...]] = {}
        for circuit in self.circuits:
            result[circuit.net_class] = (
                result.get(circuit.net_class, ()) + circuit.net_names
            )
        return result

    @property
    def net_names(self) -> tuple[str, ...]:
        """List all independent signal nets, excluding shared GND and unused pins."""
        return tuple(net for circuit in self.circuits for net in circuit.net_names)

    @property
    def signal_profiles(self) -> tuple[RouteLeg, ...]:
        """Enumerate the actual layer/width combinations across both banks."""
        profiles = {
            (profile.layer, profile.width_nm): profile
            for circuit in self.circuits
            for profile in circuit.signal_profiles
        }
        return tuple(
            profiles[key]
            for key in sorted(
                profiles, key=lambda item: (COPPER_LAYERS.index(item[0]), item[1])
            )
        )

    def traces(self) -> tuple[Trace, ...]:
        """Return all actual signal tracks without joining independent circuits."""
        return tuple(trace for circuit in self.circuits for trace in circuit.traces())

    def specifications(self) -> tuple[Specification, ...]:
        """Use two class-wide specifications with explicit per-layer approvals."""
        result = []
        for class_name in self.net_classes:
            members = tuple(
                circuit for circuit in self.circuits if circuit.net_class == class_name
            )
            profiles = tuple(
                profile for circuit in members for profile in circuit.signal_profiles
            )
            result.append(
                Specification(
                    f"{self.name}/{class_name}",
                    class_name,
                    self.target_ohms,
                    members[0].kind,
                    class_name,
                    layer_settings=_profile_layer_settings(profiles),
                )
            )
        return tuple(result)

    def snapshot(self) -> BoardSnapshot:
        """Expose all routes, class memberships and independent native USB mates."""
        classes = self.net_classes
        memberships = tuple(
            (net, (class_name,)) for class_name, nets in classes.items() for net in nets
        )
        return BoardSnapshot(
            COPPER_LAYERS,
            self.traces(),
            self.board_text(),
            net_classes=tuple(classes),
            net_class_memberships=memberships,
            net_class_context_digest=sha256(
                repr(memberships).encode("utf-8")
            ).hexdigest(),
            differential_pairs=tuple(circuit.net_names for circuit in self.circuits)
            if self.paired
            else (),
        )

    def _net_codes(self) -> dict[str, int]:
        """Assign one native net identity to GND and each independent signal."""
        codes = {"": 0, "GND": 1}
        for net in self.net_names:
            codes[net] = len(codes)
        if self.paired:
            for circuit in self.circuits:
                for side in (0, 1):
                    reference = circuit.connector_reference(side)
                    for land in (1, 2):
                        codes[f"{reference}_UNUSED_VBUS_{land}"] = len(codes)
        return codes

    def _footprints(self, circuit: CombinedCircuit, codes: dict[str, int]) -> list[str]:
        """Embed unmodified real SMA/USB land patterns with unique refs and nets."""
        result = []
        usb = circuit.source.usb_launches()
        if usb is not None:
            minus, plus = circuit.net_names
            for side, connector in enumerate(usb.connectors):
                reference = circuit.connector_reference(side)
                nets = {
                    "A6": plus,
                    "B6": plus,
                    "A7": minus,
                    "B7": minus,
                    "A1": "GND",
                    "B1": "GND",
                    "A12": "GND",
                    "B12": "GND",
                    "SH": "GND",
                    **dict.fromkeys(("A4", "B9"), reference + "_UNUSED_VBUS_1"),
                    **dict.fromkeys(("A9", "B4"), reference + "_UNUSED_VBUS_2"),
                }
                x, y = connector.point
                result.append(
                    _footprint(
                        USB,
                        reference,
                        (x / NM_PER_MM, y / NM_PER_MM, connector.rotation),
                        nets,
                        self.name,
                        net_codes=codes,
                        value="USB-C",
                        value_at=(-6.5 if side == 0 else 6.5, 0),
                    )
                )
            return result
        for side, (leg_index, endpoint) in enumerate(
            ((0, 0), (len(circuit.legs) - 1, -1))
        ):
            x, y = circuit.source._member_points(leg_index, 0)[endpoint]
            result.append(
                _footprint(
                    SMA,
                    circuit.connector_reference(side),
                    (x / NM_PER_MM, y / NM_PER_MM, 0),
                    {"1": circuit.net_names[0], "2": "GND"},
                    self.name,
                    net_codes=codes,
                )
            )
        return result

    def _outline(self) -> list[str]:
        """Use one connected perimeter with accessible USB mating-face notches."""
        left, top, right, bottom = self.board_bounds
        points = [(left, top), (right, top)]
        if self.paired:
            for circuit in self.circuits:
                usb = circuit.source.usb_launches()
                if usb is None:
                    continue
                notch_top, notch_bottom = usb.notch_y
                face = usb.mating_edges[1]
                points.extend(
                    (
                        (right, notch_top),
                        (face, notch_top),
                        (face, notch_bottom),
                        (right, notch_bottom),
                    )
                )
        points.extend(((right, bottom), (left, bottom)))
        if self.paired:
            for circuit in reversed(self.circuits):
                usb = circuit.source.usb_launches()
                if usb is None:
                    continue
                notch_top, notch_bottom = usb.notch_y
                face = usb.mating_edges[0]
                points.extend(
                    (
                        (left, notch_bottom),
                        (face, notch_bottom),
                        (face, notch_top),
                        (left, notch_top),
                    )
                )
        return [
            f"(gr_line (start {_mm(start[0])} {_mm(start[1])}) "
            f"(end {_mm(end[0])} {_mm(end[1])}) (stroke (width 0.15) (type default)) "
            f'(layer "Edge.Cuts") (uuid "{_uuid(f"{self.name}/edge/{index}")}"))'
            for index, (start, end) in enumerate(zip(points, (*points[1:], points[0])))
        ]

    def _labels_and_context(self) -> list[str]:
        """Give each bank/circuit location context without obstructing the routes."""
        labels = [
            (self.title, 135, 26, 2.0),
            (
                "NONCOPLANAR / microstrip outside, stripline inside / GND gap = 3x width",
                135,
                36,
                1.15,
            ),
            (
                "CPWG / close same-layer GND / one via-fence row each side",
                135,
                192,
                1.2,
            ),
            ("Illustrative targets only; launches not solver-qualified", 135, 344, 1.1),
        ]
        if self.paired:
            labels.append(
                (
                    "Passive USB-C coupons; CC/VBUS unused; not USB adapters",
                    135,
                    347,
                    0.9,
                )
            )
        for circuit in self.circuits:
            y = circuit.legs[0].points[0][1] / NM_PER_MM
            labels.append((circuit.title, 135, y - 8.5, 1.1))
        items = [
            f'(gr_text {json.dumps(text)} (at {x:g} {y:g}) (layer "F.SilkS") '
            f'(uuid "{_uuid(f"{self.name}/label/{index}")}") '
            f"(effects (font (size {size:g} {size:g}) (thickness 0.15))))"
            for index, (text, x, y, size) in enumerate(labels)
        ]
        for index, (x, y) in enumerate(((28, 28), (242, 28), (28, 342), (242, 342))):
            items.append(
                f'(footprint "RFContext:Mount" (layer "F.Cu") (at {x} {y}) '
                f'(uuid "{_uuid(f"{self.name}/mount/{index}")}") '
                '(pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) '
                '(layers "*.Cu" "*.Mask")))'
            )
        # U3 stays in the inter-bank gap, not beside any controlled trace.
        for reference, x in (("U1", 40), ("U3", 205)):
            pads = " ".join(
                f'(pad "{row * 4 + column + 1}" thru_hole rect '
                f"(at {column * 2.54:.2f} {row * 7.62:.2f}) (size 1.7 2) (drill 0.8) "
                '(layers "*.Cu" "*.Mask") (net 1 "GND"))'
                for row in range(2)
                for column in range(4)
            )
            items.append(
                f'(footprint "RFContext:{reference}" (layer "F.Cu") (at {x} 183) '
                f'(uuid "{_uuid(f"{self.name}/{reference}")}") '
                f'(fp_text reference "{reference}" (at 3.8 -3) (layer "F.SilkS") '
                "(effects (font (size 1.2 1.2) (thickness 0.15)))) "
                "(fp_rect (start -2 -1.5) (end 10 9.2) "
                '(stroke (width 0.2) (type default)) (fill none) (layer "F.SilkS")) '
                f"{pads})"
            )
        return items

    def board_text(self) -> str:
        """Serialize one physical board with every circuit and six shared GND pours."""
        layer_entries = " ".join(
            f'({identifier} "{layer}" {"power" if layer in GROUND_LAYERS else "signal"})'
            for identifier, layer in zip((0, 1, 2, 3, 4, 31), COPPER_LAYERS)
        )
        codes = self._net_codes()
        items = [
            '(kicad_pcb (version 20240108) (generator "jlcpcb_combined_rf_examples")',
            f'(general (thickness {_mm(BOARD_THICKNESS_NM)})) (paper "A2")',
            f'(layers {layer_entries} (35 "F.Paste" user) (36 "B.SilkS" user) '
            '(37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) '
            '(44 "Edge.Cuts" user) (47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user))',
            f"(setup {_stackup()} (pad_to_mask_clearance 0))",
            " ".join(f'(net {code} "{net}")' for net, code in codes.items()),
            *self._outline(),
            *self._labels_and_context(),
        ]
        for circuit in self.circuits:
            items.extend(self._footprints(circuit, codes))
            for trace in circuit.traces():
                start, end = trace.points
                items.append(
                    f"(segment (start {_mm(start[0])} {_mm(start[1])}) "
                    f"(end {_mm(end[0])} {_mm(end[1])}) (width {_mm(trace.width_nm)}) "
                    f'(layer "{trace.layer}") (net {codes[trace.net]}) (uuid "{trace.trace_id}"))'
                )
            for index, via in enumerate(circuit.vias()):
                items.append(
                    f"(via (at {_mm(via.x)} {_mm(via.y)}) "
                    f"(size {_mm(via.diameter_nm)}) (drill {_mm(via.drill_nm)}) "
                    f'(layers "{via.layers[0]}" "{via.layers[-1]}") (net {codes[via.net]}) '
                    f'(uuid "{_uuid(f"{self.name}/{circuit.name}/via/{index}")}"))'
                )
            items.extend(
                _native_keepout(circuit.source, index, keepout)
                for index, keepout in enumerate(circuit.keepouts())
            )
        for layer in COPPER_LAYERS:
            items.append(
                f'(zone (net 1) (net_name "GND") (layer "{layer}") '
                f'(uuid "{_uuid(f"{self.name}/zone/{layer}")}") (name "Shared {layer} GND pour") '
                "(hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.05) "
                "(filled_areas_thickness no) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)) "
                f"{_polygon_text(_rectangle(self.board_bounds))})"
            )
        return "\n".join((*items, ")", ""))


def _combined_board(paired: bool) -> CombinedBoard:
    """Place two seven-circuit banks without changing their controlled spans."""
    name = "usb-differential-90-ohm" if paired else "single-ended-50-ohm"
    circuits = []
    for bank, coplanar in enumerate((False, True)):
        cases = tuple(
            case
            for case in RF_CASES
            if case.paired == paired and case.coplanar == coplanar
        )
        for row, original in enumerate(cases):
            start_x = 135_000_000 - round(original.length_mm * NM_PER_MM / 2)
            start_y = (52 + bank * 160 + row * 20) * NM_PER_MM
            original_x, original_y = original.legs[0].points[0]
            delta = (start_x - original_x, start_y - original_y)
            legs = tuple(
                replace(
                    leg,
                    points=tuple((x + delta[0], y + delta[1]) for x, y in leg.points),
                )
                for leg in original.legs
            )
            source = _ConnectorCase(
                f"{name}/{original.name}", paired, coplanar, legs, original.target_ohms
            )
            circuits.append(CombinedCircuit(original.name, bank * 7 + row + 1, source))
    return CombinedBoard(
        name,
        "USB differential / 90 ohm / 8 mil pair gap"
        if paired
        else "Single-ended RF / 50 ohm",
        paired,
        "90" if paired else "50",
        tuple(circuits),
    )


COMBINED_BOARDS = (_combined_board(False), _combined_board(True))
