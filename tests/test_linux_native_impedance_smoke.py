"""Check runner safety and reporting, not native KiCad or GTK behavior."""

from contextlib import nullcontext
import ctypes
from dataclasses import asdict, replace
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tests.test_impedance_controls import harness

__all__ = ["harness"]


def runner() -> Any:
    """Import lazily so every missing harness contract is a distinct failure."""
    return importlib.import_module("scripts.linux_native_impedance_smoke")


@pytest.mark.parametrize("action_fails", [False, True])
def test_gallery_modal_waits_for_mapping_without_a_fixed_delay(
    action_fails: bool,
) -> None:
    """A callback queued before native mapping cannot interact or wait 250ms."""
    pending: list[Any] = []
    delays: list[int] = []
    events: list[str] = []

    class Timer:
        def __init__(self, milliseconds: int, callback: Any) -> None:
            delays.append(milliseconds)
            self.active = True
            pending.append(lambda: callback() if self.active else None)

        def Stop(self) -> None:
            self.active = False

    class Dialog:
        modal = False
        shown = False

        def IsModal(self) -> bool:
            return self.modal

        def IsShownOnScreen(self) -> bool:
            return self.shown

        def EndModal(self, _result: int) -> None:
            self.modal = False

        def ShowModal(self) -> int:
            self.modal = True
            pending.pop(0)()
            assert not events, "Action ran before the native window was mapped"
            self.shown = True
            while self.modal:
                pending.pop(0)()
            return 2

        def Destroy(self) -> None:
            events.append("destroy")

    dialog = Dialog()
    wx = SimpleNamespace(
        CallAfter=lambda callback: pending.append(callback),
        CallLater=Timer,
        ID_CANCEL=2,
    )

    def action() -> None:
        assert dialog.IsModal() and dialog.IsShownOnScreen()
        events.append("action")
        if action_fails:
            raise RuntimeError("gallery interaction failed")
        dialog.EndModal(2)

    if action_fails:
        with pytest.raises(RuntimeError, match="gallery interaction failed"):
            runner().modal(wx, dialog, action)
    else:
        assert runner().modal(wx, dialog, action) == 2
    assert events == ["action", "destroy"]
    assert delays and max(delays) <= 1


def test_gallery_modal_rejects_an_early_exit_and_disarms_its_callback() -> None:
    """An abandoned callback cannot later pretend an unvisited modal was tested."""
    pending: list[Any] = []
    acted: list[str] = []

    class Dialog:
        def ShowModal(self) -> int:
            return 2

        def Destroy(self) -> None:
            acted.append("destroy")

    wx = SimpleNamespace(CallAfter=lambda callback: pending.append(callback))
    with pytest.raises(RuntimeError, match="mapped"):
        runner().modal(wx, Dialog(), lambda: acted.append("action"))
    pending.pop()()
    assert acted == ["destroy"]


