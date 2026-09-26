"""Check runner safety and reporting, not native KiCad or GTK behavior."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


def runner() -> Any:
    """Import lazily so every missing harness contract is a distinct failure."""
    return importlib.import_module("scripts.linux_native_impedance_smoke")


def test_plan_covers_native_layers_connectors_dialogs_and_persistence() -> None:
    """Do not allow a CLI-only subset to masquerade as the whole smoke run."""
    stages = runner().STAGES
    assert stages[0] == "prerequisites"
    assert {"dialogs", "workflow", "generate", "custom-palette"}.issubset(stages)
    assert {"single-ended-50-ohm", "usb-differential-90-ohm"}.issubset(stages)
    for name in runner().CASES:
        case, source = runner().fixture(name)
        assert source.is_file()
        assert source.with_suffix(".kicad_pro").is_file()
        assert source.with_suffix(".kicad_dru").is_file()
        assert {c.length_mm for c in case.circuits} >= {2, 120, 150}
        assert {c.coplanar for c in case.circuits} == {False, True}
        assert {leg.layer for c in case.circuits for leg in c.legs} >= {
            "F.Cu",
            "In2.Cu",
            "B.Cu",
        }


@pytest.mark.parametrize("name", ["single-ended-50-ohm", "usb-differential-90-ohm"])
def test_native_plan_uses_all_combined_circuits_and_keeps_each_pair_together(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Substitute only the native snapshot; exercise production row matching."""
    from impedance import pcbnew_adapter

    case, _source = runner().fixture(name)
    snapshot = case.snapshot()
    monkeypatch.setattr(pcbnew_adapter, "snapshot_board", lambda *_args: snapshot)
    plan = runner().class_plan(case, object(), object())
    assert len(plan.config.specifications) == 2
    assert {
        trace.trace_id for section in plan.sections for trace in section.traces
    } == {trace.trace_id for trace in snapshot.traces}
    expected_nets = {tuple(sorted(circuit.net_names)) for circuit in case.circuits}
    assert {
        tuple(sorted(section.net_names)) for section in plan.sections
    } == expected_nets


def test_preview_waits_for_paint_and_display_callbacks() -> None:
    """A native selection's queued image is not equivalent to a completed view."""
    events: list[str] = []
    pane = SimpleNamespace(ready=False, failure="")
    canvas = SimpleNamespace(
        _bitmap=None,
        IsShownOnScreen=lambda: True,
        Update=lambda: events.append("update"),
    )
    pane.canvas = canvas

    def dispatch() -> None:
        events.append("yield")
        if events.count("yield") == 1:
            canvas._bitmap = SimpleNamespace(IsOk=lambda: True)
        else:
            pane.ready = True

    runner().wait_for_preview(SimpleNamespace(Yield=dispatch), pane)
    assert events == ["yield", "update", "yield", "update"]


@pytest.mark.parametrize("failure", ["pending", "failed", "hidden", "bitmap"])
def test_preview_wait_rejects_incomplete_or_invisible_images(failure: str) -> None:
    """Fail boundedly rather than approving queued, failed, or hidden pixels."""
    pane = SimpleNamespace(
        ready=failure in ("hidden", "bitmap"),
        failure="capture failed" if failure == "failed" else "",
        canvas=SimpleNamespace(
            _bitmap=SimpleNamespace(IsOk=lambda: failure != "bitmap"),
            IsShownOnScreen=lambda: failure != "hidden",
        ),
    )
    with pytest.raises(RuntimeError, match="display|failed|bitmap"):
        runner().wait_for_preview(SimpleNamespace(), pane, timeout=0)


