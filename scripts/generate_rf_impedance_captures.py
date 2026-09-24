"""Render actual RF example boards through native KiCad and production capture code.

Temporary copies are refilled and saved by KiCad CLI. Original PCB/project files
are never modified. The default is the two combined review boards. Use --board
to select one, or --case/--showcase with an on-demand legacy fixture directory.
Use --use-existing-fills for
manual review: copy already-filled boards and plot without running DRC or refill.
No alternate Python is required.
"""

import argparse
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional, Union

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impedance.matching import analyze, validate_review  # noqa: E402
from impedance.model import (  # noqa: E402
    BoardSnapshot,
    Bounds,
    Config,
    LayerSettings,
    Section,
    Specification,
    Trace,
    format_length,
    resolved_layer_settings,
)
from impedance.render import _wx_rasterize, annotated_svg  # noqa: E402
from scripts.generate_impedance_captures import (  # noqa: E402
    capture_theme,
    export_board_layers,
    rasterize_capture,
)
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard  # noqa: E402
from tests.rf_impedance_fixtures import RF_CASES, RFCase  # noqa: E402
from tests.rf_impedance_showcases import (  # noqa: E402
    CONNECTOR_SHOWCASES,
    ConnectorShowcase,
)

CONTEXT_LAYERS = ("F.SilkS", "B.SilkS", "Edge.Cuts")
DEFAULT_EXAMPLES = REPOSITORY_ROOT / "examples" / "impedance"


def _parse_board(source: str) -> list[object]:
    """Read native board records, including quoted strings containing parentheses."""
    stack: list[list[object]] = [[]]
    for token in re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', source):
        if token == "(":
            child: list[object] = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("Unexpected closing parenthesis in native PCB")
            stack.pop()
        else:
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("Incomplete native PCB")
    board = stack[0][0]
    if not isinstance(board, list) or not board or board[0] != "kicad_pcb":
        raise ValueError("Expected a native KiCad PCB")
    return board


def _children(node: list[object], name: str) -> list[list[object]]:
    """Select exact direct child records without confusing nested fill data."""
    return [
        child
        for child in node
        if isinstance(child, list) and child and child[0] == name
    ]


def _field(node: list[object], name: str) -> list[object]:
    """Require exactly one native field before trusting a capture coordinate."""
    values = _children(node, name)
    if len(values) != 1:
        raise ValueError(f"Native PCB requires one {name} field")
    return values[0][1:]


def _nm(value: object) -> int:
    """Parse native millimetre text without rounded binary floating-point values."""
    result = Decimal(str(value)) * 1_000_000
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError("Invalid nanometre coordinate in native PCB")
    return int(result)


def native_snapshot(path: Path) -> BoardSnapshot:
    """Extract the real example's complete track geometry after native zone fill."""
    board = _parse_board(path.read_text(encoding="utf-8"))
    nets = {str(net[1]): str(net[2]) for net in _children(board, "net")}
    layers = tuple(
        str(layer[1])
        for layer in _field(board, "layers")
        if isinstance(layer, list) and str(layer[1]).endswith(".Cu")
    )
    traces = []
    if _children(board, "arc"):
        raise ValueError(
            "RF gallery native reader currently expects straight-track fixtures"
        )
    for track in _children(board, "segment"):
        start, end = _field(track, "start"), _field(track, "end")
        net = str(_field(track, "net")[0])
        traces.append(
            Trace(
                str(_field(track, "uuid")[0]),
                str(_field(track, "layer")[0]),
                nets.get(net, net),
                _nm(_field(track, "width")[0]),
                ((_nm(start[0]), _nm(start[1])), (_nm(end[0]), _nm(end[1]))),
            )
        )
    return BoardSnapshot(layers, tuple(traces))


def native_capture_layers(path: Path) -> tuple[str, ...]:
    """Plot only the copper and context layers actually declared by this PCB."""
    board = _parse_board(path.read_text(encoding="utf-8"))
    layers = tuple(
        str(layer[1])
        for layer in _field(board, "layers")
        if isinstance(layer, list) and len(layer) > 1
    )
    return tuple(layer for layer in layers if layer.endswith(".Cu")) + tuple(
        layer for layer in CONTEXT_LAYERS if layer in layers
    )