def test_gallery_modal_fails_boundedly_when_the_native_window_never_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing native surface fails instead of interacting with an unseen window."""
    api = runner()
    pending: list[Any] = []
    events: list[str] = []
    now = iter((0.0, 31.0))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(now))

    class Dialog:
        modal = False

        def IsModal(self) -> bool:
            return self.modal

        def IsShownOnScreen(self) -> bool:
            return False

        def EndModal(self, _result: int) -> None:
            self.modal = False
            events.append("cancel")

        def ShowModal(self) -> int:
            self.modal = True
            while self.modal:
                pending.pop(0)()
            return 2

        def Destroy(self) -> None:
            events.append("destroy")

    wx = SimpleNamespace(
        CallAfter=lambda callback: pending.append(callback), ID_CANCEL=2
    )
    with pytest.raises(RuntimeError, match="did not become visible"):
        api.modal(wx, Dialog(), lambda: events.append("unexpected action"))
    assert events == ["cancel", "destroy"]


@pytest.mark.parametrize("kind", ["workbook", "spacing"])
@pytest.mark.parametrize(
    "failure", ["source", "palette", "changed_source", "changed_palette"]
)
def test_dialog_harness_reuses_production_captures_and_checks_live_revision(
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    failure: str,
) -> None:
    """Probe the real harness callbacks, with only rendering and controls replaced."""
    from impedance import palette, pcbnew_adapter, render
    from impedance.service import CapturedImage, ExportPlan
    from tests.test_impedance_native_workflow import _spacing_fixture
    from tests.test_impedance_report_pipeline import _png

    config, snapshot, analysis = _spacing_fixture()
    package = harness.integration.__package__
    model = importlib.import_module(package + ".model")
    matching = importlib.import_module(package + ".matching")
    spacing = importlib.import_module(package + ".spacing_review")
    # The scoped integration module must receive its own immutable model types,
    # exactly as the loaded plugin does when its source fingerprint validates.
    config = model.Config.from_dict(config.to_dict())
    values = asdict(snapshot)
    values["traces"] = tuple(model.Trace(**asdict(trace)) for trace in snapshot.traces)
    snapshot = model.BoardSnapshot(**values)
    analysis = matching.analyze(config, snapshot)
    plan = ExportPlan(config, snapshot, analysis, analysis.sections)
    entry = (
        analysis.sections[0]
        if kind == "workbook"
        else spacing.spacing_review_entries(config, snapshot, analysis.sections)[0]
    )
    state = SimpleNamespace(
        snapshot=snapshot,
        palette="fixture-native-palette",
        source_error=False,
        palette_error=False,
        reads=0,
        evidence_reads=0,
        renders=[],
        renderers=[],
        checked=[],
    )
    board = SimpleNamespace(GetFileName=lambda: harness.board_path)
    pcbnew = SimpleNamespace(GetBoard=lambda: object())

    def read_source(candidate: Any, _native: Any) -> Any:
        """Read the owned board afresh even when the PNG already exists."""
        assert candidate is board
        state.reads += 1
        if state.source_error:
            raise RuntimeError("Fixture source is unavailable")
        return state.snapshot

    def read_palette(_native: Any) -> str:
        """Make an unreadable live palette distinct from its last cached value."""
        if state.palette_error:
            raise OSError("Fixture palette is unavailable")
        return state.palette

    source = importlib.import_module(package + ".source_evidence")

    def read_evidence(candidate: Any, _native: Any) -> Any:
        """Keep every currentness boundary fresh while reusing a structured snapshot."""
        assert candidate is board
        state.evidence_reads += 1
        if state.source_error:
            raise RuntimeError("Fixture source is unavailable")
        return source.SourceEvidence(state.snapshot.context_digest.encode())

    controls: list[Any] = []
    frames: list[Any] = []
    real_controls = harness.integration.ImpedanceControls

    def make_controls(parent: Any, toolbar: Any) -> Any:
        value = real_controls(parent, toolbar)
        controls.append(value)
        return value

    class Frame:
        """Preserve toolbar construction and native-owner cleanup state."""

        def __init__(self, _parent: Any, **_kwargs: Any) -> None:
            self.destroyed = False
            frames.append(self)

        def CreateToolBar(self) -> Any:
            return SimpleNamespace(
                AddControl=harness.toolbar_controls.append, Realize=lambda: True
            )

        def Destroy(self) -> None:
            self.destroyed = True

    class Renderer:
        """Retain real PNG bytes while counting native rasterization boundaries."""

        def __init__(
            self, source: Any, _native: Any, *, appearance: Any = None
        ) -> None:
            assert source is board
            self.closed = False
            self.close_calls = 0
            state.renderers.append(self)

        def render(
            self,
            section: Any,
            destination: Path,
            width_px: int = 800,
            height_px: int = 420,
        ) -> Path:
            assert not self.closed
            state.renders.append(section)
            destination.write_bytes(
                _png(width_px, height_px, color=bytes((len(state.renders), 80, 160)))
            )
            return destination

        def render_spacing(
            self, section: Any, destination: Path, width_px: int, height_px: int
        ) -> Path:
            return self.render(section, destination, width_px, height_px)

        def close(self) -> None:
            self.closed = True
            self.close_calls += 1

    class ProbeComplete(Exception):
        """Stop after callback inspection, without claiming native modal coverage."""

    def construct(
        _parent: Any,
        _config: Any,
        expected: Any,
        preview: Any,
        refresh_snapshot: Any,
        **options: Any,
    ) -> None:
        assert len(controls) == 1, "Dialog smoke bypassed the production controller"
        assert expected is snapshot
        prepared = options["prepare_capture"](snapshot, entry)
        assert isinstance(prepared, harness.integration.CapturedImage)
        first = preview(entry)
        repeated = (preview(entry), preview(entry))
        assert len(state.renders) == 1, (
            "Repeated review callbacks rerasterized one unchanged native image"
        )
        assert all(image.sha256 == first.sha256 for image in repeated)
        assert state.evidence_reads >= 3, "Cache hits did not validate the live source"
        assert state.reads == 1, "Unchanged review callbacks rebuilt their source"
        assert controls[0]._live_source_reader.statistics.hits >= 2
        assert config.review_tracking.spacing_images == ()
        refreshed = preview(entry, refresh=True)
        assert refreshed.sha256 != first.sha256 and len(state.renders) == 2
        assert (refreshed.width, refreshed.height) == (
            (800, 420) if kind == "workbook" else (entry.width_px, entry.height_px)
        )
        if kind == "workbook":
            assert len(state.checked) == 2
            evidence = options["verify_captures"](refresh_snapshot(), (entry,))
            assert evidence.current and evidence.hashes == {entry: refreshed.sha256}
            assert len(state.renders) == 2
        else:
            evidence = options["verify_spacing_captures"](refresh_snapshot(), (entry,))
            assert evidence.current and evidence.hashes == {entry: refreshed.sha256}
            assert len(state.renders) == 2
        if failure == "changed_source":
            state.snapshot = replace(snapshot, context_digest="changed native source")
            with pytest.raises(ValueError, match="board changed"):
                options["verify_snapshot"](snapshot)
            assert state.renderers[-1].closed
            assert len(state.renders) == 2
            if kind == "spacing":
                evidence = options["verify_spacing_captures"](snapshot, (entry,))
                assert not evidence.current and evidence.hashes == {entry: None}
            raise ProbeComplete
        if failure == "changed_palette":
            previous = options["appearance_context"]()
            state.palette = "changed native palette"
            assert options["appearance_context"]() != previous
            assert preview(entry).sha256 != refreshed.sha256
            assert len(state.renders) == 3
            assert state.renderers[0].closed
            raise ProbeComplete
        if failure == "source":
            state.source_error = True
            error = RuntimeError
        else:
            state.palette_error = True
            error = OSError
        with pytest.raises(error, match="unavailable"):
            preview(entry)
        assert state.renderers[-1].closed
        assert len(state.renders) == 2
        if kind == "spacing":
            evidence = options["verify_spacing_captures"](snapshot, (entry,))
            assert not evidence.current and evidence.hashes == {entry: None}
        raise ProbeComplete

    dialogs = ModuleType("impedance.dialog")
    dialogs.ImpedanceDialog = construct
    dialogs.SpecificationDialog = construct
    monkeypatch.setitem(sys.modules, "impedance.dialog", dialogs)
    monkeypatch.setitem(sys.modules, "impedance.integration", harness.integration)
    monkeypatch.setattr(pcbnew_adapter, "snapshot_board", read_source)
    private_adapter = importlib.import_module(package + ".pcbnew_adapter")
    monkeypatch.setattr(private_adapter, "snapshot_board", read_source)
    monkeypatch.setattr(source, "acquire_source_evidence", read_evidence)
    monkeypatch.setattr(harness.integration, "ImpedanceControls", make_controls)
    monkeypatch.setattr(palette, "read_theme_context", read_palette)
    monkeypatch.setattr(harness.integration, "read_theme_context", read_palette)
    monkeypatch.setattr(render, "SectionRenderer", Renderer)
    private_render = importlib.import_module(
        harness.integration.__package__ + ".render"
    )
    monkeypatch.setattr(private_render, "SectionRenderer", Renderer)

    def check_pixels(path: Path, *_args: Any) -> dict[str, Any]:
        """Native lanes retain pixel checks; here inspect their exact input bytes."""
        capture = CapturedImage.load(path)
        assert (capture.width, capture.height) == (800, 420)
        state.checked.append(capture.sha256)
        return {}

    monkeypatch.setattr(runner(), "check_png", check_pixels)
    with pytest.raises(ProbeComplete):
        runner().dialogs(
            plan, board, pcbnew, SimpleNamespace(Frame=Frame), object(), tmp_path
        )
    assert all(renderer.close_calls == 1 for renderer in state.renderers)
    assert controls[0]._closed
    assert frames[0].destroyed


class _PreviewState(SimpleNamespace):
    """Keep restored view evidence separate from the current native paint."""

    @property
    def displayed(self) -> bool:
        """Require view evidence, a completed paint and a visible canvas."""
        return bool(
            self.ready and self.canvas._recorded and self.canvas.IsShownOnScreen()
        )


@pytest.mark.parametrize("clipped", [False, True])
@pytest.mark.parametrize("stale_client", [False, True])
def test_minimum_capture_waits_for_native_allocation_and_rejects_clipping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clipped: bool,
    stale_client: bool,
) -> None:
    """A stable outer frame alone cannot establish correctly allocated child controls."""
    api = runner()
    state = SimpleNamespace(turns=0, saved=[])

    def size(width: int, height: int) -> Any:
        return SimpleNamespace(GetWidth=lambda: width, GetHeight=lambda: height)

    def allocated() -> bool:
        return state.turns >= (7 if stale_client else 3) and not clipped

    class Control:
        def IsShownOnScreen(self) -> bool:
            return True

        def GetScreenPosition(self) -> Any:
            return SimpleNamespace(x=720 if allocated() else 1000, y=140)

        def GetSize(self) -> Any:
            return size(250, 30)

    class Window:
        stackup_button = Control()

        def Layout(self) -> None:
            pass

        def Update(self) -> None:
            pass

        def GetSize(self) -> Any:
            return size(1020, 740)

        def GetClientSize(self) -> Any:
            return size(1248 if stale_client and not allocated() else 1000, 700)

        def GetMinSize(self) -> Any:
            return size(1020, 740)

        def GetScreenPosition(self) -> Any:
            return SimpleNamespace(x=20, y=30)

        def ScreenToClient(self, position: Any) -> Any:
            return SimpleNamespace(x=position.x - 20, y=position.y - 40)

        def GetSizer(self) -> Any:
            return SimpleNamespace(
                GetSize=lambda: size(1000 if allocated() else 1248, 700)
            )

    class Bitmap:
        def SaveFile(self, path: str, _kind: Any) -> bool:
            state.saved.append(state.turns)
            Path(path).write_bytes(b"unit pixel-boundary substitution")
            return True

    def dispatch() -> None:
        state.turns += 1

    wx = SimpleNamespace(
        GetApp=lambda: SimpleNamespace(Yield=dispatch),
        Bitmap=lambda *_args: Bitmap(),
        MemoryDC=lambda _bitmap: SimpleNamespace(
            Blit=lambda *_args: True, SelectObject=lambda _bitmap: None
        ),
        ScreenDC=lambda: object(),
        NullBitmap=object(),
        BITMAP_TYPE_PNG=1,
    )
    monkeypatch.setattr(api.time, "monotonic", lambda: state.turns / 100)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    destination = tmp_path / "minimum.png"
    if clipped:
        with pytest.raises(RuntimeError, match="layout"):
            api.capture_window(wx, Window(), destination)
        assert not state.saved
    else:
        api.capture_window(wx, Window(), destination)
        assert state.saved and state.saved[0] >= (7 if stale_client else 3)
    geometry = json.loads(destination.with_suffix(".layout.json").read_text())
    assert geometry["settled"] is (not clipped)
    assert geometry["client_size"] == [
        1248 if stale_client and clipped else 1000,
        700,
    ]


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


@pytest.mark.parametrize("unavailable", [False, True])
def test_gtk_screen_capture_uses_current_x11_pixels_and_rejects_source_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    """A stale Cairo root surface cannot stand in for the visible native modal."""
    api = runner()
    fresh, stale = b"\x11\x22\x33\x44\x55\x66", b"\0" * 6
    rectangles = []

    def current_pixels(rectangle: tuple[int, int, int, int]) -> bytes:
        rectangles.append(rectangle)
        if unavailable:
            raise OSError("The native X11 screen cannot be read")
        return fresh

    class Image:
        def __init__(self, width: int, height: int) -> None:
            self.data = b""
            assert (width, height) == (2, 1)

        def SetData(self, data: bytes) -> None:
            self.data = data

    class Bitmap:
        def __init__(self, *arguments: Any) -> None:
            self.data = arguments[0].data if len(arguments) == 1 else b""

        def SaveFile(self, path: str, _kind: Any) -> bool:
            Path(path).write_bytes(self.data)
            return True

    class MemoryDC:
        def __init__(self, bitmap: Bitmap) -> None:
            self.bitmap = bitmap

        def Blit(self, *_arguments: Any) -> bool:
            self.bitmap.data = stale
            return True

        def SelectObject(self, _bitmap: Any) -> None:
            pass

    dimensions = SimpleNamespace(GetWidth=lambda: 2, GetHeight=lambda: 1)
    dialog = SimpleNamespace(
        GetSize=lambda: dimensions,
        GetScreenPosition=lambda: SimpleNamespace(x=11, y=17),
    )
    wx = SimpleNamespace(
        Platform="__WXGTK__",
        GetApp=lambda: object(),
        Image=Image,
        Bitmap=Bitmap,
        MemoryDC=MemoryDC,
        ScreenDC=lambda: object(),
        NullBitmap=object(),
        BITMAP_TYPE_PNG=1,
    )
    monkeypatch.setattr(api, "settle_window_layout", lambda *_args: {"settled": True})
    monkeypatch.setattr(api, "_x11_screen_rgb", current_pixels, raising=False)
    output = tmp_path / "native-screen.png"
    if unavailable:
        with pytest.raises(OSError, match="cannot be read"):
            api.capture_window(wx, dialog, output)
        assert not output.exists()
    else:
        api.capture_window(wx, dialog, output)
        assert output.read_bytes() == fresh, (
            "The capture reused stale Cairo root pixels"
        )
    assert rectangles == [(11, 17, 2, 1)]


@pytest.mark.parametrize(
    ("byte_order", "bits_per_pixel", "failure"),
    [
        (0, 24, "none"),
        (1, 24, "none"),
        (0, 32, "none"),
        (1, 32, "none"),
        (0, 32, "channel"),
        (0, 32, "format"),
        (0, 32, "read"),
        (0, 32, "display"),
        (0, 32, "offscreen"),
    ],
)
def test_x11_screen_capture_decodes_native_rows_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
    byte_order: int,
    bits_per_pixel: int,
    failure: str,
) -> None:
    """Preserve actual RGB across padding/byte order and close every acquired resource."""
    api = runner()
    expected = bytes((17, 34, 51, 68, 85, 102, 119, 136, 153, 170, 187, 204))
    raw = bytearray()
    for row in range(2):
        for pixel in range(2):
            offset = (row * 2 + pixel) * 3
            red, green, blue = expected[offset : offset + 3]
            if byte_order:
                raw.extend(
                    (245, red, green, blue)
                    if bits_per_pixel == 32
                    else (red, green, blue)
                )
            else:
                raw.extend(
                    (blue, green, red, 245)
                    if bits_per_pixel == 32
                    else (blue, green, red)
                )
        raw.extend((211, 212, 213, 214))
    buffer = ctypes.create_string_buffer(bytes(raw))
    image = api._X11Image(
        width=2,
        height=2,
        xoffset=0,
        format=2,
        data=ctypes.addressof(buffer),
        byte_order=byte_order,
        bits_per_pixel=16 if failure == "format" else bits_per_pixel,
        bytes_per_line=2 * (bits_per_pixel // 8) + 4,
        red_mask=0xF800 if failure == "channel" else 0xFF0000,
        green_mask=0xFF00,
        blue_mask=0xFF,
    )
    state = SimpleNamespace(reads=[], destroyed=[], closed=[])

    class NativeFunction:
        def __init__(self, callback: Any) -> None:
            self.callback = callback

        def __call__(self, *arguments: Any) -> Any:
            return self.callback(*arguments)

    def get_image(*arguments: Any) -> Any:
        state.reads.append(arguments[2:6])
        return (
            ctypes.POINTER(api._X11Image)()
            if failure == "read"
            else ctypes.pointer(image)
        )

    library = SimpleNamespace(
        XOpenDisplay=NativeFunction(lambda _name: None if failure == "display" else 1),
        XDefaultScreen=NativeFunction(lambda _display: 0),
        XDefaultRootWindow=NativeFunction(lambda _display: 42),
        XDisplayWidth=NativeFunction(lambda _display, _screen: 16),
        XDisplayHeight=NativeFunction(lambda _display, _screen: 16),
        XGetImage=NativeFunction(get_image),
        XDestroyImage=NativeFunction(state.destroyed.append),
        XCloseDisplay=NativeFunction(state.closed.append),
    )
    monkeypatch.setattr(api.ctypes, "CDLL", lambda _name: library)
    rectangle = (15, 15, 2, 2) if failure == "offscreen" else (4, 6, 2, 2)
    if failure == "none":
        assert api._x11_screen_rgb(rectangle) == expected
    else:
        with pytest.raises(RuntimeError):
            api._x11_screen_rgb(rectangle)
    assert len(state.reads) == (0 if failure in {"display", "offscreen"} else 1)
    if state.reads:
        assert state.reads == [rectangle]
    assert len(state.destroyed) == (
        1 if failure in {"none", "channel", "format"} else 0
    )
    assert state.closed == ([] if failure == "display" else [1])


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


def test_native_pair_plan_audits_via_covered_launches_without_exporting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual 0.25 mm launch stubs fit a 0.6 mm via with 0.1 mm trace copper."""
    from impedance import matching, pcbnew_adapter
    from impedance.model import EXCLUSION_COVERED_STUB, Config, CopperFeature

    case, _source = runner().fixture("usb-differential-90-ohm")
    signal_vias = tuple(
        via
        for circuit in case.circuits
        for via in circuit.vias()
        if via.net in case.net_names
    )
    features = tuple(
        CopperFeature(
            f"native-signal-via-{index}:{layer}",
            via.net,
            layer,
            "via",
            "circle",
            via.point,
            via.diameter_nm,
            via.diameter_nm,
        )
        for index, via in enumerate(signal_vias)
        for layer in via.layers
    )
    snapshot = replace(case.snapshot(), copper_features=features)
    # These physical launch capsules lie exactly within their same-net via:
    # 0.25 mm centerline + 0.05 mm trace radius = 0.30 mm flash radius.
    expected_covered = {
        trace.trace_id
        for trace in snapshot.traces
        if trace.layer == "In2.Cu"
        and trace.width_nm == 100_000
        and trace.points[-1] == (trace.points[0][0] + 250_000, trace.points[0][1])
        and any(
            via.net == trace.net
            and via.point == trace.points[0]
            and via.diameter_nm == 600_000
            for via in signal_vias
        )
    }
    assert len(expected_covered) == 4
    analysis = matching.analyze(Config(True, case.specifications()), snapshot)
    assert {
        trace.trace_id
        for row in analysis.sections
        if row.exclusion_reason == EXCLUSION_COVERED_STUB
        for trace in row.traces
    } == expected_covered
    monkeypatch.setattr(pcbnew_adapter, "snapshot_board", lambda *_args: snapshot)

    plan = runner().class_plan(case, object(), object())

    assert {
        trace.trace_id for row in plan.analysis.sections for trace in row.traces
    } == {trace.trace_id for trace in snapshot.traces}
    assert {trace.trace_id for row in plan.sections for trace in row.traces} == {
        trace.trace_id for trace in snapshot.traces
    } - expected_covered
    assert all(not row.exclusion_reason for row in plan.sections)
    pairs = {tuple(sorted(circuit.net_names)) for circuit in case.circuits}
    assert all(tuple(sorted(row.net_names)) in pairs for row in plan.sections)