@pytest.mark.parametrize("padding", [8, 16])
def test_native_capture_check_requires_requested_sixteen_pixel_padding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, padding: int
) -> None:
    """Synthetic pixels check the harness's geometry policy, not native drawing."""
    from impedance import render
    from tests.test_impedance_report_pipeline import _png

    raw = bytearray(b"\x10\x20\x30" * 800 * 420)
    for index in range(20):
        raw[index * 3 : index * 3 + 3] = bytes((index, 60, 100))
    left, top, right, bottom = (
        100 - padding,
        100 - padding,
        600 + padding,
        300 + padding,
    )
    for x in range(left, right + 1):
        for y in (top, bottom):
            raw[(y * 800 + x) * 3 : (y * 800 + x) * 3 + 3] = b"\xff\xff\x00"
    for y in range(top, bottom + 1):
        for x in (left, right):
            raw[(y * 800 + x) * 3 : (y * 800 + x) * 3 + 3] = b"\xff\xff\x00"
    trace = SimpleNamespace(points=((100, 100), (600, 300)), trace_id="trace")
    section = SimpleNamespace(
        bounds=(100, 100, 600, 300),
        traces=(trace,),
        layer="F.Cu",
        net_names=("RF",),
        width_nm=180000,
    )
    monkeypatch.setattr(render, "board_outline_bounds", lambda _board: (0, 0, 800, 420))
    monkeypatch.setattr(
        render,
        "section_viewport",
        lambda *_args, **_kwargs: SimpleNamespace(left=0, top=0, width=800, height=420),
    )
    path = tmp_path / "capture.png"
    path.write_bytes(_png(800, 420))
    wx = SimpleNamespace(
        BITMAP_TYPE_PNG=1,
        Image=lambda *_args: SimpleNamespace(IsOk=lambda: True, GetData=lambda: raw),
    )
    if padding == 16:
        assert runner().check_png(path, section, object(), wx)["yellow_bounds_px"] == (
            left,
            top,
            right,
            bottom,
        )
    else:
        with pytest.raises(RuntimeError, match="frame the complete native section"):
            runner().check_png(path, section, object(), wx)


