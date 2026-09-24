"""Capture saved connector PCBs through the same production gallery workflow."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional
from xml.etree import ElementTree as ET

import pytest

from impedance.model import resolved_layer_settings
from impedance.palette import ThemeContext
from scripts import generate_rf_impedance_captures as gallery
from tests.rf_impedance_combined import COMBINED_BOARDS
from tests.rf_impedance_fixtures import BOARD_BOUNDS, RF_CASES
from tests.rf_impedance_showcases import CONNECTOR_SHOWCASES, DIRECTORY, showcase_files
from tests.test_impedance_capture_examples import _verify_native_box


@pytest.fixture(scope="module")
def showcase_sources(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize optional legacy connectors only in an isolated test directory."""
    directory = tmp_path_factory.mktemp("connector-capture-examples") / "showcases"
    for name, source in showcase_files().items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def isolate_showcase_directory(
    monkeypatch: pytest.MonkeyPatch, showcase_sources: Path
) -> None:
    """Keep the native fixture tests independent of removed tracked example files."""
    monkeypatch.setattr(sys.modules[__name__], "DIRECTORY", showcase_sources)


@pytest.mark.parametrize(
    "arguments,case_names,showcase_names",
    [
        ([], tuple(case.name for case in COMBINED_BOARDS), ()),
        (
            ["--showcase", CONNECTOR_SHOWCASES[0].name],
            (),
            (CONNECTOR_SHOWCASES[0].name,),
        ),
        (
            [
                "--showcase",
                CONNECTOR_SHOWCASES[1].name,
                "--showcase",
                CONNECTOR_SHOWCASES[0].name,
            ],
            (),
            tuple(item.name for item in CONNECTOR_SHOWCASES),
        ),
        (
            ["--case", RF_CASES[0].name, "--showcase", CONNECTOR_SHOWCASES[1].name],
            (RF_CASES[0].name,),
            (CONNECTOR_SHOWCASES[1].name,),
        ),
        (
            [
                "--showcase",
                CONNECTOR_SHOWCASES[0].name,
                "--showcase",
                CONNECTOR_SHOWCASES[0].name,
            ],
            (),
            (CONNECTOR_SHOWCASES[0].name,),
        ),
    ],
)
def test_repeatable_cli_selection_keeps_default_matrix_and_explicit_showcases_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    case_names: tuple[str, ...],
    showcase_names: tuple[str, ...],
) -> None:
    """A showcase-only command must not unexpectedly generate all 28 RF boards."""
    calls = []

    def generate(output: Path, cli: Path, **kwargs: Any) -> Path:
        """Inspect selection without launching native processes."""
        calls.append(kwargs)
        return output / "index.html"

    monkeypatch.setattr(gallery, "generate", generate)
    assert (
        gallery.main(
            [
                "--output",
                str(tmp_path),
                "--kicad-cli",
                "kicad-cli",
                "--svg-only",
                *arguments,
            ]
        )
        == 0
    )
    assert tuple(case.name for case in calls[0]["cases"]) == case_names
    assert tuple(item.name for item in calls[0].get("showcases", ())) == showcase_names
    assert calls[0]["rasterize"] is False


@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
def test_showcase_rows_use_all_actual_saved_signal_geometry_and_widths(
    showcase: Any,
) -> None:
    """Every selected native segment belongs to a production-reviewed row."""
    path = DIRECTORY / f"{showcase.name}.kicad_pcb"
    snapshot = gallery.native_snapshot(path)
    config, sections = gallery.reviewed_showcase_sections(showcase, snapshot)
    selected = tuple(
        trace for trace in snapshot.traces if trace.net in showcase.net_names
    )
    assert selected
    assert {trace.trace_id for row in sections for trace in row.traces} == {
        trace.trace_id for trace in selected
    }
    assert {(row.layer, row.width_nm) for row in sections} == {
        (trace.layer, trace.width_nm) for trace in selected
    }
    assert "section_groups" not in config.to_dict()
    references = dict(showcase.reference_layers_by_layer)
    for row in sections:
        spec = next(
            spec for spec in config.specifications if spec.spec_id == row.spec_id
        )
        assert spec.target_ohms == showcase.target_ohms
        settings = resolved_layer_settings(spec, row.layer)
        assert settings.reference_layers == references[row.layer]
        assert settings.ground_gap_nm == 200_000
        assert settings.spacing_nm == (
            203_200 if showcase.kind.startswith("differential") else None
        )
        assert set(row.net_names) == set(showcase.net_names)
    assert max(point[0] for trace in selected for point in trace.points) > 120_000_000