def test_native_plan_rejects_unproven_covered_stub_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring an audit row may never hide a real routed pair from native acceptance."""
    from impedance import matching, pcbnew_adapter
    from impedance.model import EXCLUSION_COVERED_STUB, Config

    case, _source = runner().fixture("usb-differential-90-ohm")
    snapshot = case.snapshot()
    analysis = matching.analyze(Config(True, case.specifications()), snapshot)
    invalid = replace(
        analysis,
        sections=(
            replace(analysis.sections[0], exclusion_reason=EXCLUSION_COVERED_STUB),
            *analysis.sections[1:],
        ),
    )
    monkeypatch.setattr(pcbnew_adapter, "snapshot_board", lambda *_args: snapshot)
    monkeypatch.setattr(matching, "analyze", lambda *_args: invalid)

    with pytest.raises(RuntimeError, match="covered-stub.*proof"):
        runner().class_plan(case, object(), object())


def test_preview_waits_for_paint_and_display_callbacks() -> None:
    """A native selection's queued image is not equivalent to a completed view."""
    events: list[str] = []
    pane = _PreviewState(ready=False, failure="")
    canvas = SimpleNamespace(
        _bitmap=None,
        _recorded=False,
        IsShownOnScreen=lambda: True,
        Update=lambda: events.append("update"),
    )
    pane.canvas = canvas

    def dispatch() -> None:
        events.append("yield")
        if events.count("yield") == 1:
            canvas._bitmap = SimpleNamespace(IsOk=lambda: True)
            pane.ready = True
        else:
            canvas._recorded = True

    runner().wait_for_preview(SimpleNamespace(Yield=dispatch), pane)
    assert events == ["yield", "update", "yield", "update"]


