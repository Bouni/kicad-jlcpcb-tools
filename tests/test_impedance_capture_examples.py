"""Exercise realistic short and board-spanning route fixtures used by the gallery."""

import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from typing import Any, Optional
from xml.etree import ElementTree as ET
import zlib

import pytest

from impedance.matching import analyze
from impedance.model import (
    BoardSnapshot,
    Bounds,
    Config,
    LayerSettings,
    Section,
    Specification,
)
from impedance.palette import ThemeContext
from impedance.render import annotated_svg, section_viewport
from scripts.generate_impedance_captures import (
    capture_theme,
    export_board_layers,
    export_example_svg,
)
from scripts.generate_rf_impedance_captures import (
    DEFAULT_EXAMPLES,
    native_snapshot,
    reviewed_sections,
)
from tests.impedance_capture_fixtures import (
    BOARD_BOUNDS,
    EXAMPLES,
    CaptureExample,
    board_text,
)
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard
from tests.rf_impedance_fixtures import RF_CASES, RFCase


@pytest.fixture(scope="module")
def rf_examples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep the full geometry matrix on demand, outside the two shipped boards."""
    directory = tmp_path_factory.mktemp("rf-capture-source-boards")
    from scripts.generate_rf_impedance_fixtures import custom_rules_text, project_text

    for case in RF_CASES:
        (directory / f"{case.name}.kicad_pcb").write_text(
            case.board_text(), encoding="utf-8"
        )
        (directory / f"{case.name}.kicad_pro").write_text(
            project_text(case), encoding="utf-8"
        )
        rules = custom_rules_text(case)
        if rules is not None:
            (directory / f"{case.name}.kicad_dru").write_text(rules, encoding="utf-8")
    return directory


RF_NATIVE_CASES = (
    "single-ended-cpwg-top-2mm",
    "single-ended-microstrip-top-120mm",
    "single-ended-stripline-inner-120mm",
    "single-ended-microstrip-bottom-120mm",
    "differential-cpwg-transition-150mm",
)
SHORT_TRACE_LANDMARK = (205_000_000, 222_000_000)  # Real U2 pad center.


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
def test_gallery_routes_remain_complete_connected_sections(
    example: CaptureExample,
) -> None:
    """Keep short, long and meandering nets whole, with atomic differential pairs."""
    config = Config(
        enabled=True,
        specifications=(
            Specification(
                example.name,
                example.title,
                "90" if example.paired else "50",
                "differential" if example.paired else "single_ended",
                "Capture signals",
                (
                    LayerSettings(
                        "F.Cu",
                        ("B.Cu",),
                        spacing_nm=200_000 if example.paired else None,
                    ),
                ),
            ),
        ),
    )
    analysis = analyze(
        config,
        BoardSnapshot(
            ("F.Cu", "B.Cu"),
            example.traces(),
            net_classes=("Capture signals",),
            net_class_context_digest="fixture-class-context",
            net_class_memberships=tuple(
                (net, ("Capture signals",))
                for net in sorted({trace.net for trace in example.traces()})
            ),
            differential_pairs=(("SIGNAL_N", "SIGNAL_P"),) if example.paired else (),
        ),
    )
    sections = analysis.sections
    assert len(sections) == 1
    assert {trace.trace_id for trace in sections[0].traces} == {
        trace.trace_id for trace in example.traces()
    }
    assert sections[0].bounds == example.section().bounds


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
def test_gallery_whole_route_and_board_context_framing(example: CaptureExample) -> None:
    """Contain every endpoint and copper edge while retaining useful board context."""
    section = example.section()
    viewport = section_viewport(section, 800, 420, board_bounds=BOARD_BOUNDS)
    assert viewport.width / viewport.height == pytest.approx(800 / 420)
    left, top, right, bottom = section.bounds
    assert viewport.left < left and viewport.top < top
    assert viewport.left + viewport.width > right
    assert viewport.top + viewport.height > bottom
    assert viewport.width >= (BOARD_BOUNDS[2] - BOARD_BOUNDS[0]) * 0.15 * 0.85
    assert viewport.height >= (BOARD_BOUNDS[3] - BOARD_BOUNDS[1]) * 0.15 * 0.85
    for trace in section.traces:
        for x, y in trace.points:
            assert 0 < (x - viewport.left) / viewport.width * 800 < 800
            assert 0 < (y - viewport.top) / viewport.height * 420 < 420


def test_short_route_native_landmark_fits_the_current_context_crop() -> None:
    """Keep the optional native pixel probe inside the actual 15%-minimum crop."""
    example = next(example for example in EXAMPLES if example.name == "short-2mm")
    viewport = section_viewport(example.section(), 800, 420, board_bounds=BOARD_BOUNDS)
    x, y = SHORT_TRACE_LANDMARK
    assert 4 <= (x - viewport.left) / viewport.width * 800 < 796
    assert 4 <= (y - viewport.top) / viewport.height * 420 < 416


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
def test_capture_uses_one_unfilled_bounding_rectangle_outside_all_selected_copper(
    example: CaptureExample,
) -> None:
    """Enclose complete routes with a box rather than tracing their individual paths."""
    source = '<svg xmlns="http://www.w3.org/2000/svg" width="320mm" height="280mm" viewBox="0 0 320 280"><path d="M20,20 L270,260" stroke="#167db4" fill="none"/></svg>'
    section = example.section()
    result = annotated_svg(source, section, board_bounds=BOARD_BOUNDS)
    root = ET.fromstring(result)  # noqa: S314 - local generated regression SVG.
    highlights = [
        element for element in root.iter() if element.get("data-impedance-highlight")
    ]
    assert len(highlights) == 1
    box = highlights[0]
    assert box.tag.rsplit("}", 1)[-1] == "rect", (
        "The selected segment needs a bounding box, not a trace contour"
    )
    assert box.get("data-impedance-highlight") == "box"
    assert box.get("fill") == "none"
    assert box.get("stroke", "").lower() == "#ffff00"
    assert float(box.get("stroke-opacity", "1")) == 1
    assert float(box.get("opacity", "1")) == 1
    assert float(box.attrib["stroke-width"]) == 2
    viewport = section_viewport(section, 800, 420, board_bounds=BOARD_BOUNDS)
    factor = 800 / viewport.width
    left, top, right, bottom = section.bounds
    expected = (
        (left - viewport.left) * factor - 16,
        (top - viewport.top) * factor - 16,
        (right - left) * factor + 32,
        (bottom - top) * factor + 32,
    )
    assert tuple(
        float(box.attrib[name]) for name in ("x", "y", "width", "height")
    ) == pytest.approx(expected, abs=0.001)
    assert any(element.get("stroke") == "#167db4" for element in root.iter()), (
        "Preserve the supplied native palette instead of repainting selected copper"
    )


def test_gallery_exercises_required_lengths_and_stable_native_boards() -> None:
    """Prevent examples degenerating into tiny synthetic header-only fixtures."""
    lengths = {example.name: example.length_mm for example in EXAMPLES}
    assert lengths["short-2mm"] == 2
    assert lengths["horizontal-100mm"] == 100
    assert lengths["horizontal-200mm"] == 200
    assert lengths["vertical-200mm"] == 200
    assert lengths["diagonal-200mm"] == 200
    assert lengths["meander-240mm"] == 240
    assert lengths["differential-150mm"] == 150
    for example in EXAMPLES:
        native = board_text(example)
        assert native == board_text(example)
        assert '"Edge.Cuts"' in native and '"MountingHole"' in native
        assert all(f'(uuid "{trace.trace_id}")' in native for trace in example.traces())


@pytest.mark.native_kicad
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
def test_real_kicad_cli_plots_feed_production_capture(
    tmp_path: Path, example: CaptureExample
) -> None:
    """Use actual KiCad SVG geometry when an explicit or PATH CLI is available."""
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Set KICAD_CLI to enable native KiCad SVG capture integration")
    plot = export_example_svg(Path(executable), example, tmp_path)
    source = plot.read_text(encoding="utf-8")
    # Real KiCad plots contain many paths, unlike the unit rasterizer's stub.
    assert len(re.findall(r"<path\b", source)) > 20
    result = annotated_svg(source, example.section(), board_bounds=BOARD_BOUNDS)
    root = ET.fromstring(result)  # noqa: S314 - generated local fixture, not user XML.
    assert root.attrib["width"] == "800" and root.attrib["height"] == "420"
    assert 'data-impedance-highlight="box"' in result
    assert "Board location" not in result
    assert len(result) > len(source)


@pytest.mark.parametrize("theme", [None, "My custom palette"])
def test_native_gallery_uses_configured_theme_unless_explicitly_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, theme: Optional[str]
) -> None:
    """Do not silently force a built-in palette over the user's PCB preferences."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text(board_text(EXAMPLES[0]), encoding="utf-8")
    commands = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model a successful native command by creating its actual output file."""
        commands.append(arguments)
        output = Path(arguments[arguments.index("--output") + 1])
        output.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1mm" height="1mm" viewBox="0 0 1 1"><path d="M0,0 L1,1" stroke="#19b47b"/></svg>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("scripts.generate_impedance_captures.subprocess.run", run)
    sources = export_board_layers(
        Path("kicad-cli"), board, tmp_path / "plots", ("F.Cu", "B.Cu"), theme
    )
    assert len(sources) == len(commands) == 2
    for arguments in commands:
        assert "--black-and-white" not in arguments
        if theme is None:
            assert "--theme" not in arguments
        else:
            assert arguments[arguments.index("--theme") + 1] == theme
    assert all("#19b47b" in source for source in sources.values())


@pytest.mark.parametrize("rf_gallery", [False, True], ids=["geometry", "rf"])
def test_gallery_uses_one_configured_theme_for_foreground_and_canvas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rf_gallery: bool, rf_examples: Path
) -> None:
    """Honor light custom settings instead of mixing them with a dark canvas."""
    from scripts import (
        generate_impedance_captures as general,
        generate_rf_impedance_captures as rf,
    )

    config_base = tmp_path / "preferences"
    config_dir = config_base / "10.0"
    color_dir = config_dir / "colors"
    color_dir.mkdir(parents=True)
    (config_dir / "pcbnew.json").write_text(
        json.dumps({"appearance": {"color_theme": "light-test"}}), encoding="utf-8"
    )
    (color_dir / "light-test.json").write_text(
        json.dumps({"board": {"background": "rgb(247, 249, 251)"}}), encoding="utf-8"
    )
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config_base))
    themes = []
    source = '<svg xmlns="http://www.w3.org/2000/svg" width="320mm" height="280mm" viewBox="0 0 320 280"><path d="M20,20 L270,260" stroke="#192b3d" fill="none"/></svg>'

    def version(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Answer only the read-only CLI version discovery command."""
        assert arguments[1:] == ["version", "--format", "plain"]
        return subprocess.CompletedProcess(arguments, 0, "10.0.6\n", "")

    def export_example(
        cli: Path,
        example: CaptureExample,
        directory: Path,
        theme: Optional[str] = None,
        *,
        palette: Optional[ThemeContext] = None,
    ) -> Path:
        """Retain a source SVG so real composition/framing still executes."""
        assert palette is not None and palette.background == "#f7f9fb"
        themes.append(palette.name)
        path = directory / f"{example.name}-native.svg"
        path.write_text(source, encoding="utf-8")
        return path

    def export_layers(
        cli: Path,
        board: Path,
        directory: Path,
        layers: tuple[str, ...],
        theme: Optional[str] = None,
    ) -> dict[str, str]:
        """Keep native paint while recording the exact selected palette."""
        themes.append(theme)
        return dict.fromkeys(layers, source)

    def prepare(
        cli: Path,
        case: RFCase,
        examples: Path,
        directory: Path,
        *,
        use_existing_fills: bool = False,
    ) -> Path:
        """Copy the complete board without pretending to exercise native filling."""
        directory.mkdir(parents=True)
        return Path(shutil.copy2(examples / f"{case.name}.kicad_pcb", directory))

    monkeypatch.setattr(general.subprocess, "run", version)
    monkeypatch.setattr(general, "export_example_svg", export_example)
    monkeypatch.setattr(rf, "export_board_layers", export_layers)
    monkeypatch.setattr(rf, "prepare_native_board", prepare)
    output = tmp_path / "gallery"
    if rf_gallery:
        rf.generate(
            output,
            Path("kicad-cli"),
            examples=rf_examples,
            rasterize=False,
            cases=(RF_CASES[0],),
        )
    else:
        general.generate(output, Path("kicad-cli"), rasterize=False)
    assert themes and all(theme == "light-test" for theme in themes)
    records = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    for record in records:
        svg = ET.fromstring((output / record["svg"]).read_text(encoding="utf-8"))  # noqa: S314
        assert svg[0].attrib["fill"] == "#f7f9fb"
        assert "#192b3d" in ET.tostring(svg, encoding="unicode")
        assert record["background_color"] == "#f7f9fb"
        assert record["theme"] == "light-test"


