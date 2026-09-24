"""Reproducible real-connector examples, separate from tiny geometry stress cases.

Footprint sources are vendored, attributed KiCad library assets. Regeneration
does not consult installed libraries; the openable boards embed all geometry.
Only source templates, not 3D models, are included. Widths and 50/90-ohm targets
are illustrative, not field-solver results or USB compliance claims.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from math import ceil, hypot
from pathlib import Path
import re
from typing import Optional
from uuid import NAMESPACE_URL, uuid5

DIRECTORY = Path(__file__).resolve().parents[1] / "examples/impedance/showcases"
SMA = "Connector_Coaxial:SMA_Amphenol_132134_Vertical"
USB = "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
SOURCES = {
    SMA: "0c39f7b168bd12ea64f5cc3750dbb007a8cff3cbdb4e06524e460a817be4550b",
    USB: "3b8d7da3cae5114ec83022a759a78925113bc2eeec100ea447594f6d8687e4b8",
}
NETS = {"": 0, "GND": 1, "RF_SE": 2, "USB_D-": 3, "USB_D+": 4}
Point = tuple[float, float]


@dataclass(frozen=True)
class ConnectorShowcase:
    """Declarative report intent; captures must parse actual saved native copper."""

    name: str
    title: str
    kind: str
    target_ohms: str
    net_names: tuple[str, ...]
    reference_layers_by_layer: tuple[tuple[str, tuple[str, ...]], ...]
    spacing_nm: Optional[int]
    ground_gap_nm: int

    @property
    def net_class(self) -> str:
        """Expose the exact engineering class declared by the native project."""
        return (
            "USB differential CPWG 90 ohm"
            if self.kind.startswith("differential")
            else "RF single-ended 50 ohm"
        )


CONNECTOR_SHOWCASES = (
    ConnectorShowcase(
        "sma-single-ended-50-ohm",
        "50 ohm SMA-to-SMA connector showcase",
        "single_ended_coplanar",
        "50",
        ("RF_SE",),
        (("F.Cu", ("In1.Cu",)),),
        None,
        200_000,
    ),
    ConnectorShowcase(
        "usb-differential-cpwg-90-ohm",
        "USB-C differential CPWG — 90 ohm, 8 mil pair gap",
        "differential_coplanar",
        "90",
        ("USB_D-", "USB_D+"),
        (("F.Cu", ("In1.Cu",)), ("B.Cu", ("In2.Cu",))),
        203_200,
        200_000,
    ),
)


def _uuid(label: str) -> str:
    """Make every embedded native identity stable and unique per instance."""
    return str(uuid5(NAMESPACE_URL, "jlcpcb-tools/showcase/" + label))


def _parse(source: str) -> list:
    """Preserve exact native quoted strings while parsing a trusted template."""
    stack: list[list] = [[]]
    for token in re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', source):
        if token == "(":
            item: list = []
            stack[-1].append(item)
            stack.append(item)
        elif token == ")":
            stack.pop()
        else:
            stack[-1].append(token)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("Malformed vendored footprint")
    return stack[0][0]


def _serialize(node: list) -> str:
    """Serialize trusted S-expressions without changing literal pad dimensions."""
    return (
        "("
        + " ".join(
            _serialize(item) if isinstance(item, list) else str(item) for item in node
        )
        + ")"
    )


def _children(node: list, name: str) -> list[list]:
    """Return direct source fields without confusing nested geometry fields."""
    return [item for item in node if isinstance(item, list) and item[0] == name]


def _template(identifier: str) -> list:
    """Reject unreviewed source drift rather than silently altering real pads."""
    path = DIRECTORY / "source-footprints" / (identifier.split(":")[1] + ".kicad_mod")
    source = path.read_bytes()
    if sha256(source).hexdigest() != SOURCES[identifier]:
        raise ValueError(f"Vendored footprint checksum mismatch: {identifier}")
    return _parse(source.decode("utf-8"))


def _footprint(
    identifier: str,
    reference: str,
    at: tuple[float, float, int],
    nets: dict[str, str],
    board: str,
    *,
    net_codes: Optional[dict[str, int]] = None,
    value: Optional[str] = None,
    value_at: Optional[Point] = None,
) -> str:
    """Embed actual library geometry, assigning real pin nets and stable UUIDs."""
    codes = NETS if net_codes is None else net_codes
    result = deepcopy(_template(identifier))
    result[1] = json.dumps(identifier)
    result[:] = [
        item
        for item in result
        if not isinstance(item, list)
        or item[0] not in {"version", "generator", "generator_version", "model"}
    ]
    # Native parsers apply placement while reading. Emit it before pad angles;
    # appending it after pads rotates already-global pad angles a second time.
    result[2:2] = [["at", *at], ["uuid", json.dumps(_uuid(board + "/" + reference))]]
    for index, child in enumerate(result):
        if not isinstance(child, list):
            continue
        if child[0] in {
            "property",
            "fp_line",
            "fp_rect",
            "fp_circle",
            "fp_arc",
            "fp_text",
            "pad",
        }:
            child[:] = [
                item
                for item in child
                if not isinstance(item, list) or item[0] != "uuid"
            ]
            child.append(["uuid", json.dumps(_uuid(f"{board}/{reference}/{index}"))])
        if child[0] == "property" and child[1] == '"Reference"':
            child[2] = json.dumps(reference)
        if child[0] == "property" and child[1] == '"Value"':
            if value is not None:
                child[2] = json.dumps(value)
            if value_at is not None:
                position = _children(child, "at")[0]
                position[1:3] = value_at
        if child[0] == "pad":
            number = json.loads(child[1])
            net = nets.get(number, "")
            if net:
                child.append(["net", codes[net], json.dumps(net)])
        if child[0] in {"pad", "property", "fp_text"}:
            # Board files use footprint-relative positions but absolute angles.
            # Rotate text with its land pattern so long connector references
            # retain the library's silkscreen clearance beside exposed pads.
            position = _children(child, "at")[0]
            rotation = float(position[3]) if len(position) > 3 else 0
            position[:] = [*position[:3], f"{(rotation + at[2]) % 360:g}"]
    return _serialize(result)


def _segment(
    start: Point, end: Point, net: str, width: float, label: str, layer: str = "F.Cu"
) -> str:
    """Encode a real copper segment with exact micrometre coordinates."""
    return (
        f"(segment (start {start[0]:.6f} {start[1]:.6f}) (end {end[0]:.6f} {end[1]:.6f}) "
        f'(width {width:.6f}) (layer "{layer}") (net {NETS[net]}) (uuid "{_uuid(label)}"))'
    )


def _offset_path(points: tuple[Point, ...], distance: float) -> tuple[Point, ...]:
    """Offset a 45-degree path with true normal distances and mitered corners."""
    normals = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        length = hypot(bx - ax, by - ay)
        normals.append((-(by - ay) / length, (bx - ax) / length))
    result = []
    for index, (x, y) in enumerate(points):
        if index == 0:
            nx, ny = normals[0]
        elif index == len(points) - 1:
            nx, ny = normals[-1]
        else:
            a, b = normals[index - 1], normals[index]
            scale = 1 + a[0] * b[0] + a[1] * b[1]
            nx, ny = (a[0] + b[0]) / scale, (a[1] + b[1]) / scale
        result.append((x + distance * nx, y + distance * ny))
    return tuple(result)


def _fence(points: tuple[Point, ...], offset: float, label: str) -> list[str]:
    """Use one via row on each side, with at most 0.8 mm along-row pitch."""
    items = []
    for side in (-1, 1):
        path = _offset_path(points, offset * side)
        positions = []
        for start, end in zip(path, path[1:]):
            count = ceil(hypot(end[0] - start[0], end[1] - start[1]) / 0.8)
            positions.extend(
                (
                    start[0] + (end[0] - start[0]) * index / count,
                    start[1] + (end[1] - start[1]) * index / count,
                )
                for index in range(count)
            )
        positions.append(path[-1])
        for index, (x, y) in enumerate(positions):
            items.append(
                f'(via (at {x:.6f} {y:.6f}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "{_uuid(f"{label}/{side}/{index}")}"))'
            )
    return items


def _zone(layer: str, bounds: tuple[float, float, float, float], label: str) -> str:
    """Fill genuine ground on every layer; the inner reference remains intact."""
    left, top, right, bottom = bounds
    return (
        f'(zone (net 1) (net_name "GND") (layer "{layer}") (uuid "{_uuid(label + layer)}") '
        "(hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.1) "
        "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3)) "
        f"(polygon (pts (xy {left} {top}) (xy {right} {top}) (xy {right} {bottom}) (xy {left} {bottom}))))"
    )


def _usb_c_copper(name: str) -> list[str]:
    """Serialize paired layer transitions and the local reversible-contact bridge."""
    from tests.rf_usb_c_geometry import (
        B_COUPLED_END,
        B_COUPLED_START,
        F_COUPLED_END,
        F_COUPLED_START,
        WIDTH_NM,
        usb_routes,
        usb_vias,
    )

    result = []
    for route_index, route in enumerate(usb_routes()):
        result.extend(
            _segment(
                start,
                end,
                route.net,
                WIDTH_NM / 1_000_000,
                f"{name}/{route.net}/{route.layer}/{route_index}/{piece}",
                route.layer,
            )
            for piece, (start, end) in enumerate(zip(route.points, route.points[1:]))
        )
    for index, via in enumerate(usb_vias()):
        x, y = via.point
        result.append(
            f"(via (at {x:.6f} {y:.6f}) (size 0.6) (drill 0.3) "
            f'(layers "F.Cu" "B.Cu") (net {NETS[via.net]}) '
            f'(uuid "{_uuid(f"{name}/transition-via/{index}")}"))'
        )
    for layer, endpoints in (
        ("F.Cu", (F_COUPLED_START, F_COUPLED_END)),
        ("B.Cu", (B_COUPLED_START, B_COUPLED_END)),
    ):
        result.extend(_fence(endpoints, 1.1, name + "/" + layer + "/fence"))
    return result


def _board(name: str, differential: bool) -> str:
    """Make a long connector-to-connector routing coupon with native context."""
    # Align the horizontal USB connector's actual PCB-edge datum with Edge.Cuts.
    left = 31.325 if differential else 20
    right = 228.675 if differential else 240
    net_codes = {
        **NETS,
        **(
            {
                f"J{connector}_UNUSED_VBUS_{land}": 5 + (connector - 1) * 2 + land - 1
                for connector in (1, 2)
                for land in (1, 2)
            }
            if differential
            else {}
        ),
    }
    items = [
        '(kicad_pcb (version 20240108) (generator "jlcpcb_connector_showcase")',
        '(general (thickness 1.6)) (paper "A3")',
        '(layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal) '
        '(35 "F.Paste" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) (40 "Dwgs.User" user) '
        '(44 "Edge.Cuts" user) (47 "F.CrtYd" user) (49 "F.Fab" user))',
        '(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) '
        '(layer "dielectric 1" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4) (loss_tangent 0.02)) '
        '(layer "In1.Cu" (type "copper") (thickness 0.035)) '
        '(layer "dielectric 2" (type "core") (thickness 1.06) (material "FR4") (epsilon_r 4) (loss_tangent 0.02)) '
        '(layer "In2.Cu" (type "copper") (thickness 0.035)) '
        '(layer "dielectric 3" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4) (loss_tangent 0.02)) '
        '(layer "B.Cu" (type "copper") (thickness 0.035))) (pad_to_mask_clearance 0))',
        " ".join(f'(net {code} "{net}")' for net, code in net_codes.items()),
        f'(gr_rect (start {left} 30) (end {right} 100) (stroke (width 0.15) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{_uuid(name + "/edge")}"))',
    ]
    if differential:
        pin_nets = {
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
        for reference, placement in (("J1", (35, 65, 270)), ("J2", (225, 65, 90))):
            # KiCad treats overlapping unassigned pads as different nets. Each
            # physical VBUS land gets its own explicitly unused, isolated net.
            assigned = {
                **pin_nets,
                **dict.fromkeys(("A4", "B9"), reference + "_UNUSED_VBUS_1"),
                **dict.fromkeys(("A9", "B4"), reference + "_UNUSED_VBUS_2"),
            }
            items.append(
                _footprint(
                    USB,
                    reference,
                    placement,
                    assigned,
                    name,
                    net_codes=net_codes,
                    value="USB-C",
                    value_at=(-6.5 if reference == "J1" else 6.5, 0),
                )
            )
        items.extend(_usb_c_copper(name))
        title = "USB differential CPWG | 90 ohm target | 8 mil pair gap"
        note = "Passive coupon only; VBUS, CC and SBU unused; NOT a USB adapter"
    else:
        items.extend(
            _footprint(SMA, ref, (x, 75, 0), {"1": "RF_SE", "2": "GND"}, name)
            for ref, x in (("J1", 35), ("J2", 225))
        )
        points = ((35, 75), (70, 75), (90, 55), (160, 55), (180, 75), (225, 75))
        items.extend(
            _segment(a, b, "RF_SE", 0.3, f"{name}/RF_SE/{index}")
            for index, (a, b) in enumerate(zip(points, points[1:]))
        )
        items.extend(_fence(((41, 75), *points[1:-1], (219, 75)), 1.0, name + "/fence"))
        title = "50 ohm target | SMA pin 1 signal / pin 2 ground"
        note = (
            "Nominal CPWG geometry; connector launches are not field-solver validated"
        )
    for index, (text, y) in enumerate(
        (
            (title, 87),
            (note, 90),
            (
                "F.Cu/In1.Cu and B.Cu/In2.Cu | one via-fence row per side"
                if differential
                else "In1.Cu GND reference | one stitching-via row per side",
                93,
            ),
        )
    ):
        items.append(
            f'(gr_text {json.dumps(text)} (at 130 {y}) (layer "F.SilkS") (uuid "{_uuid(f"{name}/label/{index}")}") (effects (font (size 1 1) (thickness 0.15))))'
        )
    items.extend(
        _zone(layer, (left + 0.5, 30.5, right - 0.5, 99.5), name)
        for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    )
    return "\n".join((*items, ")", ""))


def _project(name: str, differential: bool) -> str:
    """Expose engineering intent as a genuine native Board Setup net class."""
    class_name = (
        "USB differential CPWG 90 ohm" if differential else "RF single-ended 50 ohm"
    )
    default = {
        "name": "Default",
        "priority": 2147483647,
        "clearance": 0.2,
        "track_width": 0.25,
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "diff_pair_width": 0.27,
        "diff_pair_gap": 0.3,
        "diff_pair_via_gap": 0.3,
        "tuning_profile": "",
    }
    project = {
        "meta": {"filename": name + ".kicad_pro", "version": 1},
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.2,
                    "min_track_width": 0.1,
                    "min_through_hole_diameter": 0.3,
                    "min_via_diameter": 0.6,
                    "min_via_annular_width": 0.15,
                    "min_hole_to_hole": 0.25,
                    "min_copper_edge_clearance": 0.25,
                },
                "drc_exclusions": [],
            }
        },
        "net_settings": {
            "meta": {"version": 5},
            "classes": [
                default,
                {
                    **default,
                    "name": class_name,
                    "priority": 0,
                    "track_width": 0.27 if differential else 0.3,
                    "diff_pair_gap": 0.2032
                    if differential
                    else default["diff_pair_gap"],
                },
            ],
            "netclass_assignments": {},
            "netclass_patterns": [
                {"pattern": net, "netclass": class_name}
                for net in (("USB_D-", "USB_D+") if differential else ("RF_SE",))
            ],
        },
    }
    if differential:
        # The unmodified GCT land pattern has ~0.1944 mm NPTH-to-copper spacing.
        # Retain ordinary 0.2 mm copper / 0.25 mm hole-to-hole constraints.
        project["board"]["design_settings"]["rules"]["min_hole_clearance"] = 0.15
    return json.dumps(project, indent=2) + "\n"


def showcase_files() -> dict[str, str]:
    """Return reproducible artifacts relative to examples/impedance/showcases."""
    result = {}
    for identifier in SOURCES:
        name = identifier.split(":")[1] + ".kicad_mod"
        result["source-footprints/" + name] = (
            DIRECTORY / "source-footprints" / name
        ).read_text(encoding="utf-8")
    result["source-footprints/LICENSE.md"] = (
        DIRECTORY / "source-footprints/LICENSE.md"
    ).read_text(encoding="utf-8")
    for name, differential in (
        ("sma-single-ended-50-ohm", False),
        ("usb-differential-cpwg-90-ohm", True),
    ):
        result[name + ".kicad_pcb"] = _board(name, differential)
        result[name + ".kicad_pro"] = _project(name, differential)
    result["README.md"] = """# Real connector capture showcases