@pytest.mark.parametrize("failure", ["pending", "failed", "hidden", "bitmap"])
def test_preview_wait_rejects_incomplete_or_invisible_images(failure: str) -> None:
    """Fail boundedly rather than approving queued, failed, or hidden pixels."""
    pane = _PreviewState(
        ready=failure in ("hidden", "bitmap"),
        failure="capture failed" if failure == "failed" else "",
        canvas=SimpleNamespace(
            _bitmap=SimpleNamespace(IsOk=lambda: failure != "bitmap"),
            _recorded=failure in ("hidden", "bitmap"),
            IsShownOnScreen=lambda: failure != "hidden",
        ),
    )
    with pytest.raises(RuntimeError, match="display|failed|bitmap"):
        runner().wait_for_preview(SimpleNamespace(), pane, timeout=0)


@pytest.mark.parametrize("padding", [8, 16])
@pytest.mark.parametrize("rgb_size_delta", [0, -1, 1])
def test_native_capture_check_requires_requested_sixteen_pixel_padding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    padding: int,
    rgb_size_delta: int,
) -> None:
    """Synthetic pixels check the harness's geometry policy, not native drawing."""
    from impedance import render
    from tests.test_impedance_report_pipeline import _png

    raw = bytearray(b"\x10\x20\x30" * 800 * 420)
    for index in range(20):
        raw[index * 3 : index * 3 + 3] = bytes((index, 60, 100))
    # Yellow-looking bytes crossing a pixel boundary cannot enlarge the box.
    raw[1:4] = b"\xff\xff\x00"
    assert raw.find(b"\xff\xff\x00") == 1
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
    if rgb_size_delta < 0:
        del raw[rgb_size_delta:]
    elif rgb_size_delta > 0:
        raw.extend(b"\x00" * rgb_size_delta)
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
    if rgb_size_delta:
        with pytest.raises(RuntimeError, match="RGB pixels"):
            runner().check_png(path, section, object(), wx)
    elif padding == 16:
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


