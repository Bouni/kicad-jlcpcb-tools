# RF impedance review boards

Start with the [openable board index](../examples/impedance/README.md). The human
review examples are consolidated into **two KiCad projects**, not a directory of
one-board-per-case variants:

| Project | Connector type | Circuits on the board |
| --- | --- | --- |
| `single-ended-50-ohm` | SMA at both ends of every circuit | 7 noncoplanar and 7 CPWG |
| `usb-differential-90-ohm` | USB-C at both ends of every circuit | 7 noncoplanar and 7 CPWG |

Each board has two labelled banks, with distinct net classes and unique nets for
its independent circuits. This permits comparing short, long, and layer-changing
routes without opening a separate PCB for each case. Both projects contain
embedded footprints, real copper tracks, signal and ground vias, GND zones on
every copper layer, and native clearance rules or CPWG fill-only keepouts.
They are repository example assets, not per-board plugin configuration files.
Production configuration remains in the board-scoped `project.db`.

The **2 mm cases describe controlled cores plus connector launches**. Complete
connected nets are longer than 2 mm because the SMA/USB-C bodies and launch
copper need physical space. USB launches use paired layer-changing vias and
45-degree fanouts; compact local bridges join reversible D+/D− contacts. Each
pair is kept together through the main route and transitions. These passive
coupons are not functional USB adapters: CC and VBUS are intentionally unused.
Connector launch discontinuities are illustrative, not impedance-qualified.

Deterministic tests check the consolidated source geometry, class assignments,
route coverage, connector launches, and SMA fence copper/hole clearance. These
checks do not validate native zone filling, DRC, connector access, or rendered
appearance. Native KiCad checks and manual inspection remain necessary.

Keep each project's `.kicad_pcb`, `.kicad_pro`, and `.kicad_dru` together when
copying or reopening it. Native project constraints avoid depending on a user's
broader default minimum widths; custom rules distinguish the noncoplanar bank
from the nearby-ground CPWG bank. Local editor state and backups are ignored by
Git. Embedded footprints make the projects directly openable; reusable source
footprint assets remain only under
`examples/impedance/showcases/source-footprints/`.

The fixture generator's `--check` compares file bytes. The two committed projects
contain KiCad-expanded defaults, so it currently reports their `.kicad_pro` files
as different even though the project-intent regression checks pass. This is a
remaining reproducibility-tooling limitation, not a copper or net-class mismatch.
The saved project settings are retained; generate into a separate directory when
an exact generator-byte comparison is needed. Do not overwrite an edited project
merely to silence this check.