Open either `.kicad_pcb` with its adjacent `.kicad_pro` and press **B** to fill
zones. Footprints are embedded, so opening and regeneration do not require an
installed library. In Board Setup, the named net class selects the signal(s).

- [50 ohm SMA](sma-single-ended-50-ohm.kicad_pcb): two real Amphenol 132134 SMA
  connectors; pin 1 signal, all four pin 2 pads GND; a >120 mm route with 45-degree bends.
- [90 ohm USB-C differential CPWG](usb-differential-cpwg-90-ohm.kicad_pcb): two real GCT USB4105
  USB-C receptacles, with both physical mating faces aligned to opposite PCB
  edges. A6/B6 are D+ and A7/B7 are D- at **both** connectors. All GND and shell
  pads are grounded. VBUS, CC and SBU are unused: coincident VBUS pad labels use
  isolated per-land net names, with no power route between lands or connectors;
  CC/SBU remain unassigned. Nothing unused is connected to GND.

Both use a four-layer board with an In1.Cu ground reference 0.2 mm below F.Cu.
The USB differential CPWG has nearby ground pour on each signal layer: F.Cu
references In1.Cu and the short B.Cu section references In2.Cu. **Both** members
transition to B.Cu and back together using aligned, closely spaced via pairs
and symmetric 45-degree fanouts. The long pair never swaps order or splits apart.
A compact connector-local D+ bridge on B.Cu connects the right receptacle's
reversible contacts; it is separate from the coupled transmission-line trunk.
Paired report rows highlight both members together; the local contact bridge is
not a uniform controlled-impedance segment.
The long F.Cu and B.Cu coupled windows have one via-fence row per side at
<=0.8 mm pitch; connector launches and via fanouts are excluded. Symmetric
nearby GND returns accompany the paired signal transitions.

