"""Materialize openable KiCad RF fixtures and their inspection index.

The default output is two combined human-facing review projects. Isolated matrix
cases remain opt-in with --legacy-matrix and a separate --output directory. This
generates example data, not production configuration: real board impedance
settings remain in project.db. Source generation does not require KiCad.
"""

import argparse
import hashlib
import json
from math import hypot
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Optional, Union

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impedance.matching import _components, analyze  # noqa: E402
from impedance.model import Config  # noqa: E402
from tests.rf_impedance_combined import (  # noqa: E402
    COMBINED_BOARDS,
    CombinedBoard,
    CombinedCircuit,
)
from tests.rf_impedance_fixtures import (  # noqa: E402
    NONCOPLANAR_CLEARANCE_FACTOR,
    RF_CASES,
    RFCase,
    RouteLeg,
)
from tests.rf_impedance_showcases import showcase_files  # noqa: E402


def profile_record(
    case: Union[RFCase, CombinedCircuit], leg: RouteLeg
) -> dict[str, object]:
    """Describe one physical copper profile independently of nominal core length."""
    return {
        "signal_layer": leg.layer,
        "reference_layers": list(leg.reference_layers),
        "width_mm": leg.width_nm / 1_000_000,
        "noncoplanar_ground_clearance_mm": (
            None
            if case.coplanar
            else NONCOPLANAR_CLEARANCE_FACTOR * leg.width_nm / 1_000_000
        ),
        "pair_edge_gap_mm": (
            leg.spacing_nm / 1_000_000 if leg.spacing_nm is not None else None
        ),
        "coplanar_ground_gap_mm": (
            leg.ground_gap_nm / 1_000_000 if leg.ground_gap_nm is not None else None
        ),
    }


def case_record(case: Union[RFCase, CombinedCircuit], source: str) -> dict[str, object]:
    """Record physical layer dimensions and expected report rows for inspection."""
    traces = case.traces()
    profiles = case.signal_profiles
    analysis = analyze(
        Config(enabled=True, specifications=case.specifications()), case.snapshot()
    )
    components = sum(
        len(
            _components(
                tuple(
                    trace
                    for trace in traces
                    if trace.layer == profile.layer
                    and trace.width_nm == profile.width_nm
                    and trace.net == net
                )
            )
        )
        for profile in profiles
        for net in case.net_names
    )
    return {
        "name": case.name,
        "board": f"{case.name}.kicad_pcb",
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "nominal_route_length_mm_per_net": case.length_mm,
        "actual_route_length_mm_per_net": {
            net: sum(
                hypot(
                    trace.points[-1][0] - trace.points[0][0],
                    trace.points[-1][1] - trace.points[0][1],
                )
                for trace in traces
                if trace.net == net
            )
            / 1_000_000
            for net in case.net_names
        },
        "launch_type": case.launch_type,
        "launch_count": case.launch_count,
        "length_description": (
            f"{case.length_mm:g} mm controlled core + USB launches"
            if case.paired
            else f"{case.length_mm:g} mm nominal route"
        ),
        "target_ohms": case.target_ohms,
        "net_class": case.net_class,
        "custom_rules": None if case.coplanar else f"{case.name}.kicad_dru",
        "net_names": list(case.net_names),
        "differential": case.paired,
        "coplanar": case.coplanar,
        "expected_raw_sections": len(analysis.sections),
        "expected_electrical_components": components,
        "expected_reviewed_rows": len(analysis.sections),
        "legs": [profile_record(case, profile) for profile in profiles],
        "controlled_core_legs": [
            {
                **profile_record(case, leg),
                "length_mm": leg.length_mm,
            }
            for leg in case.legs
        ],
    }