The native projects define separate circuit/family classes in
**Board Setup → Design Rules → Net Classes**. Explicit, exact-name assignment
patterns put each single-ended net, or both members of a USB pair, into its
corresponding class; GND and unrelated context nets stay in `Default`.
A class's nominal routing width and pair gap describe its first signal profile.
Transition circuits deliberately retain different actual widths on inner layers,
so matching must follow class membership rather than a class's default width.
These names express requested impedance intent, not a native impedance
calculation: no tuning profile is attached. The JSON representation follows
[KiCad 10's native net-settings schema](https://gitlab.com/kicad/code/kicad/-/blob/10.0.6/common/project/net_settings.cpp).

## Purpose and limits

These examples exercise identification, whole-route capture framing, layer
changes, differential grouping, and JLCPCB report fields using physically
meaningful RF layout geometry. Their impedance targets are **intent**, not
independently solved impedance values. They are not release-ready RF coupons or
a JLCPCB-approved stackup. Manufacturing use needs the actual fabricator's
stackup, material properties, copper/mask model, field-solver results, and
transition analysis.

The requested targets are **50 Ω single-ended** and **90 Ω differential** on
every layer, including width-changing transitions. The differential target
follows the requested USB intent; these examples are not USB-compliance
certification. Updating a target does not retune the physical widths, gaps,
stackup, or via geometry.

Surface noncoplanar routes are **microstrip**, not stripline. Buried noncoplanar
routes are **stripline** between two reference planes. CPWG cases add same-layer
ground beside the signal or pair; the inner-layer version is buried coplanar
routing with reference planes above and below. This terminology follows
[Analog Devices' RF layout guidance](https://www.analog.com/en/resources/technical-articles/pcbs-layout-guidelines-for-rf--mixedsignal.html).

## Cases and expected output

The same seven-case bank appears twice on each board: once noncoplanar and once
CPWG. The two boards retain all 28 circuit cases in a compact review workflow.

| Family | Controlled-core layers | Core lengths | Independent circuits |
| --- | --- | --- | ---: |
| Single-ended noncoplanar | Top, inner, bottom | 2 mm and 120 mm | 6 |
| Single-ended CPWG | Top, inner, bottom | 2 mm and 120 mm | 6 |
| USB differential noncoplanar | Top, inner, bottom | 2 mm and 120 mm | 6 |
| USB differential CPWG | Top, inner, bottom | 2 mm and 120 mm | 6 |
| Single-ended transitions, both families | Top → inner → bottom | 150 mm nominal | 2 |
| USB differential transitions, both families | Top → inner → bottom | 150 mm nominal | 2 |
| **Total across two boards** | | | **28** |

Corresponding differential members are grouped automatically, with one yellow
highlight box around both. Distinct circuits remain distinct rows; the banks
are not combined into one oversized highlight. Rows follow actual layer/width
profiles, including connector launches, rather than counting only the nominal
controlled legs. Inner and bottom cores need connector launch copper on other
layers. These added profiles must remain visible in matching and review;
reference settings and 3× rules follow their actual layer and width.

Each nominal transition core leg spans 50 mm, and each long core spans 120 mm.
Complete net lengths include launch copper; a 2 mm core must not be mistaken
for a complete 2 mm connector-to-connector route. No launch copper is hidden to
reduce the row count. Net-class mode keeps engineering intent across all layers.

Isolated fixture source modules remain available for on-demand regression work;
they are not additional human-review projects in `examples/impedance/`.
Earlier geometry stress cases retain vertical, diagonal, and meandering routes
up to 240 mm. Their right-angle meander is an intentional geometry stress case,
not recommended RF routing. The review circuits use straight controlled runs
and 45-degree pair fanouts.

## Declared six-layer stack

The example stack uses nominal Dk = 4.0 and 35 µm copper on all six layers.
The copper plus dielectric thickness is 1.6 mm before soldermask. From top to
bottom, dielectric thicknesses are 200, 180, 180, 630, and 200 µm.

| Physical layer | KiCad layer | Role | Report reference field |
| --- | --- | --- | --- |
| L1 | F.Cu | Top signal and GND pour | L2 |
| L2 | In1.Cu | GND reference plane | — |
| L3 | In2.Cu | Inner signal and GND pour | **L2, L4** |
| L4 | In3.Cu | GND reference plane | — |
| L5 | In4.Cu | GND reference plane | — |
| L6 | B.Cu | Bottom signal and GND pour | L5 |

These references are explicit fixture expectations. Automatic adjacent defaults
and manual overrides still require engineering review: the matcher does not
prove that a chosen reference layer is an electrically continuous ground plane.

## Nominal trace profiles

All dimensions below are in millimetres. Differential spacing is **edge to
edge**, distinct from the outer signal-to-ground gap, and is exactly **8 mil
(0.2032 mm)** on every controlled route layer.

| Profile | Outer width | Inner width | Outer pair gap | Inner pair gap | Coplanar ground gap |
| --- | ---: | ---: | ---: | ---: | ---: |
| Single-ended noncoplanar, 50 Ω target | 0.350 | 0.130 | — | — | — |
| Differential noncoplanar, 90 Ω target | 0.300 | 0.110 | 0.2032 | 0.2032 | — |
| Single-ended CPWG, 50 Ω target | 0.300 | 0.110 | — | — | 0.200 |
| Differential CPWG, 90 Ω target | 0.270 | 0.100 | 0.2032 | 0.2032 | 0.200 |

These deliberately distinguish widths used by the matcher and exercise narrow
inner routing. They are **not a field-solver calculation** of 50/90 Ω. Exact
spacing applies along uniform runs; via launches/fanouts are separately
represented discontinuities, not constant-impedance cross-sections.

## Ground geometry and the 2 GHz assumption

CPWG fixtures use GND through-vias with 0.3 mm drills and 0.6 mm pads. One row
on each side follows the nominal controlled span on a 0.8 mm longitudinal grid.
The actual adjacent via-centre spacing at layer-change fanouts needs inspection
on the consolidated boards. Connector fanouts outside the controlled span are
unqualified launch regions, not fully fenced CPWG cross-sections. Fence ends
are offset where needed to clear diagonal approaches. Ground zones connect
solidly to ground vias instead of using thermal spokes. Via pad edges are
intended to sit at least 0.15 mm inside the coplanar ground beyond the nominal
signal-to-ground gap. The fill exclusion corridor encloses the whole
differential pair, including its inter-trace space: a thin ground sliver must
not appear between P and N.

The pitch choice is informed by an
[Analog Devices engineer's via-spacing discussion](https://ez.analog.com/webinar/f/questions/538887/in-coplanar-waveguide-what-is-the-best-via-spacing-to-use).
For a 2 GHz highest frequency and Dk = 4, the bulk-dielectric wavelength estimate
is `c / (f × sqrt(Dk)) ≈ 74.95 mm`; one eightieth is approximately 0.937 mm.
The chosen 0.8 mm pitch is below that reference value. This is a documented
fixture assumption, not a universal design rule or measured isolation result.
The earlier examples copied that discussion's two-staggered-row arrangement;
the simpler one-row-per-side examples do not claim equivalent isolation.
Different materials, harmonics, or faster signal edges require revisiting it.

Every copper layer has a GND zone, including otherwise unused signal layers.
Noncoplanar microstrip and stripline circuits use native custom clearance rules
to keep **same-layer GND fill at least three trace widths from the copper edge**.
The `.kicad_dru` file scopes each rule to the actual noncoplanar net class and
signal layer; distinct CPWG classes must not inherit these enlarged gaps:

| Noncoplanar profile | Signal layer | Trace width | Minimum track-to-GND-zone gap |
| --- | --- | ---: | ---: |
| Single-ended 50 Ω | F.Cu / B.Cu | 0.350 mm | 1.050 mm |
| Single-ended 50 Ω | In2.Cu | 0.130 mm | 0.390 mm |
| USB differential 90 Ω | F.Cu / B.Cu | 0.300 mm | 0.900 mm |
| USB differential 90 Ω | In2.Cu | 0.110 mm | 0.330 mm |

The rules match tracks in the named class against GND zones on the explicitly
named signal layer. They do not enlarge clearance between differential members,
apply to connector pads or vias, or slot the reference planes. Clearance follows
each trace, including its fanout. CPWG retains its separate gap corridor and
fences. KiCad refill may produce larger gaps because of polygon resolution or
other constraints.

Three widths is the requested example-layout policy, not a field-solver result
or a guarantee of negligible lateral coupling. Reference planes above/below
should remain intact apart from normal antipads around non-ground pads and vias.

At layer transitions the differential fanouts are intended to meet identical
P/N via centers on both participating layers, preserve symmetry, and change to
the appropriate layer-dependent widths. Connector escapes can introduce length
differences; matched electrical delay has not been verified. Nearby symmetric
return vias connect the reference planes. The examples use through signal vias,
so unused barrel sections can create stubs. Connector launches, antipads, stubs,
and return loss have not been optimized or electromagnetically qualified.

## Rendering and manual inspection

Previews and report images show only the selected signal layer's copper, using
the selected KiCad palette. Inactive copper is hidden; non-copper component,
silkscreen and board-outline context uses the saved dimming amount. Pads and vias
are taken only from the selected layer's native plot, including through-hole
items that physically exist on that layer. Active zones use KiCad's default 0.6
zone opacity. The bright yellow box remains opaque. The user's
live editor mode and preferences are not changed. Saved palette/dimming changes
invalidate cached specification previews and require included layers to be
reviewed again.

This off-screen SVG composition uses KiCad's color-dimming math for non-copper
context, but is not a pixel-identical GAL canvas screenshot. Native plot geometry
lacks GAL's item-specific pad/via flashing metadata; broad fill polygons use a
zone-opacity heuristic rather than the live editor's per-object opacity sliders.
The revised contrast output and consolidated geometry have not been exercised
by an automated or native validation run.

Open a board directly in KiCad, then use **Fill All Zones** (normally **B**).
Canonical source files omit generated fill caches to keep them reviewable and
deterministic. Inspect one signal layer at a time with **Inactive Layer View
Mode → Dim**. Board labels, component outlines, pads, and ordinary routing
provide context. No footprint download or schematic is needed to open the files.

## Regeneration and review captures

To regenerate the two review projects:

```sh
myenv/bin/python scripts/generate_rf_impedance_fixtures.py
```

`--output` selects a separate directory. The manifest records example geometry
and board checksums, not saved plugin settings.

For a **review-only gallery without tests, automatic refill, or DRC**, first fill
and save temporary copies of the projects in KiCad. Point `--examples` at the
directory containing those filled copies, then select a board:

```sh
myenv/bin/python scripts/generate_rf_impedance_captures.py \
  --examples /tmp/rf-filled-review \
  --output /tmp/rf-review-captures \
  --kicad-cli /path/to/kicad-cli \
  --use-existing-fills \
  --board usb-differential-90-ohm
```

Use `--board single-ended-50-ohm` for the SMA board, or omit `--board` to capture
both projects. This command renders the supplied fills; it does not establish
that their clearances, connectivity, reference continuity, or impedance are
correct. Existing unsaved work should not be replaced to prepare a review copy.

For later isolated-fixture work, generate the legacy matrix **outside** the human
example folder:

```sh
myenv/bin/python scripts/generate_rf_impedance_fixtures.py \
  --legacy-matrix --output /tmp/rf-isolated-fixtures
```

Capture selectors `--case` and `--showcase` remain available for that explicitly
generated legacy directory. They are not needed for the two review projects.
The isolated fixture modules retain their regression scenarios without
proliferating standalone boards in the normal example folder.

Validation must exercise the consolidated boards themselves;
earlier isolated-fixture results are not evidence for this geometry. Native CLI
capture and live-editor dialog/snapshot/`pcbnew.PLOT_CONTROLLER` acceptance are
also distinct workflows.

The production renderer uses a detached copy of current native fill caches: use
**B** before scanning/previewing. It does not refill using guessed project rules
or certify electrical impedance.