USB copper width is 0.27 mm throughout, and uniform coupled edge spacing is
**8 mil = 0.2032 mm** on both F.Cu and B.Cu. Short 45-degree connector and via
fanouts deliberately widen the pair and are not uniform impedance cross-sections.
This is CPWG, not a non-coplanar microstrip example. Surface routing with the
same-layer pour pulled back is microstrip; stripline describes buried routing
between reference planes. For non-coplanar examples, a same-layer ground
clearance of at least three trace widths is a starting geometry constraint,
not a guarantee of negligible coupling or a solved impedance.
The SMA nominal width/ground gap remain 0.30/0.20 mm. These values and 50/90-ohm
targets are illustrative: no field solver has certified impedance, connector
launches, stackup, delay/skew, or USB compliance. With CC and VBUS unused this is
**not a functional USB adapter**, a complete USB product, or a manufacturing-ready
reference design.

The unmodified GCT footprint's nominal NPTH-to-copper spacing is about 0.1944 mm.
Only the USB project explicitly sets a 0.15 mm hole-to-copper minimum; ordinary
copper clearance remains 0.2 mm and hole-to-hole minimum remains 0.25 mm. Native
DRC checks remain enabled, without exclusions. Fabrication tolerances and the
actual manufacturer's capability still require engineering review.

