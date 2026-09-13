"""Variant output freezes native values and publishes complete artifact sets.

Real plotter checks additionally require KICAD_JLCPCB_NATIVE_TESTS=1 in a desktop
session with pcbnew and wx available. They use disposable projects and do not
exercise PCB Editor undo or dirty state.
"""

import csv
from dataclasses import replace
from functools import partial
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

__all__ = ["window_ui"]

import fabrication_archive
import generate_hooks
from tests.fabrication_test_support import (
    Point,
    make_footprint,
    modules as fabrication_modules,
)
from tests.native_kicad_support import (
    native_bindings as kicad_bindings,
    native_runtime as kicad_runtime,
)
from tests.native_window_support import window_ui
from tests.variant_model_test_support import Snapshot, State
from tests.variant_native_support import native

modules = fabrication_modules
native_bindings = kicad_bindings
native_runtime = kicad_runtime


@pytest.fixture
def runtime(modules: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    """Construct the real exporter with isolated native geometry and stale cache."""
    footprints = [make_footprint("R1", 0, 0, Point(10, 20))]
    board = SimpleNamespace(
        GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        Footprints=lambda: footprints,
        GetDesignSettings=lambda: SimpleNamespace(GetAuxOrigin=lambda: Point(1, 2)),
    )
    parent = SimpleNamespace(settings={}, store=MagicMock(), library=MagicMock())
    return SimpleNamespace(
        exporter=modules.fabrication.Fabrication(parent, board),
        modules=modules,
        footprints=footprints,
        parent=parent,
    )


def part(reference: str = "R1", **changes: Any) -> native.ComponentVariantState:
    """Represent resolved native state; all inclusion flags are positive."""
    values = {
        "value": "Variant device",
        "lcsc": "C999",
        "footprint": "Package:Device",
    }
    values.update(changes)
    return State(reference, reference, "A", **values)


def snapshot(
    *parts: native.ComponentVariantState,
    default_parts: Optional[tuple[native.ComponentVariantState, ...]] = None,  # noqa: UP045
    variants: tuple[str, ...] = ("", "A", "B"),
) -> native.BoardVariantSnapshot:
    """Expose the snapshot contract without a mutable active-variant store."""
    return Snapshot(
        tuple(
            replace(part, variant_name=name)
            for name in variants
            for part in (
                default_parts if name == "" and default_parts is not None else parts
            )
        )
    )


def begin(
    runtime: SimpleNamespace, *parts: native.ComponentVariantState, **kwargs: Any
) -> None:
    """Freeze one explicit output context with a source validation callback."""
    runtime.exporter.begin_generation(
        snapshot(
            *(parts or (part(),)),
            default_parts=kwargs.pop("default_parts", None),
            variants=kwargs.pop("variants", ("", "A", "B")),
        ),
        kwargs.pop("variant_name", "A"),
        kwargs.pop("corrections", ()),
        kwargs.pop("validate_source", lambda: None),
    )


def stage_artifacts(exporter: Any, marker: bytes = b"new") -> None:
    """Create all three files in the private generation directory."""
    for path in exporter.get_staged_artifact_paths().values():
        Path(path).write_bytes(marker)


@pytest.mark.parametrize("variant_name", ["", "A"])
@pytest.mark.parametrize(
    "layer,angle,expected", [(0, 0, (10, -20, 90)), (31, 90, (7, -17, 180))]
)
def test_shared_correction_drives_every_variants_transforms(
    runtime: SimpleNamespace,
    variant_name: str,
    layer: int,
    angle: float,
    expected: tuple[float, float, float],
) -> None:
    """Base Value rules give every variant identical physical placement transforms."""
    runtime.footprints[:] = [make_footprint("R1", layer, angle, Point(10, 20))]
    base = part(value="Base device")
    corrections = (
        runtime.modules.data.Correction("^Base device$", 90, (1, 2)),
        runtime.modules.data.Correction("^Variant device$", 180, (5, 6)),
    )
    begin(
        runtime,
        default_parts=(base,),
        variant_name=variant_name,
        corrections=corrections,
    )
    runtime.footprints[0].GetValue = lambda: "Changed after capture"
    (row,) = runtime.exporter.prepare_cpl(())
    value = "Base device" if not variant_name else "Variant device"
    assert tuple(float(item) for item in row[3:6]) == expected
    assert (row[0], row[1], row[2], row[6]) == (
        "R1",
        value,
        "Package:Device",
        "bottom" if layer == 31 else "top",
    )
    runtime.exporter.generate_bom()
    assert _read_csv(runtime.exporter.get_staged_artifact_paths()["bom_csv"]) == [
        {
            "Comment": value,
            "Designator": "R1",
            "Footprint": "Package:Device",
            "LCSC": "C999",
            "Quantity": "1",
        }
    ]
    runtime.parent.store.get_part.assert_not_called()
    runtime.parent.store.read_bom_parts.assert_not_called()
    runtime.exporter.abort_generation()


@pytest.mark.parametrize(
    "variant_name,variant_lcsc,expected",
    [
        ("", "C222", (10, -20, 90)),
        ("A", "C222", (12, -22, 180)),
        ("A", "C333", (7, -15, 45)),
        ("A", "", (7, -15, 45)),
        ("A", "C444", (9, -18, 0)),
    ],
)
def test_output_lcsc_correction_is_frozen_with_variant_bom_and_cpl(
    runtime: SimpleNamespace,
    variant_name: str,
    variant_lcsc: str,
    expected: tuple[float, float, float],
) -> None:
    """The ordered part drives placement; missing exact rules use Default Value."""
    corrections = (
        runtime.modules.data.LcscCorrection("C111", 90, (1, 2)),
        runtime.modules.data.LcscCorrection("C222", 180, (3, 4)),
        runtime.modules.data.LcscCorrection("C444", 0, (0, 0)),
        runtime.modules.data.Correction("^Base device$", 45, (-2, -3)),
        runtime.modules.data.Correction("^Variant device$", 270, (5, 6)),
    )
    source = snapshot(
        part(value="Variant device", lcsc=variant_lcsc),
        default_parts=(part(value="Base device", lcsc="C111"),),
        variants=("", "A"),
    )
    before_components = source.components
    before_rules = tuple(rule.db_row() for rule in corrections)
    runtime.exporter.begin_generation(source, variant_name, corrections, lambda: None)
    runtime.footprints[0].GetValue = lambda: "Changed after capture"
    runtime.exporter.generate_cpl()
    runtime.exporter.generate_bom()
    paths = runtime.exporter.get_staged_artifact_paths()
    (placement,) = _read_csv(paths["cpl_csv"])
    value = "Base device" if not variant_name else "Variant device"
    lcsc = "C111" if not variant_name else variant_lcsc
    assert tuple(
        float(placement[field]) for field in ("Mid X", "Mid Y", "Rotation")
    ) == expected
    assert (placement["Designator"], placement["Val"], placement["Package"]) == (
        "R1",
        value,
        "Package:Device",
    )
    assert _read_csv(paths["bom_csv"]) == [
        {
            "Comment": value,
            "Designator": "R1",
            "Footprint": "Package:Device",
            "LCSC": lcsc,
            "Quantity": "1",
        }
    ]
    assert source.components == before_components
    assert tuple(rule.db_row() for rule in corrections) == before_rules
    runtime.parent.store.get_part.assert_not_called()
    runtime.parent.store.read_bom_parts.assert_not_called()
    runtime.exporter.abort_generation()


@pytest.mark.parametrize("invalid", [None, (), "wrong"])
def test_unresolved_corrections_never_create_variant_outputs(
    runtime: SimpleNamespace, invalid: Any
) -> None:
    """Only immutable validated correction records may enter a capture."""
    corrections = None if invalid is None else (invalid,)
    with pytest.raises(TypeError, match="Correction"):
        begin(runtime, corrections=corrections)
    assert runtime.exporter.output_snapshot is None
    assert not list(Path(runtime.exporter.outputdir).iterdir())


def test_base_change_during_capture_is_rejected_before_staging(
    runtime: SimpleNamespace,
) -> None:
    """The source validator still surrounds the entire shared-rule resolution."""
    calls = 0

    def validate() -> None:
        """Represent a native Default Value changing during capture."""
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("Default source changed")

    with pytest.raises(RuntimeError, match="Default source changed"):
        begin(runtime, validate_source=validate)
    assert calls == 2
    assert runtime.exporter.output_snapshot is None
    assert not list(Path(runtime.exporter.outputdir).iterdir())


@pytest.mark.parametrize(
    "flags,bom_count,cpl_count",
    [
        ({"pop": False}, 0, 0),
        ({"bom": False}, 0, 1),
        ({"pos": False}, 1, 0),
    ],
)
def test_variant_population_and_exclusions_are_independent(
    runtime: SimpleNamespace, flags: dict[str, bool], bom_count: int, cpl_count: int
) -> None:
    """Explicit variant flags replace live Default DNP and cached exclusions."""
    begin(runtime, part(**flags))
    assert len(runtime.exporter.prepare_cpl(())) == cpl_count
    runtime.exporter.generate_bom()
    assert (
        len(_read_csv(runtime.exporter.get_staged_artifact_paths()["bom_csv"]))
        == bom_count
    )
    runtime.exporter.abort_generation()


def test_empty_assignment_respects_output_policy(runtime: SimpleNamespace) -> None:
    """A cleared variant cannot recover C999 from cache or emit forbidden rows."""
    runtime.parent.settings = {"gerber": {"lcsc_bom_cpl": False}}
    begin(runtime, part(lcsc=""))
    runtime.parent.settings["gerber"]["lcsc_bom_cpl"] = True
    assert runtime.exporter.prepare_cpl(()) == ()
    runtime.exporter.generate_bom()
    assert _read_csv(runtime.exporter.get_staged_artifact_paths()["bom_csv"]) == []
    runtime.exporter.abort_generation()


@pytest.mark.parametrize("variant_name", ["", "A"])
def test_existing_anchored_package_correction_keeps_matching(
    runtime: SimpleNamespace, variant_name: str
) -> None:
    """Full native identity must not change legacy package-rule or output syntax."""
    runtime.footprints[0].GetFPID = lambda: SimpleNamespace(
        GetLibItemName=lambda: "SOT-23"
    )
    correction = runtime.modules.data.Correction("^SOT-23$", 90, (0, 0))
    begin(
        runtime,
        part(footprint="Package_TO_SOT_SMD:SOT-23"),
        variant_name=variant_name,
        corrections=(correction,),
    )
    (row,) = runtime.exporter.prepare_cpl(())
    assert row[2] == "SOT-23"
    assert row[5] == 90
    assert runtime.exporter.output_snapshot.bom_rows[0][2] == "SOT-23"
    runtime.exporter.abort_generation()


def test_boards_keep_independent_gerber_sources_and_archives(
    runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """Generating another board in one project never imports or replaces its plots."""
    first = runtime.exporter
    second_board = SimpleNamespace(
        GetFileName=lambda: str(tmp_path / "display.kicad_pcb")
    )
    second = runtime.modules.fabrication.Fabrication(runtime.parent, second_board)
    assert first.gerberdir != second.gerberdir
    first_source = Path(first.gerberdir) / "copper.gbr"
    second_source = Path(second.gerberdir) / "copper.gbr"
    first_source.write_bytes(b"first copper")
    second_source.write_bytes(b"second copper")
    first_archive = first.zip_gerber_excellon()
    second_archive = second.zip_gerber_excellon()
    assert first_archive != second_archive
    with ZipFile(first_archive) as archive:
        assert archive.namelist() == ["copper.gbr"]
        assert archive.read("copper.gbr") == b"first copper"
    with ZipFile(second_archive) as archive:
        assert archive.namelist() == ["copper.gbr"]
        assert archive.read("copper.gbr") == b"second copper"
    previous = second_archive.read_bytes()
    first_source.write_bytes(b"updated first copper")
    first.zip_gerber_excellon()
    assert second_archive.read_bytes() == previous
    assert second_source.read_bytes() == b"second copper"


def test_unknown_variant_does_not_fall_back_to_default(
    runtime: SimpleNamespace,
) -> None:
    """Removal of a remembered output name stops before creating output."""
    with pytest.raises(ValueError, match="variant"):
        begin(runtime, variant_name="Removed")
    assert list(Path(runtime.exporter.outputdir).iterdir()) == []


@pytest.mark.parametrize(
    "change,error,message",
    [
        ("source", RuntimeError, "native source changed"),
        ("missing", FileNotFoundError, "missing"),
        ("geometry", RuntimeError, "placement or auxiliary origin changed"),
    ],
)
def test_invalid_generation_preserves_all_previous_outputs(
    runtime: SimpleNamespace, change: str, error: type[Exception], message: str
) -> None:
    """Incomplete output or changed native inputs cannot replace any previous artifact."""
    validate = MagicMock()
    begin(runtime, validate_source=validate)
    public = runtime.exporter.get_artifact_paths()
    for path in public.values():
        Path(path).write_bytes(b"previous")
    stage_artifacts(runtime.exporter)
    if change == "source":
        validate.side_effect = RuntimeError(message)
    elif change == "missing":
        Path(runtime.exporter.get_staged_artifact_paths()["bom_csv"]).unlink()
    else:
        runtime.footprints[0].GetPosition = lambda: Point(99, 99)
    with pytest.raises(error, match=message):
        runtime.exporter.publish_generation()
    assert {Path(path).read_bytes() for path in public.values()} == {b"previous"}
    runtime.exporter.abort_generation()


def test_complete_generation_keeps_scratch_until_release(
    runtime: SimpleNamespace,
) -> None:
    """Published artifacts coexist with raw plots until their consumer releases them."""
    begin(runtime)
    public = runtime.exporter.get_artifact_paths()
    working = Path(runtime.exporter.gerberdir)
    stage_artifacts(runtime.exporter)
    runtime.exporter.publish_generation()
    assert {Path(path).read_bytes() for path in public.values()} == {b"new"}
    assert working.exists()
    runtime.exporter.abort_generation()
    assert not working.exists()


@pytest.mark.native_wx
@pytest.mark.parametrize("hook_exit", [0, 1], ids=["success", "failure"])
def test_post_hook_reads_published_variant_plots_before_generation_cleanup(
    window_ui: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_exit: int,
) -> None:
    """Real generation handlers keep current raw plots alive through a hook subprocess."""
    hook = tmp_path / "post_hook.py"
    hook.write_text(
        "import os\nfrom pathlib import Path\n"
        'gerbers = Path(os.environ["JLCPCB_GERBER_DIR"])\n'
        'project = Path(os.environ["JLCPCB_PROJECT_DIR"])\n'
        'assert all(Path(os.environ["JLCPCB_ARTIFACT_" + name]).is_file()\n'
        '           for name in ("GERBER_ZIP", "BOM_CSV", "CPL_CSV"))\n'
        '(project / "hook-dir.txt").write_text(str(gerbers), encoding="utf-8")\n'
        '(project / "hook-plots.txt").write_bytes(\n'
        '    (gerbers / "copper.gbr").read_bytes() + (gerbers / "drill.drl").read_bytes())\n'
        f"raise SystemExit({hook_exit})\n",
        encoding="utf-8",
    )
    run_process = subprocess.run

    def run_python_hook(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Use this environment's Python for a portable executable hook fixture."""
        return run_process([sys.executable, *command], **kwargs)

    monkeypatch.setattr(generate_hooks.subprocess, "run", run_python_hook)

    def check(ui: Any) -> None:
        dialog, board = ui.dialog, ui.board
        board.Drawings = lambda: ()
        board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C999")
        ui.controller.refresh()
        exporter = dialog.fabrication
        original_gerbers = Path(exporter.gerberdir)
        (original_gerbers / "copper.gbr").write_bytes(b"previous copper")
        (original_gerbers / "drill.drl").write_bytes(b"previous drill")

        def write_plot(name: str, contents: bytes, *_args: Any) -> None:
            """Stand in only for unavailable native plotters, retaining actual files."""
            (Path(exporter.gerberdir) / name).write_bytes(contents)

        exporter.generate_geber = partial(write_plot, "copper.gbr", b"variant copper\n")
        exporter.generate_excellon = partial(
            write_plot, "drill.drl", b"variant drill\n"
        )
        exporter.fill_zones = lambda: []
        dialog.settings["hooks"] = {"post_script": str(hook)}
        dialog.settings["gerber"]["force_drc"] = False
        dialog.settings["general"]["order_number"] = False

        dialog.generate_fabrication_data()

        assert (tmp_path / "hook-plots.txt").read_bytes() == (
            b"variant copper\nvariant drill\n"
        )
        working = Path((tmp_path / "hook-dir.txt").read_text(encoding="utf-8"))
        assert working != original_gerbers
        assert not working.parent.exists()
        assert Path(exporter.gerberdir) == original_gerbers
        assert (original_gerbers / "copper.gbr").read_bytes() == b"previous copper"
        assert exporter.output_snapshot is None
        assert not ui.controller.session.generating
        assert dialog.generate_button.IsEnabled()
        assert ui.controller.view._mutations_enabled
        assert ui.cache.get_generation_count() == 1
        public = exporter.get_artifact_paths()
        with ZipFile(public["gerber_zip"]) as archive:
            assert archive.read("copper.gbr") == b"variant copper\n"
            assert archive.read("drill.drl") == b"variant drill\n"
        assert _read_csv(public["bom_csv"])[0]["LCSC"] == "C999"
        assert _read_csv(public["cpl_csv"])[0]["Designator"] == "R1"
        assert len(ui.messages) == hook_exit
        if hook_exit:
            assert "post-generate hook failed" in ui.messages[0].lower()

    window_ui.run(check)


@pytest.mark.parametrize("publish", [False, True], ids=["abort", "publish"])
def test_staging_cleanup_failure_releases_generation_for_retry(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    publish: bool,
) -> None:
    """Cleanup failure neither traps the session nor invalidates published outputs."""
    exporter = runtime.exporter
    original_gerbers = exporter.gerberdir
    begin(runtime)
    working = Path(next(iter(exporter.get_staged_artifact_paths().values()))).parent
    destinations = tuple(Path(path) for path in exporter.get_artifact_paths().values())
    for path in destinations:
        path.write_bytes(b"previous")
    stage_artifacts(exporter)
    original_cleanup = TemporaryDirectory.cleanup

    def fail_staging_cleanup(directory: TemporaryDirectory) -> None:
        if Path(directory.name) == working:
            raise OSError("staging cleanup denied")
        original_cleanup(directory)

    with monkeypatch.context() as patch:
        patch.setattr(TemporaryDirectory, "cleanup", fail_staging_cleanup)
        if publish:
            exporter.publish_generation()
        exporter.abort_generation()
    assert exporter.output_snapshot is None
    assert str(working) in caplog.text
    assert exporter.gerberdir == original_gerbers
    assert exporter.get_staged_artifact_paths() == exporter.get_artifact_paths()
    assert {path.read_bytes() for path in destinations} == {
        b"new" if publish else b"previous"
    }
    begin(runtime)
    stage_artifacts(exporter, b"retry")
    exporter.publish_generation()
    assert {path.read_bytes() for path in destinations} == {b"retry"}
    exporter.abort_generation()


@pytest.mark.parametrize(
    "stem,variant,label",
    [
        ("board", "", ""),
        ("board", "Default", "Default"),
        ("board", "Production-" + "x" * 40, "Production-" + "x" * 40),
        ("board", 'Build<>:"/\\|?*\x00\t\nTest', "Build-Test"),
        ("board", "試作", "試作"),
        ("board", "e\u0301", "é"),
        ("電路" * 35, "A", "A"),
        ("b" * 220, "試作", "試作"),
        ("b" * 230, "ABCD", "ABCD"),
    ],
)
def test_artifact_names_keep_complete_readable_board_and_variant_names(
    runtime: SimpleNamespace, stem: str, variant: str, label: str
) -> None:
    """Filename formatting needs no plotting or publication for each text input."""
    runtime.exporter.filename = f"{stem}.kicad_pcb"
    for prefix, extension in (("BOM", "csv"), ("CPL", "csv"), ("GERBER", "zip")):
        suffix = f"--variant-{label}" if variant else ""
        name = runtime.exporter._artifact_name(prefix, extension, variant)
        assert name == f"{prefix}-{stem}{suffix}.{extension}"
        assert len(name.encode("utf-8")) <= 255
    if len(stem) == 230:
        assert len(name.encode("utf-8")) == 255


@pytest.mark.parametrize(
    "variants,selected,stem,error",
    [
        (("A/B", "A:B"), "A/B", "board", "collid"),
        (("A", "a"), "A", "board", "collid"),
        (("é", "e\u0301"), "é", "board", "collid"),
        (("ΐ", "Ϊ\u0301"), "ΐ", "board", "collid"),
        (("/",), "/", "board", "empty"),
        (("試" * 80,), "試" * 80, "board", "255"),
        (("ABCDE",), "ABCDE", "b" * 230, "255"),
    ],
    ids=[
        "unsafe",
        "case",
        "unicode",
        "casefold-normalization",
        "empty",
        "utf8",
        "gerber",
    ],
)
def test_invalid_artifact_names_are_rejected_before_staging(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    variants: tuple[str, ...],
    selected: str,
    stem: str,
    error: str,
) -> None:
    """All invalid name classes fail before allocating output resources."""
    runtime.exporter.filename = f"{stem}.kicad_pcb"
    directory = MagicMock()
    monkeypatch.setattr(runtime.modules.fabrication, "TemporaryDirectory", directory)
    with pytest.raises(ValueError, match=error) as failure:
        begin(runtime, variants=("", *variants), variant_name=selected)
    directory.assert_not_called()
    assert runtime.exporter.output_snapshot is None
    assert not list(Path(runtime.exporter.outputdir).iterdir())
    if error == "collid":
        assert all(repr(name) in str(failure.value) for name in variants)


def test_rejected_name_preserves_published_outputs_and_allows_another_variant(
    runtime: SimpleNamespace,
) -> None:
    """Default and unrelated named outputs remain usable despite catalog conflicts."""
    exporter = runtime.exporter
    variants = ("", "A", "a", "A/B", "A:B", "/", "試" * 80, "B")
    previous: dict[Path, bytes] = {}
    for selected in ("", "B"):
        begin(runtime, variants=variants, variant_name=selected)
        marker = selected.encode() or b"Default"
        stage_artifacts(exporter, marker)
        exporter.publish_generation()
        exporter.abort_generation()
        paths = exporter.get_artifact_paths()
        suffix = f"--variant-{selected}" if selected else ""
        assert {Path(path).name for path in paths.values()} == {
            f"{prefix}-board{suffix}.{ext}"
            for prefix, ext in (("BOM", "csv"), ("CPL", "csv"), ("GERBER", "zip"))
        }
        previous.update((Path(path), marker) for path in paths.values())
        assert len(previous) == (3 if selected == "" else 6)
        gerberdir = exporter.gerberdir
        with pytest.raises(ValueError, match="collid"):
            begin(runtime, variants=variants, variant_name="A")
        assert exporter.output_snapshot is None and exporter.variant_name == selected
        assert (
            exporter.gerberdir == gerberdir and exporter.get_artifact_paths() == paths
        )
        assert {
            path: path.read_bytes() for path in Path(exporter.outputdir).iterdir()
        } == previous


@pytest.mark.parametrize(
    "method", ["generate_bom", "generate_cpl", "generate_geber", "zip_gerber_excellon"]
)
def test_expired_named_context_never_falls_back_to_base(
    runtime: SimpleNamespace, method: str
) -> None:
    """A finished/aborted named operation cannot write Default data to its paths."""
    begin(runtime)
    runtime.exporter.abort_generation()
    with pytest.raises(RuntimeError, match="requires a new generation snapshot"):
        getattr(runtime.exporter, method)()


@pytest.fixture
def plotted_source(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Exercise snapshot orchestration with simulated board I/O and project state."""
    begin(runtime)
    live = runtime.exporter.board
    live.current_variant = "B"
    live.GetProject = lambda: "live project"
    source = SimpleNamespace(
        data=b"serialized native board", properties={"REVISION": "1"}
    )
    clone = SimpleNamespace(current_variant="", filename="temporary", project=None)
    clone.SetCurrentVariant = lambda name: setattr(clone, "current_variant", name)
    clone.GetCurrentVariant = lambda: clone.current_variant
    clone.SetFileName = lambda name: setattr(clone, "filename", name)

    def set_project(project: Any, reference_only: bool) -> None:
        assert reference_only is True
        clone.project = project

    clone.SetProject = set_project
    clone.SynchronizeProperties = lambda: setattr(
        clone, "properties", dict(source.properties)
    )
    clone.GetProperties = lambda: clone.properties

    monkeypatch.setattr(runtime.exporter, "_board_content", lambda: source.data)
    source.owner = MagicMock()
    monkeypatch.setattr(
        runtime.modules.fabrication.Fabrication, "_own_board_copy", source.owner
    )
    pcbnew = SimpleNamespace(
        PCB_IO_KICAD_SEXPR=lambda: SimpleNamespace(
            DoLoad=lambda *_args: clone,
        ),
        STRING_LINE_READER=lambda content, path: (content, path),
    )
    monkeypatch.setattr(
        runtime.modules.fabrication, "import_module", lambda _name: pcbnew
    )
    assert runtime.exporter._get_plot_board() is clone
    source.clone = clone
    return source


def test_plot_clone_preserves_live_variant_and_project_context(
    runtime: SimpleNamespace, plotted_source: SimpleNamespace
) -> None:
    """Serialization plots the explicit output while the live editor stays on B."""
    clone = plotted_source.clone
    live = runtime.exporter.board
    plotted_source.owner.assert_called_once_with(clone)
    assert clone.current_variant == "A"
    assert clone.filename == live.GetFileName()
    assert clone.project == "live project"
    assert clone.GetProperties() == {"REVISION": "1"}
    assert live.current_variant == "B"
    runtime.exporter.abort_generation()


def test_modified_write_is_rejected_before_native_parsing(
    runtime: SimpleNamespace,
    plotted_source: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expected hash comes from the formatter, not a damaged file on disk."""
    destination = tmp_path / "copy.kicad_pcb"
    write = Path.write_bytes

    def corrupt_write(path: Path, content: bytes) -> int:
        return write(path, content + b"corruption")

    monkeypatch.setattr(Path, "write_bytes", corrupt_write)
    digest = runtime.exporter._serialize_board(destination)
    parser = MagicMock(side_effect=AssertionError("Damaged bytes reached KiCad"))
    monkeypatch.setattr(runtime.modules.fabrication, "import_module", parser)
    with pytest.raises(RuntimeError, match="Temporary board changed"):
        runtime.exporter._load_board_copy(destination, digest)
    parser.assert_not_called()


def test_native_parser_receives_the_verified_buffer_after_file_replacement(
    runtime: SimpleNamespace,
    plotted_source: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the path after verification cannot swap the parser's input."""
    destination = tmp_path / "copy.kicad_pcb"
    plotted_source.data = "board with Unicode µΩ".encode()
    digest = runtime.exporter._serialize_board(destination)
    pcbnew = runtime.modules.fabrication.import_module("pcbnew")

    def make_reader(content: str, path: str) -> str:
        Path(path).write_bytes(b"invalid replacement")
        return content

    parser = MagicMock(return_value=plotted_source.clone)
    pcbnew.STRING_LINE_READER = make_reader
    pcbnew.PCB_IO_KICAD_SEXPR = lambda: SimpleNamespace(DoLoad=parser)
    runtime.exporter._load_board_copy(destination, digest)
    parser.assert_called_once_with(
        plotted_source.data.decode("utf-8"), None, None, None, 0
    )


@pytest.mark.parametrize(
    "change,error",
    [
        ("routing", "manufacturing data changed after plotting"),
        ("variables", "Project text variables changed"),
    ],
)
def test_changed_plot_source_preserves_previous_artifacts(
    runtime: SimpleNamespace, plotted_source: SimpleNamespace, change: str, error: str
) -> None:
    """Publication checks live serialization and project substitutions against capture."""
    public = runtime.exporter.get_artifact_paths()
    for path in public.values():
        Path(path).write_bytes(b"previous")
    stage_artifacts(runtime.exporter)
    if change == "routing":
        plotted_source.data = b"updated routing"
    else:
        plotted_source.properties = {"REVISION": "2"}
    with pytest.raises(RuntimeError, match=error):
        runtime.exporter.publish_generation()
    assert {Path(path).read_bytes() for path in public.values()} == {b"previous"}
    runtime.exporter.abort_generation()


def test_split_designator_rows_are_not_value_conflicts(
    runtime: SimpleNamespace,
) -> None:
    """Splitting a large identical group must not trigger a plausibility warning."""
    references = [f"R{index:05d}" for index in range(400)]
    runtime.footprints[:] = [
        make_footprint(ref, 0, 0, Point(10, 20)) for ref in references
    ]
    begin(runtime, *(part(ref) for ref in references))
    assert len(runtime.exporter.output_snapshot.bom_rows) > 1
    assert runtime.exporter.get_part_consistency_warnings() == ""
    runtime.exporter.abort_generation()


@pytest.mark.parametrize(
    "rollback_fails", [False, True], ids=["restored", "recovery-retained"]
)
@pytest.mark.parametrize("previous_bom", [False, True], ids=["new-bom", "prior-bom"])
def test_partial_publication_restores_outputs_or_retains_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_fails: bool,
    previous_bom: bool,
) -> None:
    """Second-file failure either restores prior outputs or identifies recovery copies."""
    pairs = []
    for name in ("BOM", "CPL", "GERBER"):
        source, destination = tmp_path / f"new-{name}", tmp_path / name
        source.write_bytes(b"new")
        if previous_bom or name != "BOM":
            destination.write_bytes(b"previous")
        pairs.append((source, destination))
    original = fabrication_archive.os.replace

    def fail_second(source: Any, destination: Any) -> None:
        if Path(source).name == "new-CPL" or (
            rollback_fails and Path(source).name == "0"
        ):
            raise OSError("publication denied")
        original(source, destination)

    monkeypatch.setattr(fabrication_archive.os, "replace", fail_second)
    if rollback_fails and not previous_bom:
        monkeypatch.setattr(
            Path, "unlink", MagicMock(side_effect=OSError("compensation denied"))
        )
    error = RuntimeError if rollback_fails else OSError
    with pytest.raises(
        error, match="Recovery copies:" if rollback_fails else "publication denied"
    ) as failure:
        fabrication_archive.publish_artifact_set(tuple(pairs))
    if rollback_fails:
        assert str(tmp_path / "BOM") in str(failure.value)
        (recovery,) = tmp_path.glob(".jlcpcb-recovery-*")
        if previous_bom:
            assert (recovery / "0").read_bytes() == b"previous"
        else:
            assert (tmp_path / "BOM").read_bytes() == b"new"
            assert not (recovery / "0").exists()
            assert (recovery / "1").read_bytes() == b"previous"
    else:
        assert (tmp_path / "BOM").exists() is previous_bom
        if previous_bom:
            assert (tmp_path / "BOM").read_bytes() == b"previous"
        assert not list(tmp_path.glob(".jlcpcb-recovery-*"))
    assert [destination.read_bytes() for _, destination in pairs[1:]] == [
        b"previous"
    ] * 2


@pytest.mark.parametrize("fail_publication", [False, True])
def test_recovery_cleanup_preserves_publication_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_publication: bool,
) -> None:
    """Recovery-file cleanup does not hide either success or the original failure."""
    source = tmp_path / "new-BOM"
    source.write_bytes(b"new")
    destination = tmp_path / "BOM"
    destination.write_bytes(b"previous")

    def fail_cleanup(path: Path) -> None:
        raise OSError("recovery cleanup denied")

    def fail_replace(source: Any, destination: Any) -> None:
        raise OSError("publication denied")

    monkeypatch.setattr(fabrication_archive.shutil, "rmtree", fail_cleanup)
    if fail_publication:
        monkeypatch.setattr(fabrication_archive.os, "replace", fail_replace)
        with pytest.raises(OSError, match="publication denied"):
            fabrication_archive.publish_artifact_set(((source, destination),))
    else:
        fabrication_archive.publish_artifact_set(((source, destination),))
    assert destination.read_bytes() == (b"previous" if fail_publication else b"new")
    (recovery,) = tmp_path.glob(".jlcpcb-recovery-*")
    assert (recovery / "0").read_bytes() == b"previous"
    assert str(recovery) in caplog.text


_requires_native_plotting = pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native KiCad plotting requires an explicitly enabled desktop session",
)


def _plot(
    pcbnew: ModuleType,
    board: Any,
    directory: Path,
    suffix: str,
    file_format: Any,
    *,
    crossout: bool = True,
) -> str:
    """Render one actual fabrication layer for text and geometry assertions."""
    controller = pcbnew.PLOT_CONTROLLER(board)
    options = controller.GetPlotOptions()
    options.SetOutputDirectory(str(directory))
    options.SetPlotFrameRef(False)
    options.SetPlotValue(True)
    options.SetPlotReference(True)
    options.SetCrossoutDNPFPsOnFabLayers(crossout)
    options.SetSketchDNPFPsOnFabLayers(False)
    options.SetHideDNPFPsOnFabLayers(False)
    controller.SetLayer(pcbnew.F_Fab)
    try:
        assert controller.OpenPlotfile(suffix, file_format)
        assert controller.PlotLayer()
        path = Path(controller.GetPlotFileName())
    finally:
        controller.ClosePlot()
    return path.read_text(encoding="utf-8")


def _svg_content(text: str) -> str:
    """Ignore only the output filename and timestamp in the SVG document title."""
    return re.sub(r"<title>.*?</title>", "", text)


def _gerber_content(text: str) -> str:
    """Normalize creation times while preserving project metadata and geometry."""
    # KiCad emits the current time in GERBER_PLOTTER::StartPlot and in
    # GbrMakeCreationDateAttributeString, so identical plots can cross a second.
    text = re.sub(
        r"(?m)^(G04 Created by KiCad \([^\n]*\) date )[^\n*]+(\*)$",
        r"\1<TIMESTAMP>\2",
        text,
    )
    return re.sub(
        r"(?m)^((?:%|G04 #@! )TF\.CreationDate,)[^*\n]*(\*%?)$",
        r"\1<TIMESTAMP>\2",
        text,
    )


@pytest.mark.native_kicad
@_requires_native_plotting
def test_native_variant_text_project_variables_and_dnp_marks(
    native_runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """A DNP assembly substitutes its fields and gains marks absent from Default."""
    plot = partial(_plot, native_runtime.pcbnew, native_runtime.board, tmp_path)
    svg = native_runtime.pcbnew.PLOT_FORMAT_SVG
    default = plot("default", svg)
    assert _svg_content(default) == _svg_content(
        plot("default-no-cross", svg, crossout=False)
    )
    assert ">BASEVALUE</text>" in default and ">C123</text>" in default
    native_runtime.board.SetCurrentVariant("A")
    variant = plot("variant", svg)
    assert _svg_content(variant) != _svg_content(
        plot("variant-no-cross", svg, crossout=False)
    )
    assert ">VARIANTVALUE</text>" in variant and ">C999</text>" in variant
    assert ">A; DNP assembly; PROJECT-TOKEN</text>" in variant


@pytest.mark.native_kicad
@_requires_native_plotting
def test_native_serialization_and_production_plot_clone_preserve_rendering(
    native_runtime: SimpleNamespace, tmp_path: Path
) -> None:
    """Real save/load preserves plots while production isolation keeps B selected."""
    pcbnew, board = native_runtime.pcbnew, native_runtime.board
    board.SetCurrentVariant("A")
    svg = _plot(pcbnew, board, tmp_path, "direct", pcbnew.PLOT_FORMAT_SVG)
    gerber = _plot(pcbnew, board, tmp_path, "direct", pcbnew.PLOT_FORMAT_GERBER)
    board.SetCurrentVariant("B")
    source_path = tmp_path / "serialized.kicad_pcb"
    io = pcbnew.PCB_IO_MGR
    io.Save(io.KICAD_SEXP, str(source_path), board)
    first_serialization = source_path.read_bytes()
    io.Save(io.KICAD_SEXP, str(source_path), board)
    assert source_path.read_bytes() == first_serialization
    session = native_runtime.session
    session.set_output_variant("A")
    snapshot, name = session.begin_generation()
    exporter = native_runtime.fabrication.Fabrication(
        SimpleNamespace(settings={}), board
    )
    try:
        exporter.begin_generation(snapshot, name, (), session.validate_generation)
        clone = exporter._get_plot_board()
        assert str(clone.GetCurrentVariant()) == "A"
        assert str(board.GetCurrentVariant()) == "B"
        assert _svg_content(
            _plot(pcbnew, clone, tmp_path, "clone", pcbnew.PLOT_FORMAT_SVG)
        ) == _svg_content(svg)
        assert _gerber_content(
            _plot(pcbnew, clone, tmp_path, "clone", pcbnew.PLOT_FORMAT_GERBER)
        ) == _gerber_content(gerber)
        session.validate_generation()
        io.Save(io.KICAD_SEXP, str(source_path), board)
        assert source_path.read_bytes() == first_serialization
    finally:
        exporter.abort_generation()
        session.end_generation()


def _read_csv(path: str) -> list[dict[str, str]]:
    """Read the public artifact independently from the exporter's in-memory rows."""
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.native_kicad
@_requires_native_plotting
def test_native_exports_publish_each_variant_with_shared_physical_corrections(
    native_runtime: SimpleNamespace,
) -> None:
    """Native snapshots publish nine coexisting files without selecting output variants."""
    board = native_runtime.board
    board.SetCurrentVariant("A")
    session = native_runtime.session
    exporter = native_runtime.fabrication.Fabrication(
        SimpleNamespace(settings={}), board
    )
    corrections = (
        native_runtime.correction_data.Correction("^BASEVALUE$", 180, (0.5, -0.25)),
        native_runtime.correction_data.Correction("^ROTATED$", 90, (0, 0)),
    )
    artifacts: dict[str, bytes] = {}
    placements = []
    try:
        for name in ("", "A", "B"):
            session.set_output_variant(name)
            snapshot, output_name = session.begin_generation()
            exporter.begin_generation(
                snapshot, output_name, corrections, session.validate_generation
            )
            exporter.generate_geber(2)
            exporter.generate_excellon()
            exporter.zip_gerber_excellon()
            exporter.generate_cpl()
            exporter.generate_bom()
            paths = exporter.get_artifact_paths()
            exporter.publish_generation()
            bom, cpl = _read_csv(paths["bom_csv"]), _read_csv(paths["cpl_csv"])
            assert len(bom) == len(cpl) == (0 if name == "A" else 1)
            if cpl:
                expected = ("ROTATED", "C777") if name == "B" else ("BASEVALUE", "C123")
                assert (bom[0]["Comment"], bom[0]["LCSC"]) == expected
                assert cpl[0]["Package"] == "SOT-23"
                placement = tuple(
                    float(cpl[0][field]) for field in ("Mid X", "Mid Y", "Rotation")
                )
                assert placement == (30.5, -29.75, 180.0)
                placements.append(placement)
            with ZipFile(paths["gerber_zip"]) as archive:
                assert archive.testzip() is None
                assert any("JLC_FAB" in name for name in archive.namelist())
            assert str(board.GetCurrentVariant()) == "A"
            session.validate_generation()
            exporter.abort_generation()
            session.end_generation()
            assert not set(paths.values()).intersection(artifacts)
            assert all(
                Path(path).read_bytes() == data for path, data in artifacts.items()
            )
            artifacts.update((path, Path(path).read_bytes()) for path in paths.values())
        assert len(artifacts) == 9
        assert len(placements) == 2 and placements[0] == placements[1]
        assert all(artifacts.values())
        assert not list(Path(exporter.outputdir).glob(".jlcpcb-generation-*"))
    finally:
        exporter.abort_generation()
        session.end_generation()