@pytest.fixture
def stage_process(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Create stage records; real subprocess cancellation has independent coverage."""
    api = runner()

    def install(
        *, failure: str = "", paths: Optional[list[Path]] = None
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def launch(arguments: list[str], **kwargs: Any) -> Any:
            calls.append(arguments)
            assert arguments[0] == sys.executable
            if failure == "timeout":
                raise subprocess.TimeoutExpired(arguments, 12, output="native stalled")
            output = Path(arguments[arguments.index("--output") + 1])
            assert not output.exists()
            if paths is not None:
                assert output not in paths
                paths.append(output)
            if failure != "missing":
                output.mkdir()
                (output / "result.json").write_text(
                    json.dumps(
                        {
                            "stage": "wrong-stage"
                            if failure == "false_pass"
                            else arguments[arguments.index("--worker") + 1],
                            "status": "PASS",
                            "evidence": {"test_double": True},
                            "run_token": arguments[arguments.index("--run-token") + 1],
                        }
                    )
                )
            kwargs["stdout"].write("stage finished")

            class Process:
                returncode = 3 if failure == "exit" else 0

                def poll(self) -> int:
                    return self.returncode

                def wait(self, **_kwargs: Any) -> int:
                    return self.returncode

            return Process()

        monkeypatch.setattr(api.subprocess, "Popen", launch)
        monkeypatch.setattr(api.ProcessSupervisor, "_stop", lambda *_args: None)
        return calls

    return install


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing", "false_pass"])
def test_native_stage_failures_never_become_skips_or_success(
    tmp_path: Path, stage_process: Any, failure: str
) -> None:
    """A failed/malformed child stops later stages and retains actionable logs."""
    api = runner()
    calls = stage_process(failure=failure)
    output = tmp_path / "run"
    assert api.run(output, timeout=12) == 1
    report = json.loads((output / "summary.json").read_text())
    assert report["status"] == "FAIL"
    assert report["stages"][0]["status"] == "FAIL"
    assert all(item["status"] == "NOT_RUN" for item in report["stages"][1:])
    assert len(calls) == 1
    assert (output / "prerequisites.log").is_file()


def test_success_requires_every_exact_stage_record(
    tmp_path: Path, stage_process: Any
) -> None:
    """A complete synthetic orchestration run reports its non-native test evidence."""
    api = runner()

    stage_process()
    output = tmp_path / "run"
    assert api.run(output, timeout=12) == 0
    report = json.loads((output / "summary.json").read_text())
    assert report["status"] == "PASS"
    assert tuple(item["stage"] for item in report["stages"]) == api.STAGES
    assert all(item["evidence"]["test_double"] for item in report["stages"])


def test_parallel_smoke_starts_heavy_stages_first_and_preserves_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent native groups overlap without sharing or dropping any group."""
    api = runner()
    started: list[str] = []
    active: set[str] = set()
    maximum = 0
    lock = threading.Lock()
    heavy = threading.Barrier(2)

    def stage(
        name: str, output: Path, timeout: int, cancelled: threading.Event
    ) -> dict[str, Any]:
        nonlocal maximum
        assert timeout == 12 and not cancelled.is_set()
        with lock:
            if name != "prerequisites":
                assert started[0] == "prerequisites"
            started.append(name)
            active.add(name)
            maximum = max(maximum, len(active))
        if name in ("generate", "dialogs"):
            heavy.wait(timeout=3)
        time.sleep(0.01)
        with lock:
            active.remove(name)
        return {
            "stage": name,
            "status": "PASS",
            "evidence": {"preserved": name},
            "duration_seconds": 1.0,
        }

    monkeypatch.setattr(api, "run_stage", stage, raising=False)
    output = tmp_path / "parallel"
    assert api.run(output, timeout=12, workers=2) == 0
    summary = json.loads((output / "summary.json").read_text())
    assert started[0] == "prerequisites"
    assert set(started[1:3]) == {"generate", "dialogs"}
    assert maximum == 2
    assert [record["stage"] for record in summary["stages"]] == list(api.STAGES)
    assert [record["evidence"]["preserved"] for record in summary["stages"]] == list(
        api.STAGES
    )
    assert summary["summed_worker_seconds"] == len(api.STAGES)
    assert summary["elapsed_seconds"] < summary["summed_worker_seconds"]
    assert summary["timing_mode"] == "parallel diagnostic"


@pytest.mark.parametrize("failure", ["prerequisites", "generate"])
def test_parallel_smoke_failure_cancels_active_and_blocks_unstarted_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failure cannot produce success or allow later independent groups to start."""
    api = runner()
    started: list[str] = []
    both = threading.Barrier(2)

    def stage(
        name: str, _output: Path, _timeout: int, cancelled: threading.Event
    ) -> dict[str, Any]:
        started.append(name)
        if name in ("generate", "dialogs"):
            both.wait(timeout=3)
        if name == "dialogs":
            assert cancelled.wait(timeout=3)
        return {
            "stage": name,
            "status": "FAIL" if name in (failure, "dialogs") else "PASS",
            "error": "test-owned native failure",
            "duration_seconds": 0.1,
        }

    monkeypatch.setattr(api, "run_stage", stage, raising=False)
    output = tmp_path / "failed"
    assert api.run(output, timeout=12, workers=2) == 1
    summary = json.loads((output / "summary.json").read_text())
    records = {record["stage"]: record for record in summary["stages"]}
    assert summary["status"] == "FAIL" and records[failure]["status"] == "FAIL"
    assert all(
        records[name]["status"] == "NOT_RUN"
        for name in api.STAGES
        if name not in started
    )
    assert set(started) <= {"prerequisites", "generate", "dialogs"}


def test_parallel_supervisor_termination_cancels_children_and_restores_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow timeout must not orphan sessions detached into worker process groups."""
    api = runner()
    original = signal.getsignal(signal.SIGTERM)
    received: list[bool] = []

    def stage(
        name: str, _output: Path, _timeout: int, cancelled: threading.Event
    ) -> dict[str, Any]:
        if name == "prerequisites":
            os.kill(os.getpid(), signal.SIGTERM)
            received.append(cancelled.is_set())
        return {"stage": name, "status": "FAIL", "error": "supervisor cancelled"}

    monkeypatch.setattr(api, "run_stage", stage)
    assert api.run(tmp_path / "terminated", workers=2) == 1
    assert received == [True]
    assert signal.getsignal(signal.SIGTERM) is original
    summary = json.loads((tmp_path / "terminated" / "summary.json").read_text())
    assert summary["status"] == "FAIL"
    assert all(record["status"] == "NOT_RUN" for record in summary["stages"][1:])


def test_cancelled_stage_never_launches_another_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued stage must remain unstarted after another stage cancels the suite."""
    api = runner()
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(
        api, "stage_command", lambda *_args: pytest.fail("launched after cancellation")
    )
    record = api.run_stage("dialogs", tmp_path, 30, cancelled)
    assert record["status"] == "FAIL"
    assert record["error"] == "Cancelled after another stage failed"


def test_successful_child_cannot_override_cancellation_during_result_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_process: Any,
) -> None:
    """A current PASS artifact cannot override cancellation while it is being read."""
    api = runner()
    cancelled = threading.Event()
    stage_process()
    loads = json.loads

    def cancel_after_read(value: str) -> Any:
        data = loads(value)
        cancelled.set()
        return data

    monkeypatch.setattr(api, "json", SimpleNamespace(loads=cancel_after_read))
    record = api.run_stage("dialogs", tmp_path, 30, cancelled)
    assert record["status"] == "FAIL"
    assert record["error"] == "Cancelled after another stage failed"


@pytest.mark.parametrize(
    "configuration",
    [{"generate_only": True}, {"generate_budget_seconds": 60.0}],
)
def test_parallel_workers_cannot_masquerade_as_a_sequential_benchmark(
    tmp_path: Path, configuration: dict[str, Any]
) -> None:
    """Elapsed benchmark budgets keep the original complete sequential workload."""
    output = tmp_path / "forbidden"
    with pytest.raises(ValueError, match="sequential"):
        runner().run(output, workers=2, **configuration)
    assert not output.exists()


@pytest.mark.parametrize("failure", ["none", "missing", "stale", "wrong-stage", "exit"])
def test_parallel_child_requires_its_fresh_authenticated_successful_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A real fresh process cannot pass using missing or another invocation's JSON."""
    api = runner()
    program = """
import json, pathlib, sys
stage, destination, token, failure = sys.argv[1:]
output = pathlib.Path(destination)
output.mkdir()
if failure != 'missing':
    (output / 'result.json').write_text(json.dumps({
        'stage': 'wrong' if failure == 'wrong-stage' else stage,
        'status': 'PASS', 'evidence': {'real_child': True},
        'run_token': 'previous-run' if failure == 'stale' else token,
    }))
raise SystemExit(3 if failure == 'exit' else 0)
"""
    monkeypatch.setattr(
        api,
        "stage_command",
        lambda stage, output, token: [
            sys.executable,
            "-c",
            program,
            stage,
            str(output),
            token,
            failure,
        ],
    )
    output = tmp_path / "stages"
    output.mkdir()
    record = api.run_stage("dialogs", output, 30, threading.Event())
    assert record["status"] == ("PASS" if failure == "none" else "FAIL")
    assert (output / "dialogs.log").is_file()
    if failure == "none":
        assert record["evidence"] == {"real_child": True}


@pytest.mark.parametrize("termination", ["timeout", "cancel", "readiness_failure"])
def test_parallel_child_cancellation_reaps_its_native_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str
) -> None:
    """Killing only the native parent would leave its display/CLI descendants active."""
    api = runner()
    marker = tmp_path / "descendant-ticks"
    ready = tmp_path / "descendant-ready"
    descendant = """
import pathlib,sys,time
marker, ready = (pathlib.Path(value) for value in sys.argv[1:3])
marker.write_text(str(time.time_ns()))
if sys.argv[3] != 'readiness_failure':
    ready.write_text('ready')
while True:
    marker.write_text(str(time.time_ns()))
    time.sleep(.02)
"""
    # Deliberately exceed the former 250ms timer to cover slow interpreter startup.
    program = "import subprocess,sys,time\ntime.sleep(.45)\nsubprocess.Popen([sys.executable,'-c',*sys.argv[1:]])\ntime.sleep(30)"
    monkeypatch.setattr(
        api,
        "stage_command",
        lambda *_args: [
            sys.executable,
            "-c",
            program,
            descendant,
            str(marker),
            str(ready),
            termination,
        ],
    )
    cancelled = threading.Event()
    completed = threading.Event()
    observed: dict[str, bool] = {}
    real_monotonic = time.monotonic
    expiration_origin: Optional[float] = None

    def deadline_clock() -> float:
        """Start the timeout probe after readiness; cleanup keeps a running clock."""
        if expiration_origin is None:
            return 0.0
        return 2.0 + real_monotonic() - expiration_origin

    def wait_for_marker(path: Path, seconds: float) -> bool:
        deadline = real_monotonic() + seconds
        while not path.is_file():
            if real_monotonic() >= deadline or completed.wait(0.01):
                return False
        return True

    def request_termination() -> None:
        nonlocal expiration_origin
        try:
            observed["live"] = wait_for_marker(marker, 10.0)
            if observed["live"]:
                observed["ready"] = wait_for_marker(
                    ready, 0.05 if termination == "readiness_failure" else 10.0
                )
            if observed.get("ready") and termination == "timeout":
                expiration_origin = real_monotonic()
        finally:
            if expiration_origin is None:
                cancelled.set()

    if termination == "timeout":
        # Replace this module's clock reference, leaving subprocess/thread clocks real.
        monkeypatch.setattr(
            importlib.import_module("scripts.native_impedance_support"),
            "time",
            SimpleNamespace(monotonic=deadline_clock),
        )
    controller = threading.Thread(target=request_termination)
    controller.start()
    try:
        record = api.run_stage(
            "generate", tmp_path, 1 if termination == "timeout" else 30, cancelled
        )
    finally:
        completed.set()
        cancelled.set()
        controller.join(timeout=2)
    assert not controller.is_alive(), "The readiness controller did not finish"
    assert record["status"] == "FAIL"
    assert observed.get("live"), "The descendant must run before termination"
    assert observed.get("ready", False) is (termination != "readiness_failure")
    assert record["error"] == (
        "Native process exceeded 1 seconds"
        if termination == "timeout"
        else "Cancelled after another stage failed"
    )
    assert marker.is_file(), "The descendant must actually run before termination"
    stopped = marker.read_bytes()
    time.sleep(0.1)
    assert marker.read_bytes() == stopped, "A descendant survived its worker's failure"