def inspection_index(records: list[dict[str, object]]) -> str:
    """Make every native board directly discoverable without running Python."""
    lines = [
        "# Openable RF impedance test boards",
        "",
        "These are standalone KiCad `.kicad_pcb` files, not screenshots or Python-only",
        "mocks. Open a file in PCB Editor, fill its copper zones, then inspect the",
        "signal and reference layers. All footprints are embedded; no external",
        "footprint libraries, schematic, or generated project database is required.",
        "Each board also has a small `.kicad_pro` with its explicit fixture DRC",
        "constraints, so narrow inner traces use the intended 0.1 mm minimum.",
        "Noncoplanar boards also have a `.kicad_dru` defining same-layer",
        "track-to-GND-zone clearance of **3 × the actual trace width**. Keep this",
        "file beside the PCB and project when copying or reopening a board.",
        "",
        "The source boards deliberately omit generated zone-fill caches. Use **Fill",
        "All Zones** (normally **B**) after opening to see the actual ground pours.",
        "Native integration tests refill temporary copies with KiCad before checking",
        "them. Saving a board after filling is fine for local inspection; regenerate",
        "the fixtures to restore the canonical test version.",
        "",
        "Select `F.Cu` (top), `In2.Cu` (inner), or `B.Cu` (bottom) in the layer panel.",
        "The ground-reference planes are `In1.Cu`, `In3.Cu`, and `In4.Cu`.",
        "All six copper layers have GND pours, including unused signal layers.",
        "Inspect a signal layer on its own when filled reference planes obscure it.",
        "The RF route uses named `RF_SE` or `USB_D+` / `USB_D-` nets; labels,",
        "components and ordinary surrounding routing provide location context.",
        "In **Board Setup → Design Rules → Net Classes**, inspect the exact RF",
        "net assignments to `RF single-ended 50 ohm` or `USB differential 90 ohm`.",
        "Ground and surrounding context nets remain in `Default`. These names",
        "declare intent; no solved-impedance tuning profile is attached. A class's",
        "nominal routing width/gap describes its first route leg, while actual",
        "copper widths and reference layers vary across the transition fixtures.",
        "Custom rules are scoped by signal layer and net class: single-ended",
        "outer/inner gaps are 1.05/0.39 mm; differential gaps are 0.90/0.33 mm.",
        "They apply to tracks against GND zones, not pads, vias, or reference",
        "planes. CPWG corridors and their close ground gaps remain unchanged.",
        "",
        "**Not manufacturing-ready impedance coupons:** 50 ohms single-ended and",
        "90 ohms differential are requested targets, not solver-verified results.",
        "The differential target follows the requested USB intent; this is",
        "not USB-compliance certification. Surface noncoplanar cases are",
        "microstrip; buried noncoplanar cases are stripline. Dimensions, stackup,",
        "2 GHz fence assumptions, transition limitations, and test coverage are in",
        "[the fixture design notes](../../docs/rf-impedance-fixtures.md).",
        "",
        "The 28 boards cover 24 short/long layer cases and four three-layer",
        f"transitions. Their actual copper produces {sum(record['expected_reviewed_rows'] for record in records)} scan rows:",
        "each differential pair is grouped automatically, with one yellow box.",
        "Each physical signal profile gets its own row, including USB launches.",
        "",
        "All 14 differential boards use two real USB-C receptacles and connected",
        "paired launches. Even the 2 mm cases include USB-C: their length means",
        "a **2 mm controlled core + USB launches**, not a 2 mm full electrical net.",
        "Single-ended long/transition examples retain SMA connectors; only their",
        "2 mm stress cases retain compact testpoints. Differential",
        "controlled-run spacing is exactly **8 mil (0.2032 mm)** on every layer.",
        "Connector launches are illustrative discontinuities, not solved impedance.",
        "The embedded SMA and USB-C geometry reuses pinned KiCad library sources",
        "under [source-footprints](showcases/source-footprints/), with its",
        "[library license and attribution](showcases/source-footprints/LICENSE.md).",
        "",
        "For recognizable, connected real components, also open the separate",
        "[50 Ω SMA and 90 Ω USB connector showcases](showcases/README.md).",
        "These supplement the short-route matrix; their connector launches are",
        "illustrative, not impedance-qualified or USB-compliance designs.",
        "",
        "| Open in KiCad | Controlled core length | Actual signal layers | Target | Rows |",
        "| --- | ---: | --- | ---: | ---: |",
    ]
    for record in records:
        layers = " → ".join(leg["signal_layer"] for leg in record["legs"])
        lines.append(
            f"| [{record['name']}]({record['board']}) | "
            f"{record['nominal_route_length_mm_per_net']:g} mm | {layers} | "
            f"{record['target_ohms']} Ω | {record['expected_reviewed_rows']} |"
        )
    lines.extend(
        [
            "",
            "`manifest.json` lists exact layer-dependent widths, pair/ground gaps,",
            "reference layers, net classes/members, custom-rule files and clearances,",
            "row counts, and board SHA-256 checksums.",
            "Controlled core legs and actual per-net lengths including connector fanouts",
            "are recorded separately, along with connector type and count.",
            "It is test-fixture metadata, not the plugin's settings store.",
            "",
            "From the repository root, reproduce or check these files with:",
            "",
            "```sh",
            "myenv/bin/python scripts/generate_rf_impedance_fixtures.py --legacy-matrix --output /tmp/isolated-rf-examples",
            "```",
            "",
            "Regeneration overwrites only this generator's 28 matrix board/project pairs,",
            "14 noncoplanar custom-rule files,",
            "the two connector showcases and their index/source assets, `manifest.json`,",
            "and this index. It does not delete other files.",
            "Use `--output /path/to/another/directory` to generate a separate copy.",
            "",
        ]
    )
    return "\n".join(lines)