@pytest.mark.parametrize("kind", ["existing", "symlink", "source", "root"])
def test_output_guard_preserves_existing_and_source_data(
    tmp_path: Path, kind: str
) -> None:
    """Only a new artifact directory may be claimed by the runner."""
    api = runner()
    victim = tmp_path / "retained"
    victim.mkdir()
    marker = victim / "keep"
    marker.write_bytes(b"original")
    output = victim
    if kind == "symlink":
        output = tmp_path / "link"
        output.symlink_to(victim, target_is_directory=True)
    elif kind == "source":
        output = api.ROOT / "uncreated-smoke-output"
    elif kind == "root":
        output = Path("/")
    with pytest.raises(ValueError):
        api.claim_output(output)
    assert marker.read_bytes() == b"original"
    assert not (api.ROOT / "uncreated-smoke-output").exists()


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing", "false_pass"])
def test_native_stage_failures_never_become_skips_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failed/malformed child stops later stages and retains actionable logs."""
    api = runner()
    calls = []

    def process(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        assert arguments[0] == sys.executable
        assert kwargs["timeout"] == 12
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 12, output="native stalled")
        if failure == "false_pass":
            output = Path(arguments[arguments.index("--output") + 1])
            output.mkdir()
            (output / "result.json").write_text(
                json.dumps({"status": "PASS", "stage": "wrong-stage"}), encoding="utf-8"
            )
        return subprocess.CompletedProcess(
            arguments, 3 if failure == "exit" else 0, "diagnostic", ""
        )

    monkeypatch.setattr(api.subprocess, "run", process)
    output = tmp_path / "run"
    assert api.run(output, timeout=12) == 1
    report = json.loads((output / "summary.json").read_text())
    assert report["status"] == "FAIL"
    assert report["stages"][0]["status"] == "FAIL"
    assert all(item["status"] == "NOT_RUN" for item in report["stages"][1:])
    assert len(calls) == 1
    assert (output / "prerequisites.log").is_file()


def test_success_requires_every_exact_stage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete synthetic orchestration run reports its non-native test evidence."""
    api = runner()

    def process(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        output = Path(arguments[arguments.index("--output") + 1])
        stage = arguments[arguments.index("--worker") + 1]
        output.mkdir()
        (output / "result.json").write_text(
            json.dumps(
                {"stage": stage, "status": "PASS", "evidence": {"test_double": True}}
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0, "stage finished", "")

    monkeypatch.setattr(api.subprocess, "run", process)
    output = tmp_path / "run"
    assert api.run(output, timeout=12) == 0
    report = json.loads((output / "summary.json").read_text())
    assert report["status"] == "PASS"
    assert tuple(item["stage"] for item in report["stages"]) == api.STAGES
    assert all(item["evidence"]["test_double"] for item in report["stages"])


@pytest.mark.parametrize("timeout", [0, -1, 3_601])
def test_invalid_timeout_does_not_create_artifacts(
    tmp_path: Path, timeout: int
) -> None:
    """The process watchdog must be a positive bounded duration."""
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="timeout"):
        runner().run(output, timeout=timeout)
    assert not output.exists()


def test_prerequisites_reject_non_linux_before_native_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the script on the host cannot count as Linux acceptance."""
    api = runner()
    monkeypatch.setattr(api.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Linux"):
        api.native_runtime()


@pytest.mark.parametrize(
    ("binding_version", "cli_version", "error"),
    [
        ("(10.0.6)", "10.0.6", None),
        ("(10.0.7)", "10.0.7", None),
        ("10.0.7-10.0.7~ubuntu24.04.1", "10.0.7", None),
        ("10.0.10", "10.0.10", None),
        ("10.0.6", "10.0.7", "do not match"),
        ("10.0.7", "10.0.6", "do not match"),
        ("9.0.6", "9.0.6", "Unsupported pcbnew"),
        ("11.0.0", "11.0.0", "Unsupported pcbnew"),
        ("10.1.0", "10.1.0", "Unsupported pcbnew"),
        ("10.0.6", "11.0.0", "Unsupported KiCad CLI"),
    ],
)
def test_prerequisites_accept_matching_stable_patch_releases(
    monkeypatch: pytest.MonkeyPatch,
    binding_version: str,
    cli_version: str,
    error: Optional[str],
) -> None:
    """The existing stable PPA may advance patches; CLI and bindings must agree."""
    api = runner()
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("KICAD_CLI", "/usr/bin/kicad-cli")
    app = object()
    native = SimpleNamespace(
        GetBoard=lambda: None,
        GetBuildVersion=lambda: binding_version,
        BOARD=SimpleNamespace(__swig_destroy__=lambda _board: None),
        **dict.fromkeys(
            (
                "PCB_IO_KICAD_SEXPR",
                "STRING_FORMATTER",
                "PLOT_CONTROLLER",
                "ZONE_FILLER",
            ),
            lambda: None,
        ),
    )
    wx = ModuleType("wx")
    wx.PlatformInfo = ("gtk3",)  # type: ignore[attr-defined]
    wx.version = lambda: "4.2.0 gtk3"  # type: ignore[attr-defined]
    for name, module in (
        ("_pcbnew", SimpleNamespace(__file__="/usr/lib/_pcbnew.so")),
        ("pcbnew", native),
        ("wx", wx),
        ("wx.svg", ModuleType("wx.svg")),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(api, "checked_app", lambda _wx: (app, (1280, 900)))
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, cli_version + "\n", ""
        ),
    )
    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            api.native_runtime()
    else:
        actual_native, actual_wx, actual_app, evidence = api.native_runtime()
        assert (actual_native, actual_wx, actual_app) == (native, wx, app)
        assert evidence["pcbnew"] == binding_version
        assert evidence["cli_version"] == cli_version


@pytest.mark.parametrize("corruption", ["none", "swapped", "external", "row"])
def test_workbook_images_are_exactly_the_native_row_images(
    tmp_path: Path, corruption: str
) -> None:
    """Actual XLSX image relationships cannot claim success on counts alone."""
    from impedance.service import CapturedImage, ReportRow
    from impedance.workbook import write_workbook
    from tests.test_impedance_report_pipeline import _png

    images = tuple(tmp_path / f"capture-{index}.png" for index in (1, 2))
    for index, image in enumerate(images):
        image.write_bytes(_png(800, 420, color=bytes((index, 80, 160))))
    rows = [
        ReportRow(
            specification="USB CPWG",
            nets=("USB_D+", "USB_D-"),
            net_class="USB",
            copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
            signal_layer=layer,
            reference_layers=(reference,),
            kind="differential_coplanar",
            width_nm=270000,
            spacing_nm=203200,
            target_ohms="90",
            image=CapturedImage.load(image),
            ground_gap_nm=200000,
        )
        for (layer, reference), image in zip(
            (("F.Cu", "In1.Cu"), ("B.Cu", "In2.Cu")), images
        )
    ]
    document = write_workbook(rows, tmp_path / "report.xlsx")
    if corruption != "none":
        with ZipFile(document) as package:
            parts = {name: package.read(name) for name in package.namelist()}
        if corruption == "swapped":
            parts["xl/media/image1.png"] = images[1].read_bytes()
        elif corruption == "external":
            name = "xl/drawings/_rels/drawing1.xml.rels"
            tree = ET.fromstring(parts[name])  # noqa: S314 -- Locally generated XML.
            tree[0].set("TargetMode", "External")
            parts[name] = ET.tostring(tree)
        else:
            name = "xl/drawings/drawing1.xml"
            tree = ET.fromstring(parts[name])  # noqa: S314 -- Locally generated XML.
            row = tree.find(
                ".//{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}row"
            )
            assert row is not None
            row.text = "2"
            parts[name] = ET.tostring(tree)
        document = tmp_path / "changed.xlsx"
        with ZipFile(document, "w", compression=ZIP_DEFLATED) as package:
            for name, data in parts.items():
                package.writestr(name, data)
    if corruption == "none":
        assert runner().check_workbook_images(document, images) is None
    else:
        with pytest.raises(RuntimeError, match="image|anchor|external"):
            runner().check_workbook_images(document, images)


@pytest.mark.parametrize("failure", ["fill", "clear", "unload", "null_board"])
def test_native_failure_cleanup_releases_dependents_and_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Stateful resource doubles check ordering, not actual Linux binding behavior."""
    api = runner()
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"original native board")
    source.with_suffix(".kicad_pro").write_bytes(b"original native project")
    output = tmp_path / "output"
    output.mkdir()
    events = []
    project = object()

    class Board:
        thisown = False

        def GetProject(self) -> Any:
            return project

        def ClearProject(self) -> None:
            events.append("clear")
            if failure == "clear":
                raise RuntimeError("cleanup clear failed")

        def Zones(self) -> tuple[()]:
            return ()

    class Filler:
        def __init__(self, board: Any) -> None:
            pass

        def Fill(self, zones: Any) -> bool:
            if failure == "fill":
                raise RuntimeError("primary fill failure")
            return True

        def __del__(self) -> None:
            events.append("filler released")

    def unload(value: Any, save: bool) -> bool:
        assert value is project and save is False
        events.append("unload")
        if failure == "unload":
            raise RuntimeError("cleanup unload failed")
        return True

    native = SimpleNamespace(
        LoadBoard=lambda path: None if failure == "null_board" else Board(),
        BOARD=SimpleNamespace(__swig_destroy__=lambda board: events.append("destroy")),
        ZONE_FILLER=Filler,
        SaveBoard=lambda path, board: True,
        GetSettingsManager=lambda: SimpleNamespace(
            UnloadProject=unload, GetProject=lambda path: project
        ),
    )
    monkeypatch.setattr(api, "fixture", lambda name: (object(), source))
    expected = "no board" if failure == "null_board" else "primary"
    with (
        pytest.raises(RuntimeError, match=expected),
        api.loaded_fixture("fixture", output, native),
    ):
        raise RuntimeError("primary stage failure")
    assert events[-1] == "unload"
    if failure != "null_board":
        assert (
            events.index("filler released")
            < events.index("clear")
            < events.index("destroy")
            < events.index("unload")
        )
    assert source.read_bytes() == b"original native board"