@pytest.mark.parametrize("failure", [False, True])
def test_private_desktop_owns_display_and_preferences_and_cleans_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    """A parallel worker never captures another stage's root window or WM state."""
    api = runner()
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":original")
    monkeypatch.setenv("XDG_CONFIG_HOME", "original-config")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    processes: list[Any] = []

    class Process:
        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            self.command = command
            self.stopped = False
            self.waited = False
            if command[0] == "Xvfb":
                assert "-noreset" in command
                os.write(int(command[command.index("-displayfd") + 1]), b"321\n")
            else:
                assert os.environ["DISPLAY"] == ":321"
            processes.append(self)

        def poll(self) -> Optional[int]:
            return 0 if self.stopped else None

        def terminate(self) -> None:
            self.stopped = True

        def wait(self, **_kwargs: Any) -> int:
            self.waited = True
            return 0

    monkeypatch.setattr(api.subprocess, "Popen", Process)
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="window id # 0x123"
        ),
    )
    expected = (
        pytest.raises(RuntimeError, match="native test failed")
        if failure
        else nullcontext()
    )
    with expected, api.private_display(tmp_path):
        assert os.environ["DISPLAY"] == ":321"
        assert Path(os.environ["XDG_CONFIG_HOME"]).parent == tmp_path / "desktop"
        assert Path(os.environ["XDG_CACHE_HOME"]).parent == tmp_path / "desktop"
        if failure:
            raise RuntimeError("native test failed")
    assert len(processes) == 2 and all(p.stopped and p.waited for p in processes)
    assert os.environ["DISPLAY"] == ":original"
    assert os.environ["XDG_CONFIG_HOME"] == "original-config"
    assert "XDG_CACHE_HOME" not in os.environ