@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
def test_showcase_crop_reads_actual_native_outline_and_available_layers(
    showcase: Any,
) -> None:
    """Smaller four-layer showcases must not inherit the six-layer matrix frame."""
    path = DIRECTORY / f"{showcase.name}.kicad_pcb"
    bounds = gallery.native_board_bounds(path)
    assert bounds != BOARD_BOUNDS
    assert bounds[2] - bounds[0] > 120_000_000
    assert 0 < bounds[3] - bounds[1] < 100_000_000
    layers = gallery.native_capture_layers(path)
    assert (
        tuple(layer for layer in layers if layer.endswith(".Cu"))
        == gallery.native_snapshot(path).layers
    )
    assert "In3.Cu" not in layers and "In4.Cu" not in layers
    assert "Edge.Cuts" in layers


def test_new_routed_layer_without_reference_metadata_fails_closed() -> None:
    """A later source-board transition cannot silently lose its reference planes."""
    showcase = CONNECTOR_SHOWCASES[0]
    snapshot = gallery.native_snapshot(DIRECTORY / f"{showcase.name}.kicad_pcb")
    changed = replace(
        snapshot,
        traces=tuple(
            replace(trace, layer="B.Cu") if trace.net in showcase.net_names else trace
            for trace in snapshot.traces
        ),
    )
    with pytest.raises(ValueError, match="reference|B.Cu"):
        gallery.reviewed_showcase_sections(showcase, changed)


@pytest.mark.parametrize("theme", [None, "explicit-theme"])
def test_showcase_generation_wires_actual_geometry_crop_palette_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, theme: Optional[str]
) -> None:
    """Use native saved copper and the production compositor; do not fake traces."""
    exports = []
    original = {
        path: path.read_bytes()
        for path in DIRECTORY.iterdir()
        if path.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }

    def prepare(
        cli: Path,
        case: Any,
        examples: Path,
        output: Path,
        *,
        use_existing_fills: bool = False,
    ) -> Path:
        """Copy actual geometry; native zone filling is tested separately below."""
        output.mkdir(parents=True)
        return Path(shutil.copy2(examples / f"{case.name}.kicad_pcb", output))

    def export(
        cli: Path, board: Path, output: Path, layers: tuple[str, ...], palette: str
    ) -> dict[str, str]:
        """Record native exporter inputs without inventing any copper SVG paths."""
        exports.append((board, layers, palette))
        empty_canvas = '<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="200mm" viewBox="0 0 300 200"/>'
        sources = dict.fromkeys(layers, empty_canvas)
        left, top, right, bottom = gallery.native_board_bounds(board)
        # Only the real board outline is represented in this non-native wiring
        # stub. Actual KiCad copper/parts/fills are checked in the native test.
        sources["Edge.Cuts"] = (
            empty_canvas[:-2]
            + f'><rect x="{left / 1e6}" y="{top / 1e6}" width="{(right - left) / 1e6}" '
            + f'height="{(bottom - top) / 1e6}" fill="none" stroke="#9ca7b4"/></svg>'
        )
        return sources

    monkeypatch.setattr(
        gallery,
        "capture_theme",
        lambda cli, requested: ThemeContext(requested or "user-theme", "#edf1f3"),
    )
    monkeypatch.setattr(gallery, "prepare_native_board", prepare)
    monkeypatch.setattr(gallery, "export_board_layers", export)
    index = gallery.generate(
        tmp_path,
        Path("kicad-cli"),
        cases=(),
        showcases=CONNECTOR_SHOWCASES,
        examples=DIRECTORY.parent,
        rasterize=False,
        theme=theme,
    )
    assert index.is_file()
    records = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert {record["case"] for record in records} == {
        item.name for item in CONNECTOR_SHOWCASES
    }
    assert len(exports) == 2
    for record in records:
        assert record["theme"] == (theme or "user-theme")
        assert record["background_color"] == "#edf1f3"
        assert record["board_bounds_nm"] == list(
            gallery.native_board_bounds(tmp_path / record["board"])
        )
        assert record["png"] is None
        root = ET.fromstring((tmp_path / record["svg"]).read_text(encoding="utf-8"))  # noqa: S314 - local test-generated SVG.
        assert root[0].attrib["fill"] == "#edf1f3"
        boxes = [
            element
            for element in root.iter()
            if element.get("data-impedance-highlight")
        ]
        assert len(boxes) == 1 and boxes[0].get("stroke", "").lower() == "#ffff00"
    for board, layers, palette in exports:
        assert palette == (theme or "user-theme")
        assert (
            tuple(layer for layer in layers if layer.endswith(".Cu"))
            == gallery.native_snapshot(board).layers
        )
    assert all(path.read_bytes() == contents for path, contents in original.items())


