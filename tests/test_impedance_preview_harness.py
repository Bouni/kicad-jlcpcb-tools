"""Check manual preview setup without importing or opening real native controls."""

from collections.abc import Callable
from pathlib import Path
import struct
import sys
from types import ModuleType
from typing import Any
from xml.etree import ElementTree as ET
import zlib

import pytest

from impedance.matching import analyze
from impedance.model import BoardSnapshot, Config
from impedance.service import CapturedImage
from scripts import preview_impedance_dialogs as harness
from tests.rf_impedance_fixtures import RF_CASES, RFCase


class NativeBoundaryReached(RuntimeError):
    """Signal validated arguments reached, but did not open, the native UI."""


@pytest.fixture
def native_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never create a native app, even when an argument-validation regression occurs."""
    wx = ModuleType("wx")

    def app(*args: Any, **kwargs: Any) -> None:
        """Stop before native construction, so these tests remain headless."""
        raise NativeBoundaryReached("Validated arguments reached the native boundary")

    wx.App = app
    dialog = ModuleType("impedance.dialog")
    dialog.ImpedanceDialog = object
    dialog.SpecificationDialog = object
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "impedance.dialog", dialog)


def _context(directory: Path, name: str = "native.svg") -> Path:
    """Retain recognizable native colors in an inspectable local SVG source."""
    path = directory / name
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="320mm" height="280mm" '
        'viewBox="0 0 320 280"><path d="M20,20 L270,260" stroke="#172b3d" '
        'fill="none"/></svg>',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("arguments", [["--new"], ["--specification", "1"]])
def test_specification_harness_supplies_workbook_preview_callback(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """New and reopened native editors receive the same marked-capture renderer."""
    callbacks = []

    class App:
        """Retain the native app lifecycle without opening desktop controls."""

        def __init__(self, redirect: bool) -> None:
            assert redirect is False

        def SetAppName(self, name: str) -> None:
            assert name

    class SpecificationDialog:
        """Exercise harness wiring, not the separately tested dialog widgets."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            callback = kwargs.get("preview")
            assert callable(callback), "Layer approval must show the workbook capture"
            callbacks.append(callback)

        def ShowModal(self) -> None:
            assert callbacks

        def Destroy(self) -> None:
            assert callbacks

    wx = ModuleType("wx")
    wx.App = App
    dialog = ModuleType("impedance.dialog")
    dialog.ImpedanceDialog = object
    dialog.SpecificationDialog = SpecificationDialog
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "impedance.dialog", dialog)
    assert harness.main(arguments) == 0
    assert len(callbacks) == 1