def native_board_bounds(path: Path) -> Bounds:
    """Read actual straight Edge.Cuts geometry rather than a matrix-sized frame."""
    board = _parse_board(path.read_text(encoding="utf-8"))
    points = []
    for item in board:
        if not isinstance(item, list) or not item or not str(item[0]).startswith("gr_"):
            continue
        layers = _children(item, "layer")
        if len(layers) != 1 or layers[0][1:] != ["Edge.Cuts"]:
            continue
        if item[0] not in ("gr_line", "gr_rect"):
            raise ValueError(
                "RF gallery requires straight line/rectangle Edge.Cuts geometry"
            )
        for field in ("start", "end"):
            point = _field(item, field)
            if len(point) != 2:
                raise ValueError("Invalid native board-outline coordinate")
            points.append((_nm(point[0]), _nm(point[1])))
    if not points:
        raise ValueError("RF gallery requires a native Edge.Cuts outline")
    bounds = (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("Native board outline has no positive area")
    return bounds


def fixture_snapshot(
    case: Union[RFCase, CombinedBoard, ConnectorShowcase], snapshot: BoardSnapshot
) -> BoardSnapshot:
    """Attach declared fixture classes/mates, retaining all saved-board copper.

    These bounded projects assign non-signal routing to Default. This is fixture
    metadata for the CLI harness, not a substitute for native class inspection.
    """
    if isinstance(case, ConnectorShowcase):
        pairs = ()
        if case.kind.startswith("differential"):
            if len(case.net_names) != 2:
                raise ValueError(
                    "A differential connector showcase must declare exactly two mates"
                )
            first, second = sorted(case.net_names)
            pairs = ((first, second),)
        memberships = dict.fromkeys(case.net_names, (case.net_class,))
    else:
        intent = case.snapshot()
        pairs = intent.differential_pairs
        memberships = dict(intent.net_class_memberships)
    for trace in snapshot.traces:
        if trace.net and trace.net not in memberships:
            memberships[trace.net] = ("Default",)
    ordered_memberships = tuple(sorted(memberships.items()))
    classes = tuple(sorted({name for names in memberships.values() for name in names}))
    return replace(
        snapshot,
        differential_pairs=pairs,
        net_classes=classes,
        net_class_memberships=ordered_memberships,
        net_class_context_digest=sha256(
            repr((classes, ordered_memberships)).encode("utf-8")
        ).hexdigest(),
    )


def reviewed_sections(
    case: Union[RFCase, CombinedBoard], snapshot: BoardSnapshot
) -> tuple[Config, tuple[Section, ...]]:
    """Use production automatic rows with the fixture's declared native net mates.

    Geometry comes from the actual saved PCB. This CLI-only harness supplies the
    fixture's known USB mate identity; live plugin snapshots obtain it through
    BOARD.DpCoupledNet. Neither path manually combines candidate sections.
    """
    snapshot = fixture_snapshot(case, snapshot)
    config = Config(enabled=True, specifications=case.specifications())
    analysis = analyze(config, snapshot)
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    validate_review(config, analysis)
    return config, analysis.sections


def reviewed_showcase_sections(
    showcase: ConnectorShowcase, snapshot: BoardSnapshot
) -> tuple[Config, tuple[Section, ...]]:
    """Match declared fixture intent against every actual saved signal width.

    This CLI-only harness supplies the showcase's declared USB mate identities;
    live plugin snapshots instead ask BOARD.DpCoupledNet. Capture geometry is
    never synthesized from metadata or re-created as a simplified fixture.
    """
    traces = tuple(
        trace for trace in snapshot.traces if trace.net in showcase.net_names
    )
    if {trace.net for trace in traces} != set(showcase.net_names):
        raise ValueError(f"{showcase.name}: declared signal nets have no routed copper")
    references = dict(showcase.reference_layers_by_layer)
    signal_layers = {trace.layer for trace in traces}
    if not signal_layers.issubset(snapshot.layers):
        raise ValueError(f"{showcase.name}: signal copper uses an unavailable layer")
    missing = signal_layers - set(references)
    if missing:
        raise ValueError(
            f"{showcase.name}: missing reference layers for {', '.join(sorted(missing))}"
        )
    specifications = (
        Specification(
            f"{showcase.name}/netclass",
            showcase.title,
            showcase.target_ohms,
            showcase.kind,
            showcase.net_class,
            layer_settings=tuple(
                LayerSettings(
                    layer,
                    references[layer],
                    showcase.spacing_nm,
                    showcase.ground_gap_nm,
                )
                for layer in snapshot.layers
                if layer in signal_layers
            ),
        ),
    )
    snapshot = fixture_snapshot(showcase, snapshot)
    config = Config(enabled=True, specifications=specifications)
    analysis = analyze(config, snapshot)
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    validate_review(config, analysis)
    return config, analysis.sections


def prepare_native_board(
    cli: Path,
    case: Union[RFCase, ConnectorShowcase, CombinedBoard],
    examples: Path,
    directory: Path,
    *,
    use_existing_fills: bool = False,
) -> Path:
    """Copy board context; optionally reuse editor fills without executing DRC."""
    if directory.is_symlink():
        raise ValueError(
            f"Capture destination is a symlink: {directory}. "
            "Use a fresh output directory; existing files have been retained."
        )
    directory.mkdir(parents=True, exist_ok=True)
    original = examples / f"{case.name}.kicad_pcb"
    board = directory / original.name
    destinations = tuple(
        board.with_suffix(suffix)
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    ) + (directory / "drc.json",)
    for target in destinations:
        if target.is_symlink():
            raise ValueError(
                f"Capture destination is a symlink: {target}. "
                "Use a fresh output directory; existing files have been retained."
            )
    original.read_bytes()  # Require the source PCB before creating any copies.
    originals = {
        original.with_suffix(suffix): original.with_suffix(suffix).read_bytes()
        if original.with_suffix(suffix).is_file()
        else None
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }
    for suffix in (".kicad_pro", ".kicad_dru"):
        source, target = original.with_suffix(suffix), board.with_suffix(suffix)
        if target.exists() and not source.is_file():
            raise ValueError(
                f"Stale capture sidecar {target} has no source counterpart. "
                "Use a fresh output directory; existing files have been retained."
            )
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        source = original.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, board.with_suffix(suffix))
    if not use_existing_fills:
        result = subprocess.run(
            [
                str(cli),
                "pcb",
                "drc",
                "--refill-zones",
                "--save-board",
                "--format",
                "json",
                "--output",
                str(directory / "drc.json"),
                str(board),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(
                f"KiCad native refill failed: {result.stdout}\n{result.stderr}"
            )
    for source, contents in originals.items():
        current = source.read_bytes() if source.is_file() else None
        if current != contents:
            raise RuntimeError(
                f"The original example changed during capture: {source.name}"
            )
    if "(filled_polygon" not in board.read_text(encoding="utf-8"):
        raise ValueError(
            f"No filled copper for {case.name}. Fill All Zones in KiCad and save "
            "the review copy before generating captures."
        )
    return board


def write_case_report(
    directory: Path,
    case: Union[RFCase, CombinedBoard, ConnectorShowcase],
    config: Config,
    snapshot: BoardSnapshot,
    sections: tuple[Section, ...],
) -> Path:
    """Use the production HTML writer with already-captured, ordered PNGs."""
    from impedance.html_report import OUTPUT_FILENAME, write_html_report
    from impedance.service import CapturedImage, prepare, report_rows

    plan = prepare(config, snapshot)
    if plan is None or plan.sections != sections:
        raise ValueError("The example report no longer matches its captured sections")
    images = tuple(
        directory / f"row-{index}-{section.layer.replace('.', '_')}.png"
        for index, section in enumerate(sections, 1)
    )
    return write_html_report(
        report_rows(plan, tuple(CapturedImage.load(path) for path in images)),
        directory / OUTPUT_FILENAME,
        board_name=case.title,
    )


def _gallery(directory: Path, records: list[dict[str, object]]) -> Path:
    """Show captured rows beside exact case, layer and file metadata."""
    cards = []
    for record in records:
        image = record["png"] or record["svg"]
        width_nm = int(record["width_nm"])
        width = (
            f"{format_length(width_nm)} mm "
            f"({Decimal(width_nm) / Decimal(25_400):.2f} mil)"
        )
        report = record.get("html_report")
        report_link = (
            f' · <a href="{html.escape(str(report), quote=True)}">Offline HTML report</a>'
            if report
            else ""
        )
        cards.append(
            f'<section><h2>{html.escape(str(record["title"]))} — {record["layer"]}</h2><img width="800" height="420" src="{html.escape(str(image))}"><p>{html.escape(str(record["kind"]))}, {record["target_ohms"]} Ω intended; width {width} · <a href="{record["board"]}">Native filled PCB</a> · <a href="{record["svg"]}">Capture SVG</a>{report_link}</p></section>'
        )
    target = directory / "index.html"
    target.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>RF impedance review captures</title><style>body{font:16px system-ui;margin:2rem;background:#eceff2;color:#18212c}section{background:white;padding:1rem;margin:1rem 0;width:800px}h2{font-size:18px}img{border:1px solid #ccd1d8}</style><h1>RF impedance review captures</h1><p>Actual saved example boards with native filled copper. Only the selected layer’s copper is shown; inactive copper is hidden, while component outlines, silkscreen and board edges are dimmed. The yellow box marks the selected route or pair. These are composed KiCad plots, not native editor screenshots or field-solver certification.</p>'
        + "".join(cards)
        + "</html>",
        encoding="utf-8",
    )
    (directory / "manifest.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )
    return target


def generate(
    directory: Path,
    cli: Path,
    *,
    examples: Path = DEFAULT_EXAMPLES,
    cases: tuple[Union[RFCase, CombinedBoard], ...] = COMBINED_BOARDS,
    showcases: tuple[ConnectorShowcase, ...] = (),
    rasterize: bool = True,
    theme: Optional[str] = None,
    use_existing_fills: bool = False,
) -> Path:
    """Capture every selected physical-layer section with shared production code."""
    from impedance.render import compose_layer_svgs

    selected = (*cases, *showcases)
    if len({case.name for case in selected}) != len(selected):
        raise ValueError("Duplicate RF capture cases or connector showcases")
    palette = capture_theme(cli, theme)
    directory.mkdir(parents=True, exist_ok=True)
    app = None
    if rasterize:
        import wx

        app = wx.GetApp() or wx.App(False)
    records = []
    for case in selected:
        case_directory = directory / case.name
        is_showcase = isinstance(case, ConnectorShowcase)
        source_directory = examples / "showcases" if is_showcase else examples
        board = prepare_native_board(
            cli,
            case,
            source_directory,
            case_directory,
            use_existing_fills=use_existing_fills,
        )
        snapshot = fixture_snapshot(case, native_snapshot(board))
        config, sections = (
            reviewed_showcase_sections(case, snapshot)
            if is_showcase
            else reviewed_sections(case, snapshot)
        )
        board_bounds = native_board_bounds(board)
        sources = export_board_layers(
            cli,
            board,
            case_directory / "native-layers",
            native_capture_layers(board),
            palette.name,
        )
        (case_directory / "review-config.json").write_text(
            json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        contexts: dict[str, str] = {}
        case_record_start = len(records)
        for row_index, section in enumerate(sections):
            stem = f"row-{row_index + 1}-{section.layer.replace('.', '_')}"
            composed = (
                case_directory / f"native-context-{section.layer.replace('.', '_')}.svg"
            )
            if section.layer not in contexts:
                contexts[section.layer] = compose_layer_svgs(
                    sources,
                    section.layer,
                    background_color=palette.background,
                    dimming_factor=palette.dimming_factor,
                )
                composed.write_text(contexts[section.layer], encoding="utf-8")
            svg = case_directory / f"{stem}.svg"
            svg.write_text(
                annotated_svg(
                    contexts[section.layer],
                    section,
                    board_bounds=board_bounds,
                    background_color=palette.background,
                ),
                encoding="utf-8",
            )
            png = case_directory / f"{stem}.png"
            baseline = None
            if rasterize:
                if use_existing_fills:
                    _wx_rasterize(svg, png, 800, 420)
                else:
                    baseline = rasterize_capture(svg, png)
            spec = next(
                spec
                for spec in config.specifications
                if spec.spec_id == section.spec_id
            )
            profile = resolved_layer_settings(spec, section.layer)
            records.append(
                {
                    "case": case.name,
                    "title": f"{case.title}: {', '.join(sorted({trace.net for trace in section.traces}))}",
                    "row_index": row_index,
                    "spec_id": section.spec_id,
                    "section_id": section.section_id,
                    "layer": section.layer,
                    "kind": spec.kind,
                    "width_nm": section.width_nm,
                    "target_ohms": case.target_ohms,
                    "reference_layers": profile.reference_layers,
                    "pair_spacing_nm": profile.spacing_nm,
                    "ground_gap_nm": profile.ground_gap_nm,
                    "trace_ids": tuple(trace.trace_id for trace in section.traces),
                    "section_bounds_nm": section.bounds,
                    "board_bounds_nm": board_bounds,
                    "source_kind": (
                        "connector showcase"
                        if is_showcase
                        else "combined review board"
                        if isinstance(case, CombinedBoard)
                        else "RF matrix"
                    ),
                    "source_board": str((source_directory / board.name).resolve()),
                    "png": str(png.relative_to(directory)) if rasterize else None,
                    "baseline_png": str(baseline.relative_to(directory))
                    if baseline is not None
                    else None,
                    "svg": str(svg.relative_to(directory)),
                    "context_svg": str(composed.relative_to(directory)),
                    "board": str(board.relative_to(directory)),
                    "review_config": str(
                        (case_directory / "review-config.json").relative_to(directory)
                    ),
                    "theme": palette.name,
                    "background_color": palette.background,
                    "inactive_layer_mode": "hidden-copper-dimmed-context",
                    "inactive_copper_visibility": "hidden",
                    "context_layer_mode": "dimmed",
                    "dimming_factor": palette.dimming_factor,
                    "zone_opacity": 0.6,
                    "fill_source": (
                        "existing editor fills" if use_existing_fills else "CLI refill"
                    ),
                    "theme_source": "explicit override"
                    if theme
                    else "configured KiCad PCB editor theme",
                    "renderer": (
                        "KiCad CLI + production color compositor / crop / box"
                        + (" + wx.svg" if rasterize else "")
                    ),
                }
            )
        if rasterize:
            report = write_case_report(case_directory, case, config, snapshot, sections)
            for record in records[case_record_start:]:
                record["html_report"] = str(report.relative_to(directory))
        sys.stdout.write(f"Captured {case.name}: {len(sections)} row(s)\n")
        sys.stdout.flush()
    del app
    return _gallery(directory, records)


def main(argv: Optional[list[str]] = None) -> int:
    """Offer repeatable example selection without any path-specific Python setup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    parser.add_argument("--kicad-cli", type=Path, default=shutil.which("kicad-cli"))
    parser.add_argument(
        "--board",
        action="append",
        choices=[board.name for board in COMBINED_BOARDS],
        help="Capture a combined review board; defaults to both",
    )
    parser.add_argument(
        "--case", action="append", choices=[case.name for case in RF_CASES]
    )
    parser.add_argument(
        "--showcase",
        action="append",
        choices=[case.name for case in CONNECTOR_SHOWCASES],
        help="Capture a real connector board; repeat to select several",
    )
    parser.add_argument(
        "--theme", help="Override the configured KiCad PCB editor theme"
    )
    parser.add_argument("--svg-only", action="store_true")
    parser.add_argument(
        "--use-existing-fills",
        action="store_true",
        help="Plot already-filled review boards without running DRC or refill",
    )
    args = parser.parse_args(argv)
    if args.kicad_cli is None:
        parser.error("Supply --kicad-cli when KiCad CLI is not on PATH")
    default_boards = args.board is None and args.case is None and args.showcase is None
    selected = tuple(
        board
        for board in COMBINED_BOARDS
        if default_boards or board.name in (args.board or ())
    ) + tuple(case for case in RF_CASES if case.name in (args.case or ()))
    selected_showcases = tuple(
        case for case in CONNECTOR_SHOWCASES if case.name in (args.showcase or ())
    )
    result = generate(
        args.output.resolve(),
        args.kicad_cli.resolve(),
        examples=args.examples.resolve(),
        cases=selected,
        showcases=selected_showcases,
        rasterize=not args.svg_only,
        theme=args.theme,
        use_existing_fills=args.use_existing_fills,
    )
    sys.stdout.write(f"{result}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