@pytest.mark.parametrize("over_budget", [False, True])
def test_full_smoke_suite_applies_generate_budget_only_to_complete_generate_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_process: Any,
    over_budget: bool,
) -> None:
    """A Generate budget preserves seven stages and their existing artifact paths."""
    api = runner()
    durations = [120.0] * (len(api.STAGES) - 1) + [61.0 if over_budget else 59.0]
    clock = iter(
        value
        for index, duration in enumerate(durations)
        for value in (index * 200.0, index * 200.0 + duration)
    )
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(clock))
    paths: list[Path] = []

    stage_process(paths=paths)
    output = tmp_path / "full-smoke"
    assert api.run(output, generate_budget_seconds=60.0) == int(over_budget)
    report = json.loads((output / "summary.json").read_text())
    assert tuple(path.name for path in paths) == api.STAGES
    assert tuple(record["stage"] for record in report["stages"]) == api.STAGES
    assert [record["duration_seconds"] for record in report["stages"]] == durations
    assert all(
        record["status"] == "PASS" and "budget_seconds" not in record
        for record in report["stages"][:-1]
    )
    assert report["stages"][-1]["budget_seconds"] == 60.0
    assert report["stages"][-1]["status"] == ("FAIL" if over_budget else "PASS")
    assert report["status"] == ("FAIL" if over_budget else "PASS")
    assert all(record["log"] == record["stage"] + ".log" for record in report["stages"])


@pytest.mark.parametrize("over_budget", [False, True])
def test_generate_benchmark_runs_complete_fresh_workers_and_accounts_for_all_wall_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_process: Any,
    over_budget: bool,
) -> None:
    """Three isolated complete runs remain measured even when the budget is missed."""
    api = runner()
    elapsed = 61.0 if over_budget else 59.0
    clock = iter(
        value for run in range(3) for value in (run * 100, run * 100 + elapsed)
    )
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(clock))
    paths: list[Path] = []

    stage_process(paths=paths)
    output = tmp_path / "benchmark"
    assert api.run(
        output,
        timeout=120,
        generate_only=True,
        generate_runs=3,
        generate_budget_seconds=60.0,
    ) == int(over_budget)
    report = json.loads((output / "summary.json").read_text())
    assert len(paths) == 3
    assert [record["duration_seconds"] for record in report["stages"]] == [elapsed] * 3
    assert [record["run"] for record in report["stages"]] == [1, 2, 3]
    assert all(record["stage"] == "generate" for record in report["stages"])
    assert all(record["budget_seconds"] == 60.0 for record in report["stages"])
    assert all(
        record["status"] == ("FAIL" if over_budget else "PASS")
        for record in report["stages"]
    )
    assert all((output / record["log"]).is_file() for record in report["stages"])