def test_cached_context_requires_explicit_originating_background_before_native_ui(
    tmp_path: Path, native_boundary: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never guess a current or built-in background for previously plotted colors."""
    source = _context(tmp_path)
    with pytest.raises(SystemExit) as error:
        harness.main(["--context", f"F.Cu={source}"])
    assert error.value.code == 2
    assert "--background-color" in capsys.readouterr().err


@pytest.mark.parametrize("background", ["#f7f9fb", "#001023", "#A1b2C3"])
def test_explicit_cached_background_is_accepted_before_native_ui(
    tmp_path: Path, native_boundary: None, background: str
) -> None:
    """Allow the originating light or dark palette without touching editor preferences."""
    source = _context(tmp_path)
    with pytest.raises(NativeBoundaryReached):
        harness.main(["--context", f"F.Cu={source}", "--background-color", background])


@pytest.mark.parametrize(
    "background",
    [
        "white",
        "#fff",
        "#fffffff",
        "#ffffff80",
        "rgb(1,2,3)",
        "#fff;fill:red",
        "",
        "#gggggg",
    ],
)
def test_parser_rejects_nonliteral_or_nonopaque_backgrounds(
    tmp_path: Path, background: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cached palette requires exact opaque RGB, not CSS or alpha guesses."""
    with pytest.raises(SystemExit) as error:
        harness.parse_preview_args(
            [
                "--context",
                f"F.Cu={_context(tmp_path)}",
                "--background-color",
                background,
            ]
        )
    assert error.value.code == 2
    assert "opaque #rrggbb" in capsys.readouterr().err


def test_background_without_cached_context_is_not_silently_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep palette provenance scoped to the cached plot it describes."""
    with pytest.raises(SystemExit) as error:
        harness.parse_preview_args(["--background-color", "#001023"])
    assert error.value.code == 2
    assert "requires a cached --context" in capsys.readouterr().err


@pytest.mark.parametrize(
    "assignment", ["F.Cu", "F.SilkS={source}", "F.Cu=", "F.Cu={missing}"]
)
def test_parser_rejects_invalid_context_layer_assignment_or_file(
    tmp_path: Path, assignment: str
) -> None:
    """Validate every cached source before importing native UI modules."""
    argument = assignment.format(
        source=_context(tmp_path), missing=tmp_path / "missing.svg"
    )
    with pytest.raises(SystemExit) as error:
        harness.parse_preview_args(
            ["--context", argument, "--background-color", "#001023"]
        )
    assert error.value.code == 2


def test_parser_rejects_duplicate_context_layers_instead_of_replacing_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A layer must have one unambiguous source for its declared palette."""
    first, second = _context(tmp_path), _context(tmp_path, "another.svg")
    with pytest.raises(SystemExit) as error:
        harness.parse_preview_args(
            [
                "--context",
                f"F.Cu={first}",
                "--context",
                f"F.Cu={second}",
                "--background-color",
                "#001023",
            ]
        )
    assert error.value.code == 2
    assert "Only one --context" in capsys.readouterr().err


def test_contexts_and_background_parse_without_importing_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve multiple physical layers and paths containing '=' entirely headlessly."""
    monkeypatch.setitem(sys.modules, "wx", None)
    first, second = _context(tmp_path), _context(tmp_path, "source=inner.svg")
    options = harness.parse_preview_args(
        [
            "--context",
            f"F.Cu={first}",
            "--context",
            f"In2.Cu={second}",
            "--background-color",
            "#A1b2C3",
        ]
    )
    assert options.contexts == {"F.Cu": first.resolve(), "In2.Cu": second.resolve()}
    assert options.background_color == "#a1b2c3"


@pytest.mark.parametrize("background", ["#f7f9fb", "#001023"])
def test_cached_preview_uses_originating_background_without_recoloring_native_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, background: str
) -> None:
    """Exercise production framing with old plotted colors, never current preferences."""
    import impedance.palette
    import impedance.render

    def no_current_theme(*args: Any, **kwargs: Any) -> None:
        """Fail if a cached preview tries to substitute the user's current theme."""
        raise AssertionError("Cached native SVG must use its originating palette")

    monkeypatch.setattr(impedance.palette, "read_theme_context", no_current_theme)
    monkeypatch.setattr(impedance.palette, "read_theme_files", no_current_theme)
    monkeypatch.setattr(impedance.render, "read_theme_context", no_current_theme)
    monkeypatch.setitem(sys.modules, "wx", None)
    case = RF_CASES[0]
    section = analyze(
        Config(enabled=True, specifications=case.specifications()), case.snapshot()
    ).sections[0]
    source = _context(tmp_path)
    actual = harness.cached_preview_svg(section, {section.layer: source}, background)
    root = ET.fromstring(actual)  # noqa: S314 - locally generated regression SVG.
    assert root[0].get("fill") == background
    assert root.get("viewBox") == "0 0 800 420"
    assert any(node.get("stroke") == "#172b3d" for node in root.iter())
    boxes = [node for node in root.iter() if node.get("data-impedance-highlight")]
    assert len(boxes) == 1 and boxes[0].get("data-impedance-highlight") == "box"
    assert source.read_text(encoding="utf-8").count("#172b3d") == 1


def test_cached_preview_reports_missing_layer_and_missing_background(
    tmp_path: Path,
) -> None:
    """Failures remain actionable when previewed layers lack a cached native source."""
    case = RF_CASES[0]
    section = analyze(
        Config(enabled=True, specifications=case.specifications()), case.snapshot()
    ).sections[0]
    with pytest.raises(ValueError, match="--context F.Cu"):
        harness.cached_preview_svg(section, {}, "#001023")
    with pytest.raises(ValueError, match="--background-color"):
        harness.cached_preview_svg(section, {section.layer: _context(tmp_path)}, None)


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_preview_netclass_inputs_retain_full_fixture_membership(
    case: RFCase,
) -> None:
    """One class selects every physical leg and connector launch, including pairs."""
    options = harness.parse_preview_args(["--case", case.name])
    snapshot, specifications = harness.preview_inputs(options)
    assert specifications == (case.netclass_specification(),)
    assert specifications[0].net_class == case.net_class
    assert snapshot.net_classes == (case.net_class,)
    assert dict(snapshot.net_class_memberships) == dict.fromkeys(
        case.net_names, (case.net_class,)
    )
    analysis = analyze(Config(enabled=True, specifications=specifications), snapshot)
    assert {
        trace.trace_id for section in analysis.sections for trace in section.traces
    } == {trace.trace_id for trace in case.traces()}
    assert {section.layer for section in analysis.sections} == {
        leg.layer for leg in case.signal_profiles
    }
    assert specifications == case.specifications()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--specification", "0"],
        ["--specification", "4"],
        ["--specification", "-1"],
        ["--specification", "text"],
        ["--case", RF_CASES[0].name, "--specification", "2"],
        ["--new", "--specification", "1"],
    ],
)
def test_specification_edit_selection_rejects_unavailable_or_conflicting_modes(
    arguments: list[str],
) -> None:
    """Reject missing class specifications and mutually exclusive editor modes."""
    with pytest.raises(SystemExit) as error:
        harness.parse_preview_args(arguments)
    assert error.value.code == 2