def project_text(case: Union[RFCase, CombinedCircuit]) -> str:
    """Declare native rules and exact, Board Setup-visible RF class assignments."""
    first_leg = case.signal_profiles[0]
    default_class = {
        "name": "Default",
        "priority": 2_147_483_647,
        "tuning_profile": "",
        "clearance": 0.2,
        "track_width": 0.1,
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "diff_pair_width": 0.1,
        "diff_pair_gap": 0.2,
        "diff_pair_via_gap": 0.2,
    }
    rf_class = {
        **default_class,
        "name": case.net_class,
        "priority": 0,
        "track_width": first_leg.width_nm / 1_000_000,
    }
    if case.paired:
        rf_class["diff_pair_width"] = first_leg.width_nm / 1_000_000
        rf_class["diff_pair_gap"] = first_leg.spacing_nm / 1_000_000
    project = {
        "meta": {"filename": f"{case.name}.kicad_pro", "version": 1},
        "board": {
            "design_settings": {
                "rules": {
                    "min_track_width": 0.1,
                    "min_clearance": 0.2,
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
            "classes": [default_class, rf_class],
            "netclass_assignments": {},
            "netclass_patterns": [
                {"pattern": net_name, "netclass": case.net_class}
                for net_name in case.net_names
            ],
        },
    }
    if case.paired:
        project["board"]["design_settings"]["rules"]["min_hole_clearance"] = 0.15
    return json.dumps(project, indent=2) + "\n"


def custom_rules_text(case: Union[RFCase, CombinedCircuit]) -> Optional[str]:
    """Set each noncoplanar layer's trace-edge clearance to its GND pour."""
    if case.coplanar:
        return None
    lines = [
        "(version 1)",
        "# Generated fixture policy: same-layer GND stays 3 trace widths away.",
        "# Tracks only: leave pads, vias and adjacent reference planes unchanged.",
    ]
    for leg in case.signal_profiles:
        clearance_nm = NONCOPLANAR_CLEARANCE_FACTOR * leg.width_nm
        clearance = f"{clearance_nm / 1_000_000:.6f}mm"
        condition = (
            f"A.Type == 'Track' && A.hasNetclass('{case.net_class}') "
            "&& B.Type == 'Zone' && B.NetName == 'GND'"
        )
        name = f"{case.net_class}: {leg.layer} track to GND >= {clearance}"
        lines.extend(
            (
                "",
                f"(rule {json.dumps(name)}",
                f"  (layer {json.dumps(leg.layer)})",
                f"  (condition {json.dumps(condition)})",
                f"  (constraint clearance (min {clearance})))",
            )
        )
    return "\n".join(lines) + "\n"


def legacy_fixture_files() -> dict[str, str]:
    """Retain isolated cases for targeted automated work outside the review folder."""
    files = {}
    records = []
    for case in RF_CASES:
        source = case.board_text()
        files[f"{case.name}.kicad_pcb"] = source
        files[f"{case.name}.kicad_pro"] = project_text(case)
        rules = custom_rules_text(case)
        if rules is not None:
            files[f"{case.name}.kicad_dru"] = rules
        records.append(case_record(case, source))
    files["manifest.json"] = json.dumps(records, indent=2) + "\n"
    files["README.md"] = inspection_index(records)
    files.update(
        {f"showcases/{name}": source for name, source in showcase_files().items()}
    )
    return files


def combined_project_text(board: CombinedBoard) -> str:
    """Give each bank its own native class and every circuit an exact assignment."""
    project = json.loads(project_text(board.circuits[0]))
    project["meta"]["filename"] = f"{board.name}.kicad_pro"
    classes = [project["net_settings"]["classes"][0]]
    patterns = []
    for priority, (name, nets) in enumerate(board.net_classes.items()):
        circuit = next(case for case in board.circuits if case.net_class == name)
        native_class = json.loads(project_text(circuit))["net_settings"]["classes"][1]
        native_class["priority"] = priority
        classes.append(native_class)
        patterns.extend({"pattern": net, "netclass": name} for net in nets)
    project["net_settings"]["classes"] = classes
    project["net_settings"]["netclass_patterns"] = patterns
    return json.dumps(project, indent=2) + "\n"


def combined_rules_text(board: CombinedBoard) -> str:
    """Apply each noncoplanar layer/profile rule once, leaving CPWG untouched."""
    rules = ["(version 1)"]
    seen = set()
    for circuit in board.circuits:
        source = custom_rules_text(circuit)
        if source is None:
            continue
        for rule in source.split("\n\n")[1:]:
            rule = rule.strip()
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)
    return "\n\n".join(rules) + "\n"


def combined_record(board: CombinedBoard, source: str) -> dict[str, object]:
    """Describe circuits within one physical board, not separate board files."""
    circuits = []
    for circuit in board.circuits:
        record = case_record(circuit, "")
        for key in ("board", "sha256", "custom_rules"):
            record.pop(key)
        record["title"] = circuit.title
        record["length_description"] = (
            f"{circuit.length_mm:g} mm controlled core + connector launches"
        )
        circuits.append(record)
    analysis = analyze(
        Config(enabled=True, specifications=board.specifications()), board.snapshot()
    )
    return {
        "name": board.name,
        "title": board.title,
        "board": f"{board.name}.kicad_pcb",
        "project": f"{board.name}.kicad_pro",
        "custom_rules": f"{board.name}.kicad_dru",
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "target_ohms": board.target_ohms,
        "differential": board.paired,
        "board_bounds_nm": board.board_bounds,
        "net_classes": board.net_classes,
        "circuit_count": len(circuits),
        "expected_reviewed_rows": len(analysis.sections),
        "circuits": circuits,
    }


def combined_index(records: list[dict[str, object]]) -> str:
    """Make the two human-facing projects the only example-board entry points."""
    lines = [
        "# Two impedance review boards",
        "",
        "| Open in KiCad | Target | Connectors | Circuits |",
        "| --- | ---: | --- | ---: |",
    ]
    for record in records:
        connectors = "USB-C" if record["differential"] else "SMA"
        lines.append(
            f"| [{record['title']}]({record['board']}) | {record['target_ohms']} Ω "
            f"| {connectors}, two per circuit | {record['circuit_count']} |"
        )
    lines.extend(
        [
            "",
            "Each project is one continuous six-layer board with two labelled banks:",
            "**noncoplanar** (surface microstrip / inner stripline) and **CPWG**",
            "(surface / buried coplanar waveguide). Each bank contains top, inner and",
            "bottom 2 mm and 120 mm controlled cores, plus a 150 mm three-layer route.",
            "Lengths describe controlled cores; connector launches add electrical length.",
            "Every circuit has unique nets; both members of each USB pair share one",
            "highlight. Separate noncoplanar/CPWG net classes are visible in Board Setup.",
            "USB controlled-run edge spacing is 8 mil (0.2032 mm).",
            "",
            "Open the PCB, **Fill All Zones** (B), and select F.Cu, In2.Cu or B.Cu.",
            "Use **Inactive Layer View Mode → Dim** to see nearby board context.",
            "All six copper layers have GND pours. The adjacent reference layers are",
            "In1.Cu for top, In1.Cu + In3.Cu for inner, and In4.Cu for bottom.",
            "Keep each `.kicad_pro` and `.kicad_dru` beside its board: custom rules",
            "hold noncoplanar same-layer GND zones 3 × the actual trace width away.",
            "CPWG uses its close ground gap and stitching vias instead.",
            "",
            "These are workflow/capture examples, **not solver-qualified impedance",
            "coupons or USB-certified designs**. Consolidated geometry has not been",
            "tested or run through DRC; review the workflow first. Canonical sources",
            "omit generated fill caches. Plugin settings remain in the project database;",
            "`manifest.json` is example metadata only.",
            "",
            "[Design notes](../../docs/rf-impedance-fixtures.md) ·",
            "[Embedded footprint sources and license](showcases/source-footprints/LICENSE.md)",
            "",
            "Regenerate these two projects:",
            "",
            "```sh",
            "myenv/bin/python scripts/generate_rf_impedance_fixtures.py",
            "```",
            "",
            "Isolated matrix cases remain available on demand, outside this folder:",
            "",
            "```sh",
            "myenv/bin/python scripts/generate_rf_impedance_fixtures.py --legacy-matrix --output /tmp/isolated-rf-examples",
            "```",
            "",
            "The generator only writes its named artifacts. `--retire-legacy` moves",
            "known old standalone board projects to a recoverable temporary backup;",
            "unrelated files and the pinned footprint assets are left alone.",
            "",
        ]
    )
    return "\n".join(lines)


def fixture_files(*, legacy_matrix: bool = False) -> dict[str, str]:
    """Generate two combined review projects unless isolated files are requested."""
    if legacy_matrix:
        return legacy_fixture_files()
    files = {}
    records = []
    for board in COMBINED_BOARDS:
        source = board.board_text()
        files[f"{board.name}.kicad_pcb"] = source
        files[f"{board.name}.kicad_pro"] = combined_project_text(board)
        files[f"{board.name}.kicad_dru"] = combined_rules_text(board)
        records.append(combined_record(board, source))
    files["manifest.json"] = json.dumps(records, indent=2) + "\n"
    files["README.md"] = combined_index(records)
    assets = REPOSITORY_ROOT / "examples/impedance/showcases/source-footprints"
    for name in (
        "LICENSE.md",
        "SMA_Amphenol_132134_Vertical.kicad_mod",
        "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.kicad_mod",
    ):
        files[f"showcases/source-footprints/{name}"] = (assets / name).read_text(
            encoding="utf-8"
        )
    return files


def retire_legacy_examples(directory: Path) -> Optional[Path]:
    """Move only known superseded example artifacts to a recoverable backup."""
    from tests.rf_impedance_showcases import CONNECTOR_SHOWCASES

    names = [
        f"{case.name}{suffix}"
        for case in RF_CASES
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    ]
    names.extend(
        f"showcases/{case.name}{suffix}"
        for case in CONNECTOR_SHOWCASES
        for suffix in (".kicad_pcb", ".kicad_pro")
    )
    names.append("showcases/README.md")
    existing = [name for name in names if (directory / name).is_file()]
    if not existing:
        return None
    backup = Path(tempfile.mkdtemp(prefix="retired-impedance-examples."))
    for name in existing:
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(directory / name), target)
    return backup