@pytest.mark.parametrize(
    "arguments",
    [
        {"generate_runs": 3},
        {"generate_only": True, "generate_runs": 0},
        {"generate_only": True, "generate_runs": 11},
        {"generate_budget_seconds": -1.0},
        {"generate_only": True, "generate_budget_seconds": -1.0},
        {"generate_only": True, "generate_budget_seconds": float("nan")},
    ],
)
def test_invalid_generate_benchmark_configuration_creates_no_artifacts(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    """Benchmark selectors cannot silently change the normal complete smoke suite."""
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Generate"):
        runner().run(output, **arguments)
    assert not output.exists()


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
            runner().check_yellow_edges(b"".join(pixels), (left, top, right, bottom))
    else:
        assert (
            runner().check_yellow_edges(b"".join(pixels), (left, top, right, bottom))
            is None
        )


@pytest.fixture(scope="module")
def storage_workflow_baseline(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Export identical inputs once; each corruption still receives private real files."""
    from impedance import render, service
    from scripts.generate_rf_impedance_captures import (
        fixture_snapshot,
        reviewed_sections,
    )
    from tests.test_impedance_report_pipeline import _png

    api = runner()
    case, source = api.fixture("usb-differential-90-ohm")
    snapshot = fixture_snapshot(case, case.snapshot())
    config, sections = reviewed_sections(case, snapshot)
    from impedance.matching import analyze

    analysis = analyze(config, snapshot)
    plan = service.ExportPlan(config, snapshot, analysis, sections)

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

        def render_spacing(
            self, entry: Any, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Use distinct deterministic native-boundary diagnostic bytes."""
            assert (width_px, height_px) == (entry.width_px, entry.height_px)
            destination.write_bytes(
                _png(width_px, height_px, color=bytes((90, 120, 200)))
            )
            return destination

        def render_spacing_original(self, entry: Any, destination: Path) -> Path:
            """Keep real HTML comparison validation at the substituted PNG boundary."""
            width, height = entry.composition.detail_rect[2:]
            destination.write_bytes(_png(width, height, color=bytes((90, 121, 200))))
            return destination

        def close(self) -> None:
            """Match native renderer lifetime without inventing render evidence."""

    output = tmp_path_factory.mktemp("storage-workflow-baseline")
    original_export = service.export_reports
    exported: list[Any] = []

    def nonnative_export(
        plan: Any, board: Any, module: Any, scratch: Path
    ) -> service.ExportArtifacts:
        """Keep the production exporter, PNG media and HTML/XLSX construction real."""
        artifacts = original_export(
            plan, board, module, scratch, renderer_factory=FixtureRenderer
        )
        exported.append((plan, artifacts))
        return artifacts

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(render, "SectionRenderer", FixtureRenderer)
        patch.setattr(service, "export_reports", nonnative_export)
        result = api.workflow(
            plan, SimpleNamespace(GetFileName=lambda: str(source)), object(), output
        )
    assert len(exported) == 1
    reviewed_plan, artifacts = exported[0]
    return SimpleNamespace(
        plan=reviewed_plan,
        directory=output / "export",
        artifacts=artifacts,
        result=result,
        original={
            path.relative_to(output / "export"): path.read_bytes()
            for path in (output / "export").rglob("*")
            if path.is_file()
        },
    )


@pytest.mark.parametrize("corruption", ["none", "width", "ground_gap"])
def test_storage_workflow_preserves_both_board_intents(
    tmp_path: Path,
    storage_workflow_baseline: Any,
    corruption: str,
) -> None:
    """Preserve complete successful storage/export and independently damage copies."""
    from impedance.service import ExportArtifacts

    baseline = storage_workflow_baseline
    assert baseline.result["distinct_board_intents_preserved"] is True
    scratch = tmp_path / "export"
    shutil.copytree(baseline.directory, scratch)
    artifacts = ExportArtifacts(
        scratch / baseline.artifacts.workbook.name,
        scratch / baseline.artifacts.html_report.name,
        baseline.artifacts.appearance,
    )
    with ZipFile(artifacts.workbook) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    name = "xl/worksheets/sheet1.xml"
    sheet = ET.fromstring(parts[name])  # noqa: S314 -- Locally generated report XML.
    width = sheet.find(".//s:c[@r='D2']/s:v", namespace)
    gap = sheet.find(".//s:c[@r='F2']/s:is/s:t", namespace)
    assert width is not None and width.text == "10.6299"
    assert gap is not None and gap.text == "Pair: 8\nGround: 7.874"
    if corruption != "none":
        if corruption == "width":
            width.text = "10.63"
        else:
            gap.text = "Pair: 8\nGround: 7.8741"
        parts[name] = ET.tostring(sheet)
        with ZipFile(artifacts.workbook, "w", compression=ZIP_DEFLATED) as package:
            for name, data in parts.items():
                package.writestr(name, data)
        expected = "native width" if corruption == "width" else "ground gap"
        with pytest.raises(RuntimeError, match=expected):
            runner().check_workflow_reports(baseline.plan, artifacts, scratch)
    assert baseline.original == {
        path.relative_to(baseline.directory): path.read_bytes()
        for path in baseline.directory.rglob("*")
        if path.is_file()
    }, "A corruption case modified the shared real-export baseline"


@pytest.mark.parametrize("failure", ["none", "uncaptured", "stale"])
def test_noninteractive_spacing_fixture_requires_fresh_exact_png_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Fixture setup cannot turn failed or subsequently changed captures into consent."""
    from impedance import render, service
    from impedance.model import ValidationError
    from tests.test_impedance_native_workflow import _spacing_fixture
    from tests.test_impedance_report_pipeline import _png

    config, snapshot, analysis = _spacing_fixture()
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    with pytest.raises(ValidationError, match="individual approval"):
        service.prepare(config, snapshot)
    plan = service.ExportPlan(config, snapshot, analysis, analysis.sections)
    pixels_changed = False

    class Renderer:
        """Substitute only rasterization; production consent and writers remain real."""

        def __init__(self, *_args: Any) -> None:
            pass

        def render_spacing(
            self, entry: Any, destination: Path, width_px: int, height_px: int
        ) -> Path:
            if failure != "uncaptured":
                destination.write_bytes(
                    _png(
                        width_px,
                        height_px,
                        color=bytes((40 if pixels_changed else 20, 70, 120)),
                    )
                )
            return destination

        def render(
            self, _section: Any, destination: Path, width_px: int, height_px: int
        ) -> Path:
            destination.write_bytes(_png(width_px, height_px))
            return destination

        def close(self) -> None:
            pass

    monkeypatch.setattr(render, "SectionRenderer", Renderer)
    if failure == "uncaptured":
        with pytest.raises(ValidationError, match="Cannot read"):
            runner().fixture_spacing_review(plan, object(), object(), tmp_path)
        return
    reviewed = runner().fixture_spacing_review(plan, object(), object(), tmp_path)
    assert reviewed.config.review_tracking.spacing_images
    assert all(
        record.approved_digest == record.viewed_digest
        for record in reviewed.config.review_tracking.spacing_images
    )
    pixels_changed = failure == "stale"
    scratch = tmp_path / "export"
    scratch.mkdir()
    if pixels_changed:
        with pytest.raises(ValidationError, match="spacing.*changed|Spacing.*changed"):
            service.export_reports(
                reviewed, object(), object(), scratch, renderer_factory=Renderer
            )
        assert not tuple(scratch.glob("*.xlsx"))
    else:
        artifacts = service.export_reports(
            reviewed, object(), object(), scratch, renderer_factory=Renderer
        )
        assert artifacts.workbook.is_file()
        assert 'class="spacing-review"' in artifacts.html_report.read_text(
            encoding="utf-8"
        )


@pytest.mark.parametrize(
    "palette",
    [
        "selected",
        "default_background",
        "default_copper",
        "old_dimming",
        "missing_dimming",
        "excessive_dimming",
        "unboosted_selected",
        "dimmed_selected",
        "wrong_opacity",
    ],
)
def test_custom_palette_checks_the_canvas_under_translucent_copper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, palette: str
) -> None:
    """A valid viewport may be entirely covered by the selected layer's pour."""
    from impedance import render
    from tests.test_impedance_report_pipeline import _png

    # Surrounding copper mixes 50% toward the canvas, then its native zone
    # opacity composites at 60%. Selected traces boost 30% toward black here.
    # A filled viewport has no exposed canvas, so the plane must prove both
    # color operations; accepting the old undimmed plane would miss the feature.
    background = (16, 21, 29) if palette == "default_background" else (247, 249, 251)
    foreground = (200, 52, 52) if palette == "default_copper" else (23, 43, 61)
    dimming = {
        "old_dimming": 0.35,
        "missing_dimming": 0.0,
        "excessive_dimming": 0.8,
    }.get(palette, 0.5)
    subdued = tuple(
        round((1 - dimming) * channel + dimming * canvas)
        for channel, canvas in zip(foreground, background)
    )
    opacity = 1.0 if palette == "wrong_opacity" else 0.6
    plane = bytes(
        round(opacity * channel + (1 - opacity) * canvas)
        for channel, canvas in zip(subdued, background)
    )
    copper = bytes(
        subdued
        if palette == "dimmed_selected"
        else foreground
        if palette == "unboosted_selected"
        else tuple(round(channel * 0.7) for channel in foreground)
    )
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
        # The gallery has the same valid policy when every capture is entirely
        # inside its pour. Do not require an exposed-canvas pixel elsewhere.
        from impedance import pcbnew_adapter

        def synthetic_capture(
            _section: Any, destination: Path, _width: int, _height: int
        ) -> Path:
            """Write the synthetic PNG at the renderer's actual output boundary."""
            destination.write_bytes(image_path.read_bytes())
            return destination

        snapshot = object()
        monkeypatch.setattr(pcbnew_adapter, "snapshot_board", lambda *_args: snapshot)
        monkeypatch.setattr(
            render,
            "SectionRenderer",
            lambda *_args: SimpleNamespace(
                render=synthetic_capture,
                close=lambda: None,
            ),
        )
        gallery = runner().render_rows(
            SimpleNamespace(sections=(section,)),
            object(),
            object(),
            wx,
            tmp_path,
            custom=True,
        )
        assert len(gallery) == 1
        assert gallery[0]["light_canvas_pixels"] == 0
        assert gallery[0]["light_plane_pixels"] == result["light_plane_pixels"]
    else:
        with pytest.raises(RuntimeError, match="Selected"):
            runner().check_png(image_path, section, object(), wx, custom=True)