def test_worker_destroys_app_after_a_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native-style app remains scoped even when loading the fixture fails."""
    api = runner()
    destroyed = []
    monkeypatch.setattr(api, "configure_palette", lambda *args: None)
    monkeypatch.setattr(
        api,
        "native_runtime",
        lambda: (
            object(),
            object(),
            SimpleNamespace(Destroy=lambda: destroyed.append(True)),
            {},
        ),
    )

    def failed(*args: Any) -> Any:
        raise RuntimeError("native load failed")

    monkeypatch.setattr(api, "loaded_fixture", failed)
    assert api.worker(api.CASES[0], tmp_path / "worker") == 1
    assert destroyed == [True]


def test_stock_buttons_are_looked_up_inside_the_intended_modal_dialog() -> None:
    """The static FindWindowById must not select a parent with the same stock ID."""
    wrong_parent_button, child_button = object(), object()
    child = SimpleNamespace(
        FindWindowById=lambda identity: wrong_parent_button,
        FindWindow=lambda identity: child_button,
    )
    assert runner().stock_button(child, 5101) is child_button


def test_display_failure_releases_the_app_created_before_validation() -> None:
    """Reject too-small Xvfb displays without leaving native app lifetime unbounded."""
    destroyed = []
    app = SimpleNamespace(
        SetAppName=lambda name: None, Destroy=lambda: destroyed.append(True)
    )
    wx = SimpleNamespace(
        App=lambda redirect: app,
        GetDisplaySize=lambda: SimpleNamespace(
            GetWidth=lambda: 640, GetHeight=lambda: 480
        ),
    )
    with pytest.raises(RuntimeError, match="1280x900"):
        runner().checked_app(wx)
    assert destroyed == [True]


@pytest.mark.parametrize("damaged", [False, True])
def test_yellow_border_requires_complete_edges_not_only_extreme_pixels(
    damaged: bool,
) -> None:
    """Missing middle-of-edge pixels must fail even with all four corners intact."""
    pixels = [b"\x00\x10\x23"] * (800 * 420)
    left, top, right, bottom = 20, 30, 220, 60
    for x in range(left, right + 1):
        for y in (top, bottom):
            pixels[y * 800 + x] = b"\xff\xff\x00"
    for y in range(top, bottom + 1):
        for x in (left, right):
            pixels[y * 800 + x] = b"\xff\xff\x00"
    if damaged:
        for x in range(115, 126):
            pixels[top * 800 + x] = b"\x00\x10\x23"
        with pytest.raises(RuntimeError, match="edge"):
            runner().check_yellow_edges(pixels, (left, top, right, bottom))
    else:
        assert runner().check_yellow_edges(pixels, (left, top, right, bottom)) is None


def test_storage_workflow_preserves_both_board_intents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check SQLite and report contents; native Generate is a separate stage."""
    from impedance import service
    from scripts.generate_rf_impedance_captures import (
        fixture_snapshot,
        reviewed_sections,
    )
    from tests.test_impedance_report_pipeline import _png

    api = runner()
    case, source = api.fixture("usb-differential-90-ohm")
    snapshot = fixture_snapshot(case, case.snapshot())
    config, sections = reviewed_sections(case, snapshot)
    plan = service.prepare(config, snapshot)
    assert plan is not None

    class FixtureRenderer:
        """Only the native PNG rendering boundary is substituted in this unit test."""

        def __init__(self, _board: Any, _module: Any) -> None:
            self.index = 0

        def render(
            self, section: Any, destination: Path, width_px: int, height_px: int
        ) -> Path:
            assert section == sections[self.index]
            self.index += 1
            destination.write_bytes(
                _png(width_px, height_px, color=bytes((self.index, 80, 160)))
            )
            return destination

    original_export = service.export_reports

    def nonnative_export(
        plan: Any, board: Any, module: Any, scratch: Path
    ) -> service.ExportArtifacts:
        return original_export(
            plan, board, module, scratch, renderer_factory=FixtureRenderer
        )

    monkeypatch.setattr(service, "export_reports", nonnative_export)
    result = api.workflow(
        plan, SimpleNamespace(GetFileName=lambda: str(source)), object(), tmp_path
    )
    assert result["distinct_board_intents_preserved"] is True