def generate(
    directory: Path, *, check: bool = False, legacy_matrix: bool = False
) -> tuple[Path, ...]:
    """Write canonical fixtures, or read-only check every expected artifact."""
    files = fixture_files(legacy_matrix=legacy_matrix)
    if not check:
        directory.mkdir(parents=True, exist_ok=True)
    different = []
    for name, source in files.items():
        destination = directory / name
        if check:
            if not destination.is_file() or destination.read_bytes() != source.encode(
                "utf-8"
            ):
                different.append(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")
    return tuple(different)


def main(argv: Optional[list[str]] = None) -> int:
    """Expose reproducible generation without importing KiCad or initializing wx."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY_ROOT / "examples" / "impedance"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--legacy-matrix", action="store_true")
    parser.add_argument("--retire-legacy", action="store_true")
    args = parser.parse_args(argv)
    directory = args.output.resolve()
    if args.legacy_matrix and directory == REPOSITORY_ROOT / "examples/impedance":
        parser.error("Use --output outside examples/impedance for isolated cases")
    if args.retire_legacy and (args.check or args.legacy_matrix):
        parser.error("--retire-legacy only applies when generating combined boards")
    different = generate(directory, check=args.check, legacy_matrix=args.legacy_matrix)
    if different:
        sys.stderr.write("RF fixtures differ; regenerate after reviewing changes:\n")
        sys.stderr.write("".join(f"  {path}\n" for path in different))
        return 1
    verb = "Verified" if args.check else "Generated"
    if args.retire_legacy:
        backup = retire_legacy_examples(directory)
        if backup is not None:
            sys.stdout.write(f"Moved superseded standalone examples to {backup}\n")
    description = (
        "28 isolated cases + two showcases"
        if args.legacy_matrix
        else "two combined boards"
    )
    sys.stdout.write(f"{verb} {description}: {directory / 'README.md'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