@pytest.mark.parametrize(
    ("platform", "environment", "relative"),
    [
        ("darwin", {}, "Library/Preferences/kicad/10.0"),
        ("linux", {}, ".config/kicad/10.0"),
        ("linux", {"XDG_CONFIG_HOME": "xdg"}, "xdg/kicad/10.0"),
        ("win32", {"APPDATA": "appdata"}, "appdata/kicad/10.0"),
        (
            "darwin",
            {"KICAD_CONFIG_HOME": "custom", "XDG_CONFIG_HOME": "ignored"},
            "custom/10.0",
        ),
    ],
)
def test_capture_theme_respects_native_versioned_settings_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    environment: dict[str, str],
    relative: str,
) -> None:
    """Read the selected CLI major/minor's profile, not another KiCad install."""
    from scripts import generate_impedance_captures as gallery

    for name in ("KICAD_CONFIG_HOME", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, str(tmp_path / value))
    monkeypatch.setattr(gallery.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def version(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Return the installed CLI's exact version without invoking another Python."""
        assert arguments[1:] == ["version", "--format", "plain"]
        return subprocess.CompletedProcess(arguments, 0, "10.0.6\n", "")

    monkeypatch.setattr(gallery.subprocess, "run", version)
    expected = tmp_path / relative
    expected.mkdir(parents=True)
    (expected / "pcbnew.json").write_text(
        json.dumps({"appearance": {"color_theme": "_builtin_classic"}}),
        encoding="utf-8",
    )
    selected = capture_theme(Path("kicad-cli"))
    assert (selected.name, selected.background) == ("_builtin_classic", "#000000")
    override = capture_theme(Path("kicad-cli"), "_builtin_default")
    assert (override.name, override.background) == ("_builtin_default", "#001023")


@pytest.mark.parametrize(("returncode", "stdout"), [(1, "10.0.6"), (0, "unknown")])
def test_capture_theme_cannot_silently_guess_preferences_after_cli_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
) -> None:
    """Refuse misleading configured-theme provenance when version lookup fails."""
    from impedance.palette import PaletteError
    from scripts import generate_impedance_captures as gallery

    def version(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Model a failed or unrecognized native CLI response."""
        return subprocess.CompletedProcess(arguments, returncode, stdout, "")

    monkeypatch.setattr(gallery.subprocess, "run", version)
    with pytest.raises(PaletteError, match="settings version"):
        capture_theme(Path("kicad-cli"))


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_gallery_matches_actual_on_disk_board_and_pairs_each_layer(
    case: RFCase,
    rf_examples: Path,
) -> None:
    """Exercise actual saved example geometry instead of trusting fixture previews."""
    snapshot = native_snapshot(rf_examples / f"{case.name}.kicad_pcb")
    config, sections = reviewed_sections(case, snapshot)
    assert "section_groups" not in config.to_dict(), (
        "Gallery must exercise automatic pair rows"
    )
    assert all(
        spec.target_ohms == ("90" if case.paired else "50")
        for spec in config.specifications
    )
    assert len(sections) == len(case.signal_profiles)
    assert {(row.layer, row.width_nm) for row in sections} == {
        (profile.layer, profile.width_nm) for profile in case.signal_profiles
    }
    assert {section.spec_id for section in sections} == {
        spec.spec_id for spec in config.specifications
    }
    assert {trace.trace_id for section in sections for trace in section.traces} == {
        trace.trace_id for trace in case.traces()
    }
    for section in sections:
        assert len(section.net_names) == (2 if case.paired else 1)
        assert len({trace.layer for trace in section.traces}) == 1


@pytest.mark.parametrize("board", COMBINED_BOARDS, ids=lambda board: board.name)
def test_shipped_combined_boards_capture_all_actual_signal_copper(
    board: CombinedBoard,
) -> None:
    """The two human-review boards retain complete pairs across layers and widths."""
    snapshot = native_snapshot(DEFAULT_EXAMPLES / f"{board.name}.kicad_pcb")
    config, sections = reviewed_sections(board, snapshot)
    signals = {net for circuit in board.circuits for net in circuit.net_names}
    captured_ids = [trace.trace_id for row in sections for trace in row.traces]
    assert len(captured_ids) == len(set(captured_ids))
    assert set(captured_ids) == {
        trace.trace_id for trace in snapshot.traces if trace.net in signals
    }
    assert {row.layer for row in sections} == {"F.Cu", "In2.Cu", "B.Cu"}
    assert len({row.width_nm for row in sections}) > 1
    assert {spec.target_ohms for spec in config.specifications} == {
        "90" if board.paired else "50"
    }
    assert all(len(row.net_names) == (2 if board.paired else 1) for row in sections)
    circuits = {frozenset(circuit.net_names) for circuit in board.circuits}
    assert {frozenset(row.net_names) for row in sections} == circuits
    assert any(circuit.length_mm == 2 for circuit in board.circuits)
    assert any(circuit.length_mm >= 100 for circuit in board.circuits)


@pytest.mark.parametrize("bad_source", ["(wrong)", "(kicad_pcb", "(kicad_pcb))"])
def test_rf_native_reader_rejects_malformed_board(
    tmp_path: Path, bad_source: str
) -> None:
    """Refuse to fabricate overlays when the real native input is invalid."""
    board = tmp_path / "invalid.kicad_pcb"
    board.write_text(bad_source, encoding="utf-8")
    with pytest.raises(ValueError):
        native_snapshot(board)


@pytest.mark.parametrize("suffix", [".kicad_pro", ".kicad_dru"])
def test_rf_gallery_reused_output_rejects_obsolete_native_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """Never let yesterday's copied project/rules silently change a fresh capture."""
    from scripts import generate_rf_impedance_captures as gallery

    case = RF_CASES[0]
    examples, output = tmp_path / "examples", tmp_path / "output"
    examples.mkdir()
    output.mkdir()
    original = examples / f"{case.name}.kicad_pcb"
    original.write_text("(kicad_pcb)", encoding="utf-8")
    obsolete = output / f"{case.name}{suffix}"
    obsolete.write_bytes(b"retained old rules")

    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Native CLI must not consume an obsolete sidecar")

    monkeypatch.setattr(gallery.subprocess, "run", must_not_run)
    with pytest.raises(ValueError, match="[Ss]tale.*sidecar"):
        gallery.prepare_native_board(Path("kicad-cli"), case, examples, output)
    assert obsolete.read_bytes() == b"retained old rules"
    assert not (output / original.name).exists()


@pytest.mark.parametrize("net_field", ["(net 1)", '(net "RF_SE")'])
def test_rf_native_reader_accepts_numeric_and_named_net_serialization(
    tmp_path: Path, net_field: str
) -> None:
    """Handle both legacy net codes and the names written by newer KiCad saves."""
    board = tmp_path / "native.kicad_pcb"
    board.write_text(
        '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "RF_SE") (segment (start 10 20) (end 130 20) (width 0.35) (layer "F.Cu") '
        + net_field
        + ' (uuid "native-track")))',
        encoding="utf-8",
    )
    snapshot = native_snapshot(board)
    assert snapshot.traces[0].net == "RF_SE"
    assert snapshot.traces[0].points == (
        (10_000_000, 20_000_000),
        (130_000_000, 20_000_000),
    )
    assert snapshot.traces[0].width_nm == 350_000


def _png_pixels(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Decode real wx RGB/RGBA PNG samples, including every adaptive row filter."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color, _, _, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    assert depth == 8 and color in (2, 6) and interlace == 0
    channels = 3 if color == 2 else 4
    offset, compressed = 8, bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        assert (
            zlib.crc32(kind + payload) & 0xFFFFFFFF
            == struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        )
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
    raw = zlib.decompress(compressed)
    stride = width * channels
    assert len(raw) == (stride + 1) * height
    previous = bytearray(stride)
    pixels = []
    for row_number in range(height):
        start = row_number * (stride + 1)
        filter_type = raw[start]
        row = bytearray(raw[start + 1 : start + 1 + stride])
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                assert filter_type == 4
                estimate = left + above - upper_left
                predictor = min(
                    (left, above, upper_left), key=lambda value: abs(estimate - value)
                )
            row[index] = (row[index] + predictor) & 255
        for index in range(0, stride, channels):
            pixels.append(
                (
                    row[index],
                    row[index + 1],
                    row[index + 2],
                    row[index + 3] if channels == 4 else 255,
                )
            )
        previous = row
    return width, height, pixels


def _verify_native_box(
    svg: Path,
    png: Path,
    baseline_png: Path,
    native_context: Path,
    section: Section,
    board_bounds: Bounds,
) -> list[tuple[int, int, int, int]]:
    """Check every box edge and prove arbitrary native-palette pixels are untouched."""
    root = ET.fromstring(svg.read_text(encoding="utf-8"))  # noqa: S314 - generated local SVG.
    native = ET.fromstring(native_context.read_text(encoding="utf-8"))  # noqa: S314 - native local KiCad SVG.

    def geometry(node: ET.Element) -> tuple[object, ...]:
        """Compare actual native shapes, paint and hierarchy without XML whitespace."""
        return (
            node.tag.rsplit("}", 1)[-1],
            tuple(sorted(node.attrib.items())),
            (node.text or "").strip(),
            tuple(geometry(child) for child in node),
        )

    views = [node for node in root if node.get("data-impedance-view") == "segment"]
    assert len(views) == 1
    assert tuple(geometry(child) for child in views[0]) == tuple(
        geometry(child) for child in native
    ), "Capture changed or painted over native PCB colors/geometry"
    highlights = [node for node in root.iter() if node.get("data-impedance-highlight")]
    assert len(highlights) == 1
    box = highlights[0]
    assert (
        box.tag.rsplit("}", 1)[-1] == "rect"
        and box.get("data-impedance-highlight") == "box"
    )
    assert box.get("fill") == "none" and float(box.attrib["stroke-width"]) == 2
    assert box.get("stroke", "").lower() == "#ffff00"
    assert float(box.get("stroke-opacity", "1")) == 1
    left, top, box_width, box_height = (
        float(box.attrib[key]) for key in ("x", "y", "width", "height")
    )
    right, bottom = left + box_width, top + box_height
    viewport = section_viewport(section, 800, 420, board_bounds=board_bounds)
    scale = 800 / viewport.width
    expected = (
        (section.bounds[0] - viewport.left) * scale - 16,
        (section.bounds[1] - viewport.top) * scale - 16,
        (section.bounds[2] - section.bounds[0]) * scale + 32,
        (section.bounds[3] - section.bounds[1]) * scale + 32,
    )
    assert (left, top, box_width, box_height) == pytest.approx(expected, abs=0.001)
    width, height, pixels = _png_pixels(png)
    baseline_width, baseline_height, baseline = _png_pixels(baseline_png)
    assert (width, height) == (baseline_width, baseline_height) == (800, 420)
    assert all(pixel[3] == 255 for pixel in pixels)
    for index in range(17):
        fraction = index / 16
        for x, y in (
            (left + fraction * box_width, top),
            (left + fraction * box_width, bottom),
            (left, top + fraction * box_height),
            (right, top + fraction * box_height),
        ):
            neighbors = [
                pixels[row * width + column]
                for row in range(max(0, round(y) - 2), min(height, round(y) + 3))
                for column in range(max(0, round(x) - 2), min(width, round(x) + 3))
            ]
            assert any(
                (red, green, blue) == (255, 255, 0) for red, green, blue, _ in neighbors
            ), f"Missing rectangular yellow border near {x}, {y}"
    for index, (actual, original) in enumerate(zip(pixels, baseline)):
        x, y = index % width, index // width
        horizontal = (
            left - 2 <= x <= right + 2 and min(abs(y - top), abs(y - bottom)) <= 2
        )
        vertical = (
            top - 2 <= y <= bottom + 2 and min(abs(x - left), abs(x - right)) <= 2
        )
        if not (horizontal or vertical):
            assert actual == original, (
                f"Capture repainted native context/color at {x}, {y}"
            )
    for trace in section.traces:
        for x, y in trace.points:
            assert left < (x - viewport.left) * scale < right
            assert top < (y - viewport.top) * scale < bottom
    return pixels


def _verify_muted_native_paints(path: Path) -> None:
    """Preserve the NanoSVG opacity regression without assuming particular colors."""
    root = ET.fromstring(path.read_text(encoding="utf-8"))  # noqa: S314 - generated local SVG.
    active = root.attrib["data-impedance-active-copper-layer"]
    assert root.attrib["data-impedance-inactive-copper-visibility"] == "hidden"
    copper = {
        group.get("data-impedance-layer")
        for group in root.iter()
        if group.get("data-impedance-layer", "").endswith(".Cu")
    }
    assert copper == {active}
    checked = 0
    for group in root.iter():
        role = group.get("data-impedance-role")
        if role != "plane":
            continue
        assert group.get("opacity") == "1", (
            "Bake opacity into native leaves for NanoSVG"
        )
        for node in group:
            style = dict(
                item.split(":", 1)
                for item in node.get("style", "").split(";")
                if ":" in item
            )
            assert float(style.get("opacity", "1")) == 1
            if style.get("fill", "none") != "none":
                assert 0 < float(style["fill-opacity"]) <= 0.6
            if style.get("stroke", "none") != "none":
                assert float(style["stroke-opacity"]) <= 1
            checked += 1
    assert checked > 0


@pytest.fixture(scope="module")
def native_capture_gallery(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Isolate wx's native app from fake-wx unit tests when explicitly requested."""
    if os.environ.get("KICAD_CAPTURE_PNG") != "1":
        pytest.skip("Set KICAD_CAPTURE_PNG=1 and KICAD_CLI for real PNG gallery tests")
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Native PNG examples require a KiCad CLI")
    directory = tmp_path_factory.mktemp("native-impedance-captures")
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_impedance_captures.py"
    )
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(directory),
            "--kicad-cli",
            executable,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return directory


@pytest.mark.native_kicad
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
def test_real_capture_png_is_opaque_visible_and_not_header_only(
    native_capture_gallery: Path, example: CaptureExample
) -> None:
    """Keep all original native colors while boxing whole routes at every scale."""
    pixels = _verify_native_box(
        native_capture_gallery / f"{example.name}.svg",
        native_capture_gallery / f"{example.name}.png",
        native_capture_gallery / f"{example.name}.baseline.png",
        native_capture_gallery / f"{example.name}-native.svg",
        example.section(),
        BOARD_BOUNDS,
    )
    viewport = section_viewport(example.section(), 800, 420, board_bounds=BOARD_BOUNDS)
    if example.name == "short-2mm":
        # The real U2 pad remains in frame; remote board edges need not be visible.
        point_x, point_y = SHORT_TRACE_LANDMARK
        pixel_x = round((point_x - viewport.left) / viewport.width * 800)
        pixel_y = round((point_y - viewport.top) / viewport.height * 420)
        assert 4 <= pixel_x < 796 and 4 <= pixel_y < 416
        neighbors = [
            pixels[y * 800 + x]
            for y in range(pixel_y - 4, pixel_y + 5)
            for x in range(pixel_x - 4, pixel_x + 5)
        ]
        assert len(set(neighbors)) > 1


@pytest.mark.native_kicad
def test_native_custom_light_palette_preserves_native_copper_and_canvas(
    tmp_path: Path,
) -> None:
    """Actually plot a custom theme without changing any real user's preferences."""
    if os.environ.get("KICAD_CAPTURE_PNG") != "1":
        pytest.skip("Set KICAD_CAPTURE_PNG=1 for the native custom-theme PNG test")
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Native custom-theme capture requires KiCad CLI")
    version = subprocess.run(
        [executable, "version", "--format", "plain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    match = re.match(r"^(\d+\.\d+)\.", version)
    assert match is not None
    config_base = tmp_path / "isolated-kicad-config"
    config_dir = config_base / match.group(1)
    colors = config_dir / "colors"
    colors.mkdir(parents=True)
    (config_dir / "pcbnew.json").write_text(
        json.dumps({"appearance": {"color_theme": "capture-light"}}), encoding="utf-8"
    )
    (colors / "capture-light.json").write_text(
        json.dumps(
            {
                "meta": {"version": 6},
                "board": {
                    "background": "rgb(247, 249, 251)",
                    "copper": {"f": "rgb(23, 43, 61)"},
                },
            }
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ, KICAD_CONFIG_HOME=str(config_base))
    output = tmp_path / "native-custom-light-gallery"
    command = (
        "from pathlib import Path; import sys; "
        "from scripts import generate_impedance_captures as gallery; "
        "gallery.EXAMPLES = (gallery.EXAMPLES[0],); "
        "gallery.generate(Path(sys.argv[1]), Path(sys.argv[2]))"
    )
    subprocess.run(
        [sys.executable, "-c", command, str(output), executable],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
    )
    record = json.loads((output / "manifest.json").read_text(encoding="utf-8"))[0]
    assert record["theme"] == "capture-light"
    assert record["background_color"] == "#f7f9fb"
    native = (output / record["native_svg"]).read_text(encoding="utf-8")
    assert "#172b3d" in native.lower(), "Native KiCad must use the custom copper color"
    example = EXAMPLES[0]
    pixels = _verify_native_box(
        output / record["svg"],
        output / record["png"],
        output / record["baseline_png"],
        output / record["native_svg"],
        example.section(),
        BOARD_BOUNDS,
    )
    assert pixels.count((247, 249, 251, 255)) > 1_000, (
        "Native raster must retain the selected light canvas, not a fixed dark fill"
    )


@pytest.fixture(scope="module")
def native_rf_capture_gallery(
    tmp_path_factory: pytest.TempPathFactory, rf_examples: Path
) -> Path:
    """Exercise filled native RF crops in a separate real-wx process on request."""
    if os.environ.get("KICAD_CAPTURE_PNG") != "1":
        pytest.skip("Set KICAD_CAPTURE_PNG=1 for actual colored RF PNG tests")
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Native RF captures require KiCad CLI")
    directory = tmp_path_factory.mktemp("native-rf-capture-gallery")
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_rf_impedance_captures.py"
    )
    command = [
        sys.executable,
        str(script),
        "--output",
        str(directory),
        "--kicad-cli",
        executable,
        "--examples",
        str(rf_examples),
    ]
    for case_name in RF_NATIVE_CASES:
        command.extend(("--case", case_name))
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    return directory


@pytest.mark.native_kicad
@pytest.mark.parametrize("case_name", RF_NATIVE_CASES)
def test_rf_native_pngs_have_full_color_context_and_complete_yellow_boxes(
    native_rf_capture_gallery: Path, case_name: str
) -> None:
    """Cover native palettes and complete rectangular boxes on every RF layer."""
    manifest = json.loads(
        (native_rf_capture_gallery / "manifest.json").read_text(encoding="utf-8")
    )
    records = [record for record in manifest if record["case"] == case_name]
    case = next(case for case in RF_CASES if case.name == case_name)
    assert len(records) == len(case.signal_profiles)
    for record in records:
        board = native_rf_capture_gallery / record["board"]
        _, sections = reviewed_sections(case, native_snapshot(board))
        section = next(
            section
            for section in sections
            if section.section_id == record["section_id"]
        )
        assert record["target_ohms"] == ("90" if case.paired else "50")
        _verify_native_box(
            native_rf_capture_gallery / record["svg"],
            native_rf_capture_gallery / record["png"],
            native_rf_capture_gallery / record["baseline_png"],
            native_rf_capture_gallery / record["context_svg"],
            section,
            tuple(record["board_bounds_nm"]),
        )
        _verify_muted_native_paints(native_rf_capture_gallery / record["context_svg"])
        source = (native_rf_capture_gallery / record["svg"]).read_text(encoding="utf-8")
        assert (
            "Board location" not in source
            and 'data-impedance-highlight="box"' in source
        )