@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
@pytest.mark.parametrize("suffix", [".kicad_pro", ".kicad_dru"])
def test_connector_copy_rejects_stale_sidecars_without_touching_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, showcase: Any, suffix: str
) -> None:
    """Showcase support must preserve the existing stale-sidecar safety boundary."""
    examples, output = tmp_path / "examples", tmp_path / "output"
    examples.mkdir()
    output.mkdir()
    source = examples / f"{showcase.name}.kicad_pcb"
    shutil.copy2(DIRECTORY / source.name, source)
    stale = output / f"{showcase.name}{suffix}"
    stale.write_bytes(b"retained obsolete sidecar")
    original = source.read_bytes()

    def no_native(*args: Any, **kwargs: Any) -> None:
        """Reject sidecars before invoking KiCad or copying replacement files."""
        pytest.fail("Native command must not run with a stale sidecar")

    monkeypatch.setattr(gallery.subprocess, "run", no_native)
    with pytest.raises(ValueError, match="Stale capture sidecar"):
        gallery.prepare_native_board(Path("kicad-cli"), showcase, examples, output)
    assert source.read_bytes() == original
    assert stale.read_bytes() == b"retained obsolete sidecar"
    assert not (output / source.name).exists()


@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
def test_native_connector_capture_uses_the_running_test_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, showcase: Any
) -> None:
    """Linux subprocesses must not launch a mounted host macOS virtual environment."""
    monkeypatch.setenv("KICAD_CLI", "/container/bin/kicad-cli")
    monkeypatch.setenv("KICAD_CAPTURE_PNG", "1")
    monkeypatch.setattr(sys, "executable", "/container/test-env/bin/python")

    def inspect_invocation(command: list[str], **kwargs: Any) -> None:
        """Stop before native execution after checking the selected interpreter."""
        assert command[0] == sys.executable
        assert command[command.index("--kicad-cli") + 1] == "/container/bin/kicad-cli"
        assert kwargs["check"] is True
        assert kwargs["timeout"] > 0
        raise RuntimeError("Native invocation inspected")

    monkeypatch.setattr(subprocess, "run", inspect_invocation)
    with pytest.raises(RuntimeError, match="Native invocation inspected"):
        test_native_connector_gallery_preserves_sources_and_generates_real_color_pngs(
            tmp_path, showcase
        )