@pytest.mark.parametrize(
    "palette", ["selected", "default_background", "default_copper"]
)
def test_custom_palette_checks_the_canvas_under_translucent_copper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, palette: str
) -> None:
    """A valid viewport may be entirely covered by the selected layer's pour."""
    from impedance import render
    from tests.test_impedance_report_pipeline import _png

    # Native wxSVG's 60%-opaque custom copper over the chosen light background.
    # The whole-route viewport can be inside this plane; exposed canvas is not
    # guaranteed. Wrong canvas/copper must still fail acceptance.
    plane = b"\x6f\x7c\x88" if palette != "default_background" else b"\x0e\x20\x32"
    copper = b"\x17\x2b\x3d" if palette != "default_copper" else b"\xc8\x34\x34"
    raw = bytearray(plane * 800 * 420)
    for index in range(20):
        raw[index * 3 : index * 3 + 3] = bytes((index, 60, 100))
    raw[60:360] = copper * 100
    for x in range(84, 617):
        for y in (84, 316):
            raw[(y * 800 + x) * 3 : (y * 800 + x) * 3 + 3] = b"\xff\xff\x00"
    for y in range(84, 317):
        for x in (84, 616):
            raw[(y * 800 + x) * 3 : (y * 800 + x) * 3 + 3] = b"\xff\xff\x00"
    section = SimpleNamespace(
        bounds=(100, 100, 600, 300),
        traces=(SimpleNamespace(points=((100, 100), (600, 300)), trace_id="trace"),),
        layer="F.Cu",
        net_names=("RF",),
        width_nm=180000,
    )
    monkeypatch.setattr(render, "board_outline_bounds", lambda _board: (0, 0, 800, 420))
    monkeypatch.setattr(
        render,
        "section_viewport",
        lambda *_args, **_kwargs: SimpleNamespace(left=0, top=0, width=800, height=420),
    )
    image_path = tmp_path / "covered.png"
    image_path.write_bytes(_png(800, 420))
    wx = SimpleNamespace(
        BITMAP_TYPE_PNG=1,
        Image=lambda *_args: SimpleNamespace(IsOk=lambda: True, GetData=lambda: raw),
    )
    if palette == "selected":
        result = runner().check_png(image_path, section, object(), wx, custom=True)
        assert result["light_canvas_pixels"] == 0
        assert result["light_plane_pixels"] > 1000
    else:
        with pytest.raises(RuntimeError, match="Selected"):
            runner().check_png(image_path, section, object(), wx, custom=True)