def test_combined_board_case_edit_and_new_modes_remain_explicit() -> None:
    """Combined boards have two bank classes; isolated cases have one class."""
    combined = harness.parse_preview_args(["--specification", "2"])
    assert (
        combined.specification == 2 and combined.board is not None and not combined.new
    )
    class_edit = harness.parse_preview_args(
        ["--case", RF_CASES[0].name, "--specification", "1"]
    )
    assert (
        class_edit.specification == 1
        and class_edit.case == RF_CASES[0]
        and not class_edit.new
    )
    new = harness.parse_preview_args(["--new"])
    assert new.specification is None and new.board is not None and new.new
    assert new.contexts == {} and new.background_color is None


@pytest.mark.parametrize(
    "combined", [False, True], ids=["isolated-case", "combined-board"]
)
@pytest.mark.parametrize(
    "raster_failure", [False, True], ids=["rendered", "render-failed"]
)
def test_main_review_callback_keeps_originating_background_and_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    combined: bool,
    raster_failure: bool,
) -> None:
    """Exercise real harness wiring with stateful dialog/raster doubles, never a GUI."""
    import impedance.render

    calls: dict[str, Any] = {}
    wx = ModuleType("wx")
    dialogs = ModuleType("impedance.dialog")

    class App:
        """Record application setup without invoking native framework construction."""

        def __init__(self, redirect: bool) -> None:
            calls["redirect"] = redirect

        def SetAppName(self, name: str) -> None:
            """Retain the configured application name."""
            calls["app_name"] = name

    class Dialog:
        """Run the supplied real callback for an actual matched fixture section."""

        def __init__(
            self,
            parent: object,
            config: Config,
            snapshot: BoardSnapshot,
            preview: Callable[..., CapturedImage],
            refresh: Callable[[], BoardSnapshot],
        ) -> None:
            self.config = config
            self.snapshot = snapshot
            self.preview = preview
            self.refresh = refresh
            calls["config"] = config

        def ShowModal(self) -> None:
            """Verify callback state while temporary capture outputs still exist."""
            assert self.refresh() == self.snapshot
            section = analyze(self.config, self.snapshot).sections[0]
            calls["png"] = self.preview(section, refresh=True)
            assert calls["png"].data == calls["raster_data"]
            assert (calls["png"].width, calls["png"].height) == (800, 420)

        def Destroy(self) -> None:
            """Retain dialog cleanup, including after rendering completes."""
            calls["destroyed"] = True

    def rasterize(svg: Path, png: Path, width: int, height: int) -> None:
        """Model the required output state while inspecting the exact production SVG."""
        calls["svg"] = svg.read_text(encoding="utf-8")
        calls["svg_path"] = svg
        calls["size"] = (width, height)
        if raster_failure:
            raise RuntimeError("Native rasterizer unavailable")

        def chunk(kind: bytes, data: bytes) -> bytes:
            """Write genuine PNG chunks so the real capture validator is exercised."""
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        data = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x40\x80\xc0" * width) * height))
            + chunk(b"IEND", b"")
        )
        calls["raster_data"] = data
        png.write_bytes(data)

    wx.App = App
    dialogs.ImpedanceDialog = Dialog
    dialogs.SpecificationDialog = object
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "impedance.dialog", dialogs)
    monkeypatch.setattr(impedance.render, "_wx_rasterize", rasterize)
    source = _context(tmp_path)
    args = ["--context", f"F.Cu={source}", "--background-color", "#f7f9fb"]
    if not combined:
        args.extend(("--case", RF_CASES[0].name))
    if raster_failure:
        with pytest.raises(RuntimeError, match="Native rasterizer unavailable"):
            harness.main(args)
    else:
        assert harness.main(args) == 0
    svg = ET.fromstring(calls["svg"])  # noqa: S314 - generated local regression SVG.
    assert svg[0].get("fill") == "#f7f9fb"
    assert len(calls["config"].specifications) == (2 if combined else 1)
    assert calls["size"] == (800, 420)
    assert calls["destroyed"]
    assert not calls["svg_path"].parent.exists(), (
        "The harness must clean temporary captures after success or failure"
    )
    assert source.is_file(), "The original cached plot must remain unchanged"