@pytest.mark.native_kicad
@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
def test_native_connector_gallery_preserves_sources_and_generates_real_color_pngs(
    tmp_path: Path, showcase: Any
) -> None:
    """Render real KiCad layers and wx SVG PNGs when native checks are enabled."""
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if not executable or os.environ.get("KICAD_CAPTURE_PNG") != "1":
        pytest.skip(
            "Set KICAD_CLI and KICAD_CAPTURE_PNG=1 for actual connector captures"
        )
    original = {
        path: path.read_bytes()
        for path in DIRECTORY.iterdir()
        if path.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }
    # Isolate native wx from UI doubles while retaining this platform's test env.
    subprocess.run(
        [
            sys.executable,
            str(Path(gallery.__file__).resolve()),
            "--output",
            str(tmp_path),
            "--kicad-cli",
            executable,
            "--showcase",
            showcase.name,
            "--examples",
            str(DIRECTORY.parent),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=gallery.REPOSITORY_ROOT,
    )
    records = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert records
    for record in records:
        board = tmp_path / record["board"]
        _, sections = gallery.reviewed_showcase_sections(
            showcase, gallery.native_snapshot(board)
        )
        section = next(
            section
            for section in sections
            if section.section_id == record["section_id"]
        )
        _verify_native_box(
            tmp_path / record["svg"],
            tmp_path / record["png"],
            tmp_path / record["baseline_png"],
            tmp_path / record["context_svg"],
            section,
            gallery.native_board_bounds(board),
        )
        assert (tmp_path / record["context_svg"]).read_text(encoding="utf-8").count(
            "<path"
        ) > 20
        assert "(filled_polygon" in (tmp_path / record["board"]).read_text(
            encoding="utf-8"
        )
    assert all(path.read_bytes() == contents for path, contents in original.items())


@pytest.mark.parametrize(
    "outline",
    [
        "",
        '(gr_rect (start 20 30) (end 20 30) (layer "Edge.Cuts"))',
        '(gr_circle (center 20 30) (end 40 30) (layer "Edge.Cuts"))',
    ],
)
def test_unavailable_or_unsupported_board_outline_is_not_replaced_with_matrix_bounds(
    tmp_path: Path,
    outline: str,
) -> None:
    """Reject missing, degenerate or unsupported context instead of guessing scale."""
    board = tmp_path / "outline.kicad_pcb"
    board.write_text(f"(kicad_pcb {outline})", encoding="utf-8")
    with pytest.raises(ValueError, match="outline|Edge.Cuts"):
        gallery.native_board_bounds(board)


def test_duplicate_python_showcase_selection_fails_before_native_or_file_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Python API must not overwrite a selected capture directory twice."""

    def no_theme(*args: Any, **kwargs: Any) -> None:
        """Reject duplicate work before consulting native preferences."""
        pytest.fail("No native command should run for duplicate showcase selection")

    monkeypatch.setattr(gallery, "capture_theme", no_theme)
    with pytest.raises(ValueError, match="Duplicate"):
        gallery.generate(
            tmp_path / "not-created",
            Path("kicad-cli"),
            cases=(),
            showcases=(CONNECTOR_SHOWCASES[0],) * 2,
        )
    assert not (tmp_path / "not-created").exists()


def test_connector_refill_detects_source_project_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard adjacent project definitions as well as the original PCB bytes."""
    showcase = CONNECTOR_SHOWCASES[0]
    examples = tmp_path / "sources"
    examples.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(DIRECTORY / f"{showcase.name}{suffix}", examples)
    source_project = examples / f"{showcase.name}.kicad_pro"

    def changed_source(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Model an external source edit without fabricating a native fill cache."""
        source_project.write_text('{"external_change":true}', encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(gallery.subprocess, "run", changed_source)
    with pytest.raises(RuntimeError, match="original example changed.*kicad_pro"):
        gallery.prepare_native_board(
            Path("kicad-cli"), showcase, examples, tmp_path / "output"
        )


@pytest.mark.parametrize(
    "suffix", [".kicad_pcb", ".kicad_pro", ".kicad_dru", "directory", "report"]
)
def test_capture_copy_rejects_output_symlinks_before_overwriting_other_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    """A reused output link must never redirect a copy into another source board."""
    showcase = CONNECTOR_SHOWCASES[0]
    examples, output = tmp_path / "sources", tmp_path / "output"
    examples.mkdir()
    for native_suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(DIRECTORY / f"{showcase.name}{native_suffix}", examples)
    (examples / f"{showcase.name}.kicad_dru").write_text(
        "(version 1)", encoding="utf-8"
    )
    victim = tmp_path / "other-project"
    victim.mkdir()
    target = victim / f"{showcase.name}.kicad_pcb"
    target.write_bytes(b"retain unrelated original bytes")
    if suffix == "directory":
        output.symlink_to(victim, target_is_directory=True)
        link = output
    else:
        output.mkdir()
        link = output / (
            "drc.json" if suffix == "report" else f"{showcase.name}{suffix}"
        )
        link.symlink_to(target)

    def no_native(*args: Any, **kwargs: Any) -> None:
        """Reject unsafe destinations before KiCad or any replacement copy runs."""
        pytest.fail("Native command must not consume an output symlink")

    monkeypatch.setattr(gallery.subprocess, "run", no_native)
    with pytest.raises(ValueError, match="[Ss]ymlink"):
        gallery.prepare_native_board(Path("kicad-cli"), showcase, examples, output)
    assert target.read_bytes() == b"retain unrelated original bytes"
    assert link.is_symlink()


@pytest.mark.parametrize("showcase", CONNECTOR_SHOWCASES, ids=lambda item: item.name)
def test_connector_copy_passes_complete_native_project_and_rejects_missing_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    showcase: Any,
) -> None:
    """Copy all sidecars intact; a successful command alone is not fill evidence."""
    examples, output = tmp_path / "sources", tmp_path / "output"
    examples.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(DIRECTORY / f"{showcase.name}{suffix}", examples)
    (examples / f"{showcase.name}.kicad_dru").write_text(
        "(version 1)", encoding="utf-8"
    )
    originals = {path: path.read_bytes() for path in examples.iterdir()}
    calls = []

    def successful_command_without_native_fill(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Inspect exactly what native KiCad receives without fabricating copper."""
        calls.append(arguments)
        assert all(
            (output / source.name).read_bytes() == contents
            for source, contents in originals.items()
        )
        assert Path(arguments[-1]) == output / f"{showcase.name}.kicad_pcb"
        assert arguments[1:5] == ["pcb", "drc", "--refill-zones", "--save-board"]
        assert arguments[arguments.index("--output") + 1] == str(output / "drc.json")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(
        gallery.subprocess, "run", successful_command_without_native_fill
    )
    with pytest.raises(ValueError, match="No filled copper.*Fill All Zones"):
        gallery.prepare_native_board(Path("kicad-cli"), showcase, examples, output)
    assert len(calls) == 1
    assert all(
        source.read_bytes() == contents for source, contents in originals.items()
    )