Pinout and edge datum: [GCT manufacturer drawing](https://gct.co/files/drawings/usb4105.pdf).
The short A6/B6 and A7/B7 bridges follow the connector-local arrangement described
by [USB-IF Type-C R2.0, Table 3-5 notes](https://www.usb.org/sites/default/files/USB%20Type-C%20Spec%20R2.0%20-%20August%202019.pdf);
this source reference is not a claim of compliance with a current USB standard.

## Footprint provenance

Copyright KiCad library contributors; sources are copied unchanged from the
installed KiCad 10 footprint library (native source format version **20260206**).
`source-footprints/` contains the exact source `.kicad_mod` files; their 3D model
references are removed when embedding them in generated boards. No 3D assets are
redistributed. Instance changes: reference, placement, UUIDs, and net assignment;
USB instances also use a concise "USB-C" Value placed clear of the signal route.
Checksums are pinned in `tests/rf_impedance_showcases.py`.

Library sources are licensed **CC-BY-SA-4.0** with the KiCad electronic-design
exception. See [KiCad library license](https://www.kicad.org/libraries/license/)
and [CC BY-SA 4.0 legal code](https://creativecommons.org/licenses/by-sa/4.0/legalcode).
The exception permits these electronic designs and generated board files to use
library geometry without imposing that license on the entire design. The
vendored source footprints retain the library license and attribution.

Upstream sources:

- [SMA](https://gitlab.com/kicad/libraries/kicad-footprints/-/blob/master/Connector_Coaxial.pretty/SMA_Amphenol_132134_Vertical.kicad_mod)
- [USB-C](https://gitlab.com/kicad/libraries/kicad-footprints/-/blob/master/Connector_USB.pretty/USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.kicad_mod)

Regenerate through `scripts/generate_rf_impedance_fixtures.py`; these two larger
connector examples are separate from the 2 mm geometry regression matrix.
"""
    return result
