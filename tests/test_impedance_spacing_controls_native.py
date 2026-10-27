"""Real wx controls review separate, readable spacing images and exact consent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import importlib
import os
from pathlib import Path
import struct
import sys
from types import ModuleType
from typing import Any
import zlib

import pytest

from scripts.linux_native_impedance_smoke import capture_window, event, write_json
from scripts.native_impedance_workflow import approve_spacing_review

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_dialog_preview import _png
from .test_impedance_workbook_sizing_native import assert_columns_fit

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]
_BACKGROUND = (17, 43, 73)
_COPPER = (230, 170, 30)
_PNG = _png(_BACKGROUND)


ScreenRect = tuple[int, int, int, int]


def _wait_specification_footer_paint(wx: Any, footer: Any) -> None:
    """Observe GTK allocation through fresh paint before measuring requested bounds."""
    if wx.Platform != "__WXGTK__":
        return
    painted = None

    def geometry() -> tuple[tuple[int, int], tuple[int, int]]:
        """Identify the requested layout without testing any acceptance bounds."""
        return tuple(footer.GetPosition()), tuple(footer.GetClientSize())

    def observe(event: Any) -> None:
        nonlocal painted
        size = footer.GetClientSize()
        # GTK3 paints after native layout; wx can report the requested position
        # before allocation, including while a child still uses its old origin.
        if footer.GetUpdateRegion().Contains(wx.Rect((0, 0), size)) == wx.InRegion:
            painted = geometry()
        event.Skip()

    footer.Bind(wx.EVT_PAINT, observe)
    try:
        footer.Refresh()
        wait_until(
            wx,
            lambda: painted is not None and painted == geometry(),
            reason="The specification footer did not receive its current native allocation paint.",
        )
    finally:
        footer.Unbind(wx.EVT_PAINT, handler=observe)


def _independent_button_best_size(wx: Any, parent: Any, button: Any) -> Any:
    """Measure a fresh native control outside the hierarchy whose bounds are tested."""
    reference = wx.Button(parent, label=button.GetLabel())
    try:
        reference.SetFont(button.GetFont())
        reference.InvalidateBestSize()
        return reference.GetBestSize()
    finally:
        reference.Hide()
        reference.Destroy()


def test_native_workbook_spacing_precision_survives_refresh_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Rounded native cells must retain exact nanometre verdicts and saved targets."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_PRECISION_CHILD"):
        return
    name = "_impedance_spacing_precision_native"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    dialogs = importlib.import_module(f"{name}.dialog")
    model = importlib.import_module(f"{name}.model")
    service = importlib.import_module(f"{name}.service")
    specification = model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (model.LayerSettings("F.Cu", ("B.Cu",), 203_205),),
    )

    def board(gap_nm: int) -> Any:
        """Declare exact copper edge distances independently of display rounding."""
        return model.BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=tuple(
                model.Trace(
                    net,
                    "F.Cu",
                    net,
                    150_000,
                    (
                        (0, side * (150_000 + gap_nm)),
                        (2_000_000, side * (150_000 + gap_nm)),
                    ),
                )
                for side, net in enumerate(("USB_P", "USB_N"))
            ),
            net_classes=("USB",),
            net_class_memberships=(("USB_P", ("USB",)), ("USB_N", ("USB",))),
            net_class_context_digest="spacing-precision-native-membership",
            differential_pairs=(("USB_P", "USB_N"),),
        )

    def exercise(frame: Any, wx: Any) -> None:
        """Inspect real grid text and native colors before and after reconstruction."""
        current = [board(204_205)]

        def open_dialog(config: Any) -> Any:
            return dialogs.ImpedanceDialog(
                frame,
                config,
                current[0],
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=lambda: current[0],
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
            )

        def assert_cells(dialog: Any, *, mismatch: bool) -> None:
            """Keep the one-nanometre threshold crossing visible in the verdict."""
            table = dialog.sections
            assert table.GetItemCount() == 1
            assert table.GetTextValue(0, 11) == "0.20421 mm"
            assert table.GetTextValue(0, 13) == "0.20321 mm"
            assert table.GetTextValue(0, 12) == ("!=" if mismatch else "")
            assert table.GetTextValue(0, 14) == ("Deviation" if mismatch else "Match")
            for column in (11, 12, 13, 14):
                assert table.GetCellTextColour(0, column) == (
                    wx.Colour(180, 140, 0)
                    if mismatch
                    else table.GetDefaultCellTextColour()
                )

        dialog = open_dialog(model.Config(specifications=(specification,)))
        try:
            dialog.Show()
            pump(wx)
            assert_cells(dialog, mismatch=False)
            current[0] = board(204_206)
            assert dialog._refresh_rows(current[0])
            pump(wx)
            assert_cells(dialog, mismatch=True)
            config = model.Config.from_dict(dialog.session.config.to_dict())
            assert config.specifications[0].layer_settings[0].spacing_nm == 203_205
        finally:
            dialog.Destroy()
            pump(wx)
        reopened = open_dialog(config)
        try:
            reopened.Show()
            pump(wx)
            assert_cells(reopened, mismatch=True)
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("enlarged", [False, True], ids=["normal-font", "large-font"])
def test_native_specification_approval_button_fits_after_list_selection(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    enlarged: bool,
) -> None:
    """Changing image type must retain native label space without manual resizing."""
    if isolated_native(request, "KICAD_IMPEDANCE_APPROVAL_BUTTON_SIZE_CHILD"):
        return
    name = "_impedance_approval_button_size_native"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    dialogs = importlib.import_module(f"{name}.dialog")
    model = importlib.import_module(f"{name}.model")
    service = importlib.import_module(f"{name}.service")
    spec = model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (model.LayerSettings("F.Cu", ("B.Cu",), 200_000),),
    )
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                net,
                "F.Cu",
                net,
                150_000,
                ((0, side * 350_000), (40_000_000, side * 350_000)),
            )
            for side, net in enumerate(("USB_P", "USB_N"))
        ),
        net_classes=("USB",),
        net_class_memberships=(("USB_P", ("USB",)), ("USB_N", ("USB",))),
        net_class_context_digest="approval-button-native-membership",
        differential_pairs=(("USB_P", "USB_N"),),
    )

    def exercise(frame: Any, wx: Any) -> None:
        """Compare real button allocations with an independent fresh native control."""
        if enlarged:
            native_button = wx.Button

            class LargeNativeButton(native_button):
                """Keep actual native controls while selecting a larger native font."""

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.SetFont(self.GetFont().Scaled(1.4))

            monkeypatch.setattr(wx, "Button", LargeNativeButton)
        editor = dialogs.SpecificationDialog(
            frame,
            snapshot,
            spec,
            preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
        )
        observations: list[dict[str, Any]] = []
        try:
            editor.Show()
            pump(wx)
            pane = editor.preview_pane
            assert not pane.spacing_entries
            editor.spacing.SetValue("0.15")
            event(wx, editor.spacing, wx.EVT_TEXT)
            pump(wx)
            trace = pane.sections[0]
            spacing = pane.spacing_entries[0]
            minimum = tuple(editor.GetMinSize())
            sizes = (
                minimum,
                (minimum[0] + 100, minimum[1] + 60),
                minimum,
            )
            for size in sizes:
                editor.SetSize(size)
                pump(wx)
                _wait_specification_footer_paint(wx, editor.footer)
                fixed_size = tuple(editor.GetSize())
                for entry in (trace, spacing, trace, spacing):
                    index = pane.entries.index(entry)
                    editor.image_list.SetItemState(
                        index,
                        wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                        wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                    )
                    wait_until(
                        wx,
                        lambda entry=entry: pane.selected_entry == entry
                        and pane.displayed,
                    )
                    pump(wx)
                    _wait_specification_footer_paint(wx, editor.footer)
                    button = editor.approve_layer
                    # Compare with a new control rather than stale GetBestSize()
                    # on the button whose label was changed by the real handler.
                    native_best = _independent_button_best_size(wx, frame, button)
                    bounds = button.GetScreenRect()
                    footer = editor.footer.GetScreenRect()
                    client_origin = editor.ClientToScreen((0, 0))
                    client = wx.Rect(client_origin, editor.GetClientSize())
                    observation = {
                        "dialog_size": fixed_size,
                        "label": button.GetLabel(),
                        "button": tuple(bounds),
                        "native_best": tuple(native_best),
                        "font_points": button.GetFont().GetFractionalPointSize(),
                        "footer": tuple(footer),
                    }
                    observations.append(observation)
                    write_json(
                        tmp_path / f"approval-button-geometry-{len(observations)}.json",
                        observation,
                    )
                    assert button.IsShownOnScreen(), observation
                    assert bounds.width >= native_best.width, observation
                    assert bounds.height >= native_best.height, observation
                    assert footer.Contains(bounds), observation
                    assert client.Contains(bounds), observation
                    assert tuple(editor.GetSize()) == fixed_size, observation
                    for sibling in editor.footer.GetChildren():
                        if (
                            sibling is not button
                            and sibling.IsShownOnScreen()
                            and sibling.GetSize().width > 0
                            and sibling.GetSize().height > 0
                        ):
                            assert not bounds.Intersects(sibling.GetScreenRect()), (
                                observation,
                                sibling.GetLabel(),
                                tuple(sibling.GetScreenRect()),
                            )
            assert not editor.review_tracking.spacing_images
        finally:
            editor.Destroy()
            pump(wx)

    run_native(exercise)


def _assert_review_geometry(
    work_area: ScreenRect,
    frame: ScreenRect,
    client: ScreenRect,
    controls: Mapping[str, ScreenRect],
    *,
    maximized: bool,
) -> None:
    """Require complete visible content while accounting for maximized native borders."""

    def contained(outer: ScreenRect, inner: ScreenRect) -> bool:
        """Compare complete rectangles, including their right and bottom edges."""
        x, y, width, height = outer
        left, top, w, h = inner
        return (
            x <= left and y <= top and left + w <= x + width and top + h <= y + height
        )

    evidence = work_area, frame, client, controls, maximized
    assert contained(work_area, client), evidence
    if not maximized:
        # Only maximized native frames may hide borders outside the work area.
        assert contained(work_area, frame), evidence
    for name, bounds in controls.items():
        assert contained(work_area, bounds), (name, evidence)
        assert contained(client, bounds), (name, evidence)


def _review_geometry_ready(
    viewer: Any, work_area: ScreenRect, *, maximized: bool
) -> bool:
    """Wait for actual native bounds as well as the independently delivered state bit."""
    if viewer.IsMaximized() != maximized:
        return False
    origin = viewer.ClientToScreen((0, 0))
    size = viewer.GetClientSize()
    controls = {
        str(index): tuple(control.GetScreenRect().Get())
        for index, control in enumerate(viewer.GetChildren())
        if control.IsShownOnScreen()
    }
    try:
        _assert_review_geometry(
            work_area,
            tuple(viewer.GetScreenRect().Get()),
            (origin.x, origin.y, size.width, size.height),
            controls,
            maximized=maximized,
        )
    except AssertionError:
        return False
    return True


def _closeup_png(entry: Any, *, original: bool = False) -> bytes:
    """Draw the actual deterministic straight copper at its physical review scale."""
    left, top, width, height = entry.viewport_nm
    detail_left, detail_top, detail_width, detail_height = entry.composition.detail_rect
    image_width, image_height = entry.width_px, entry.height_px
    if original:
        detail_left = detail_top = 0
        image_width, image_height = detail_width, detail_height
    rows = [bytearray(bytes(_BACKGROUND) * image_width) for _ in range(image_height)]
    for trace in entry.traces:
        first, last = trace.points[0], trace.points[-1]
        assert first[1] == last[1]
        start = detail_left + max(
            0, round((min(first[0], last[0]) - left) * detail_width / width)
        )
        end = (
            min(
                detail_width,
                round((max(first[0], last[0]) - left) * detail_width / width),
            )
            + detail_left
        )
        upper = detail_top + max(
            0, round((first[1] - trace.width_nm / 2 - top) * detail_height / height)
        )
        lower = (
            min(
                detail_height,
                round((first[1] + trace.width_nm / 2 - top) * detail_height / height),
            )
            + detail_top
        )
        for row in rows[upper:lower]:
            row[start * 3 : end * 3] = bytes(_COPPER) * (end - start)

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Encode one standard PNG chunk without an optional image dependency."""
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", image_width, image_height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\0" + bytes(row) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# Real maximize/unmaximize transitions require the isolated desktop's window manager.
@pytest.mark.os_input
@pytest.mark.parametrize("layer", ["F.Cu", "B.Cu"])
def test_native_spacing_events_refresh_repair_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    layer: str,
    tmp_path: Path,
) -> None:
    """Real events require each concern, preserve pixels on resize and restore consent."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CONTROLS_CHILD"):
        return
    package_name = "_impedance_spacing_controls_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialogs = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    tracking = importlib.import_module(f"{package_name}.review_tracking")
    spec = model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (
            model.LayerSettings(
                layer, ("B.Cu" if layer == "F.Cu" else "F.Cu",), 200_000
            ),
        ),
    )
    pairs = (("USB_P", "USB_N"), ("OTHER_P", "OTHER_N"))
    traces = tuple(
        model.Trace(
            f"{index}-{side}",
            layer,
            net,
            150_000,
            (
                (0, index * 3_000_000 + side * 350_000),
                (40_000_000, index * 3_000_000 + side * 350_000),
            ),
        )
        for index, pair in enumerate(pairs)
        for side, net in enumerate(pair)
    )
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=traces,
        net_classes=("USB",),
        net_class_memberships=tuple((net, ("USB",)) for pair in pairs for net in pair),
        differential_pairs=pairs,
        net_class_context_digest="native-spacing-controls",
    )
    current = [snapshot]
    saved: list[Any] = []

    def capture(entry: Any, *_args: Any, **_kwargs: Any) -> Any:
        """Use distinct capture bytes and exact diagnostic viewports."""
        if hasattr(entry, "view_id"):
            return service.CapturedImage(
                _closeup_png(entry),
                original=service.CapturedImage(
                    _closeup_png(entry, original=True), viewport_nm=entry.viewport_nm
                ),
            )
        return service.CapturedImage(
            _PNG, viewport_nm=(-300_000, -300_000, 2_600_000, 1_365_000)
        )

    def assert_native_readable_copper(viewer: Any, wx: Any) -> dict[str, Any]:
        """Read actual mapped pixels: two substantial traces and a visible clear gap."""
        canvas = viewer.canvas
        detail = viewer._composition.detail_rect
        wait_until(wx, lambda: canvas._recorded and viewer.readable)
        assert canvas._scaled_view == (
            0,
            0,
            viewer._capture.width,
            viewer._capture.height,
        )
        assert canvas._visible_source_rect == (
            0,
            0,
            viewer._capture.width,
            viewer._capture.height,
        )
        assert canvas._painted_scale >= viewer._composition.minimum_scale
        assert canvas._presentation_crop is None
        physical_view = viewer._capture.original.viewport_nm
        assert physical_view is not None
        physical_left, _physical_top, physical_width, _physical_height = physical_view
        # Both deterministic straight tracks run from x=0 to x=40mm. The worst
        # witness can be at their endpoint, so the detail's center may land on
        # the fixture raster's half-open boundary. Probe a known common interior
        # cross-section rather than requiring copper outside those tracks.
        common_left = max(0, physical_left)
        common_right = min(40_000_000, physical_left + physical_width)
        assert common_left < common_right
        probe_board_x = (common_left + common_right) / 2
        probe_source_x = (
            detail[0] + (probe_board_x - physical_left) / physical_width * detail[2]
        )
        latest: list[Any] = []
        observed: list[object] = []

        def readable() -> bool:
            if not canvas.IsShownOnScreen() or min(canvas._dimensions) <= 0:
                return False
            canvas.Update()
            source = wx.ClientDC(canvas)
            width, height = canvas.GetClientSize()
            bitmap = wx.Bitmap(width, height)
            target = wx.MemoryDC(bitmap)
            try:
                assert target.Blit(0, 0, width, height, source, 0, 0)
            finally:
                target.SelectObject(wx.NullBitmap)
            pixels = bitmap.ConvertToImage()
            latest[:] = [pixels]
            drawn_width, drawn_height = canvas._dimensions
            left, top = (width - drawn_width) // 2, (height - drawn_height) // 2
            right, bottom = left + drawn_width, top + drawn_height
            view_left = canvas._visible_source_rect[0]
            x = round(left + (probe_source_x - view_left) * canvas._painted_scale)
            bands = []
            start = None
            for y in range(top, bottom):
                red, green, blue = (
                    pixels.GetRed(x, y),
                    pixels.GetGreen(x, y),
                    pixels.GetBlue(x, y),
                )
                copper = red > 150 and green > 100 and blue < 100
                if copper and start is None:
                    start = y
                elif not copper and start is not None:
                    bands.append((start, y))
                    start = None
            observed[:] = [(left, top, right, bottom), bands]
            return (
                len(bands) == 2
                and all(end - start >= 8 for start, end in bands)
                and bands[1][0] - bands[0][1] >= 8
            )

        canvas.Refresh()
        try:
            wait_until(
                wx,
                readable,
                reason="The native close-up did not show two readable traces and their unobscured gap.",
            )
        except AssertionError as error:
            directory = Path(os.environ.get("NATIVE_WX_ARTIFACTS", str(tmp_path)))
            directory.mkdir(parents=True, exist_ok=True)
            artifact = directory / f"spacing-closeup-{layer.replace('.', '-')}.png"
            if latest:
                latest[0].SaveFile(str(artifact), wx.BITMAP_TYPE_PNG)
            raise AssertionError(
                f"{error} Observed {observed}; capture: {artifact}"
            ) from error

        bands = observed[1]
        return {
            "source_dimensions": (viewer._capture.width, viewer._capture.height),
            "canvas_dimensions": tuple(canvas.GetClientSize()),
            "fit_dimensions": canvas._dimensions,
            "fit_scale": canvas._painted_scale,
            "source_rect": canvas._visible_source_rect,
            "probe_board_x_nm": probe_board_x,
            "copper_bands": bands,
            "copper_widths_px": tuple(end - start for start, end in bands),
            "gap_px": bands[1][0] - bands[0][1],
        }

    def exercise(frame: Any, wx: Any) -> None:
        artifacts = (
            Path(os.environ.get("NATIVE_WX_ARTIFACTS", str(tmp_path)))
            / tmp_path.parent.name
            / layer.replace(".", "-")
        )
        artifacts.mkdir(parents=True, exist_ok=True)
        preview_module = importlib.import_module(f"{package_name}.dialog_preview")
        large_review_count = 0
        maximize_restore_count = 0

        def viewer_geometry(viewer: Any) -> dict[str, Any]:
            """Independently compare every visible review control with native bounds."""
            area = preview_module._display_client_area(viewer)
            origin = viewer.ClientToScreen((0, 0))
            client = viewer.GetClientSize()
            client_bounds = (origin.x, origin.y, client.width, client.height)
            bounds = tuple(viewer.GetScreenRect().Get())
            controls = {
                "canvas": viewer.canvas,
                "caption": viewer.caption,
                "status": viewer.status,
                "approve": viewer.approve_button,
                "cancel": viewer.FindWindow(wx.ID_CANCEL),
            }
            native_button_best = _independent_button_best_size(
                wx, frame, viewer.approve_button
            )
            approve_bounds = viewer.approve_button.GetScreenRect()
            assert approve_bounds.width >= native_button_best.width
            assert approve_bounds.height >= native_button_best.height
            assert not approve_bounds.Intersects(controls["cancel"].GetScreenRect())

            descendants = list(viewer.GetChildren())
            visible_buttons = []
            while descendants:
                control = descendants.pop()
                if isinstance(control, wx.Button):
                    label = control.GetLabel().replace("&", "").strip()
                    assert label not in {"Fit image", "Original image"}, label
                    if control.IsShownOnScreen():
                        visible_buttons.append(label)
                descendants.extend(control.GetChildren())
            assert sorted(visible_buttons) == ["Approve spacing exception", "Cancel"]
            evidence = {
                "visible_buttons": visible_buttons,
                "work_area": area,
                "frame": bounds,
                "client": client_bounds,
                "maximized": viewer.IsMaximized(),
                "controls": {
                    name: tuple(control.GetScreenRect().Get())
                    for name, control in controls.items()
                },
            }
            _assert_review_geometry(
                area,
                bounds,
                client_bounds,
                evidence["controls"],
                maximized=evidence["maximized"],
            )
            for name, control in controls.items():
                assert control.IsShownOnScreen(), (name, evidence)
            return evidence

        def inspect_maximize_restore(viewer: Any) -> dict[str, Any]:
            """Deliver real native maximize/restore transitions and retain their Fit proof."""
            assert viewer.GetWindowStyleFlag() & wx.MAXIMIZE_BOX
            assert not viewer.IsMaximized() and not viewer.approved
            capture = viewer._capture
            image = viewer.canvas._image
            image_hash = capture.sha256
            before_generation = viewer.canvas._generation
            viewer.Maximize(True)
            wait_until(
                wx,
                lambda: viewer.IsMaximized()
                and _review_geometry_ready(
                    viewer, preview_module._display_client_area(viewer), maximized=True
                )
                and viewer.canvas._generation > before_generation
                and viewer.canvas._recorded
                and viewer.readable
                and viewer.approve_button.IsEnabled(),
                reason="Native maximize did not produce a fresh readable full-image Fit paint.",
            )
            maximized = viewer_geometry(viewer)
            maximized_pixels = assert_native_readable_copper(viewer, wx)
            assert viewer._capture is capture and viewer._capture.sha256 == image_hash
            assert viewer.canvas._image is image and not viewer.approved
            maximized_generation = viewer.canvas._generation
            # Request actual unmaximization: wxGTK Restore() only presents the window.
            viewer.Maximize(False)
            wait_until(
                wx,
                lambda: not viewer.IsMaximized()
                and _review_geometry_ready(
                    viewer, preview_module._display_client_area(viewer), maximized=False
                )
                and viewer.canvas._generation > maximized_generation
                and viewer.canvas._recorded
                and viewer.readable
                and viewer.approve_button.IsEnabled(),
                reason="Native restore did not produce a fresh readable full-image Fit paint.",
            )
            restored = viewer_geometry(viewer)
            restored_pixels = assert_native_readable_copper(viewer, wx)
            assert viewer._capture is capture and viewer._capture.sha256 == image_hash
            assert viewer.canvas._image is image and not viewer.approved
            return {
                "capture_sha256": image_hash,
                "maximized": maximized,
                "maximized_fit_pixels": maximized_pixels,
                "restored": restored,
                "restored_fit_pixels": restored_pixels,
                "approved_before_explicit_click": viewer.approved,
            }

        def inspect_large(viewer: Any) -> None:
            """Prove the minimum feature remains readable after actual resize paint."""
            nonlocal large_review_count, maximize_restore_count
            before = viewer_geometry(viewer)
            before_pixels = assert_native_readable_copper(viewer, wx)
            image = viewer.canvas._image
            size = viewer.GetSize()
            x, y, width, height = before["work_area"]
            position = viewer.GetScreenPosition()
            grow_y = min(10, y + height - position.y - size.height)
            grow_x = min(20, x + width - position.x - size.width)
            assert grow_x > 0 or grow_y > 0, before
            viewer.SetSize((size.width + grow_x, size.height + grow_y))
            pump(wx)
            wait_until(
                wx, lambda: viewer.readable and viewer.approve_button.IsEnabled()
            )
            after = viewer_geometry(viewer)
            after_pixels = assert_native_readable_copper(viewer, wx)
            assert viewer.canvas._image is image
            native_area = wx.Display(wx.Display.GetFromWindow(viewer)).GetClientArea()
            maximize_restore = None
            if maximize_restore_count == 0 and before["work_area"] == (
                native_area.x,
                native_area.y,
                native_area.width,
                native_area.height,
            ):
                maximize_restore = inspect_maximize_restore(viewer)
                maximize_restore_count += 1
            write_json(
                artifacts / f"spacing-viewer-{large_review_count}.geometry.json",
                {
                    "before_resize": before,
                    "after_resize": after,
                    "initial_fit_pixels": before_pixels,
                    "resized_fit_pixels": after_pixels,
                    "maximize_restore": maximize_restore,
                },
            )
            large_review_count += 1

        def verify_fixture(expected: Any) -> None:
            """Preserve live-source failure semantics around native modal acceptance."""
            if current[0] != expected:
                raise ValueError("The native fixture source changed during review")

        draft = replace(
            spec,
            target_ohms="",
            kind="differential_coplanar",
            layer_settings=(model.LayerSettings(layer, (), 200_000),),
        )
        editor = dialogs.SpecificationDialog(frame, snapshot, draft, preview=capture)
        try:
            editor.Show()
            pump(wx)
            assert "Routed spacing matches" in editor.spacing_comparison.GetLabel()
            for entered, label in (
                ("0.15", "routed spacing issue"),
                ("", "Needs target"),
                ("0.2", "Routed spacing matches"),
            ):
                editor.spacing.SetValue(entered)
                event(wx, editor.spacing, wx.EVT_TEXT)
                pump(wx)
                assert label in editor.spacing_comparison.GetLabel()
                pane = editor.preview_pane
                if label == "routed spacing issue":
                    assert len(pane.spacing_entries) == 2
                    pane.select(pane.entries.index(pane.spacing_entries[0]))
                    wait_until(
                        wx,
                        lambda pane=pane: pane.capture_loaded
                        and min(pane.canvas._dimensions) > 0,
                    )
                    pump(wx)
                    entry = pane.selected_entry
                    assert (
                        tracking.find_spacing_image(
                            editor.review_tracking,
                            entry.spec_id,
                            entry.layer,
                            entry.view_id,
                        )
                        is None
                    ), "The compact detail received full-composition viewing evidence"
                    summary = pane.caption.GetLabel().replace("\n", " ")
                    assert summary == "Shown gap 0.2 mm · target 0.15 mm"
                    assert pane.selected_entry.caption not in summary
                    assert pane.caption.IsShown()
                    viewed, png = dict(pane._viewed), pane._capture
                    size = editor.GetSize()
                    editor.SetSize((size.width + 80, size.height + 20))
                    pump(wx)
                    assert pane._viewed == viewed and pane._capture is png
                    assert (
                        tracking.find_spacing_image(
                            editor.review_tracking,
                            entry.spec_id,
                            entry.layer,
                            entry.view_id,
                        )
                        is None
                    )
                else:
                    assert pane.spacing_entries == ()
            editor.kind.SetSelection(0)
            event(wx, editor.kind, wx.EVT_CHOICE)
            pump(wx)
            assert "Routed spacing matches" not in editor.spacing_comparison.GetLabel()
            assert (
                editor.spacing_comparison.GetRect().GetBottom()
                <= editor.ground_gap.GetRect().GetTop()
            )
        finally:
            editor.Destroy()
            pump(wx)

        reviewed_spec = replace(
            spec,
            layer_settings=(
                model.LayerSettings(
                    layer, spec.layer_settings[0].reference_layers, 150_000
                ),
            ),
        )
        editor = dialogs.SpecificationDialog(
            frame,
            snapshot,
            reviewed_spec,
            preview=capture,
            verify_snapshot=verify_fixture,
        )
        try:
            editor.Show()
            pump(wx)
            editor.SetSize((980, 620))
            pump(wx)
            capture_window(
                wx,
                editor,
                artifacts / f"specification-minimum-{layer.replace('.', '-')}.png",
            )
            pane = editor.preview_pane
            assert len(pane.spacing_entries) == 2
            entries = tuple(pane.spacing_entries)
            for index, entry in enumerate(entries):
                pane.select(pane.entries.index(entry))
                wait_until(wx, lambda: pane.displayed)
                wait_until(wx, lambda: editor.approve_layer.IsEnabled())
                assert editor.approve_layer.GetLabel() == "Approve spacing exception"
                assert (
                    tracking.find_spacing_image(
                        editor.review_tracking,
                        entry.spec_id,
                        entry.layer,
                        entry.view_id,
                    )
                    is None
                )
                if index == 0:
                    area = preview_module._display_client_area(editor)
                    compact = (area[0], area[1], min(area[2], 800), min(area[3], 600))

                    def inspect_compact(viewer: Any) -> None:
                        """Retain native compact-display containment and readable pixels."""
                        inspect_large(viewer)
                        assert (
                            viewer.canvas._painted_scale
                            >= viewer._composition.minimum_scale
                        )
                        assert_native_readable_copper(viewer, wx)
                        write_json(
                            artifacts
                            / f"spacing-compact-{layer.replace('.', '-')}.geometry.json",
                            viewer_geometry(viewer),
                        )

                    with monkeypatch.context() as compact_display:
                        compact_display.setattr(
                            preview_module,
                            "_display_client_area",
                            lambda _parent, area=compact: area,
                        )
                        approve_spacing_review(
                            wx.GetApp(),
                            wx,
                            editor,
                            editor.approve_layer,
                            entry,
                            cancel=True,
                            inspect=inspect_compact,
                            capture_path=(
                                artifacts
                                / f"spacing-compact-{layer.replace('.', '-')}.png"
                            ),
                        )
                    record = tracking.find_spacing_image(
                        editor.review_tracking,
                        entry.spec_id,
                        entry.layer,
                        entry.view_id,
                    )
                    assert record is not None and not record.approved_digest

                with monkeypatch.context() as approval_display:
                    if index == 0:
                        approval_display.setattr(
                            preview_module,
                            "_display_client_area",
                            lambda _parent, area=compact: area,
                        )
                    approve_spacing_review(
                        wx.GetApp(),
                        wx,
                        editor,
                        editor.approve_layer,
                        entry,
                        inspect=inspect_large,
                        capture_path=(
                            artifacts
                            / f"spacing-compact-approved-{layer.replace('.', '-')}.png"
                            if index == 0
                            else None
                        ),
                    )
                record = tracking.find_spacing_image(
                    editor.review_tracking, entry.spec_id, entry.layer, entry.view_id
                )
                assert tracking.spacing_image_current(
                    record, entry.input_digest, approved=True
                )
            assert len(editor.review_tracking.spacing_images) == len(entries)
        finally:
            editor.Destroy()
            pump(wx)

        editor = dialogs.SpecificationDialog(
            frame,
            snapshot,
            reviewed_spec,
            preview=capture,
            verify_snapshot=verify_fixture,
        )
        try:
            editor.Show()
            pump(wx)
            pane = editor.preview_pane
            entry = pane.spacing_entries[0]
            pane.select(pane.entries.index(entry))
            wait_until(wx, lambda: pane.displayed)
            wait_until(wx, lambda: editor.approve_layer.IsEnabled())

            def stale_source(viewer: Any) -> None:
                """Reject a changed source through the actual approval handler."""
                inspect_large(viewer)
                current[0] = replace(
                    snapshot, context_digest="source changed in large review"
                )
                try:
                    event(wx, viewer.approve_button, wx.EVT_BUTTON)
                    assert not viewer.approved and not viewer.readable
                    assert not viewer.approve_button.IsEnabled()
                finally:
                    current[0] = snapshot

            approve_spacing_review(
                wx.GetApp(),
                wx,
                editor,
                editor.approve_layer,
                entry,
                cancel=True,
                inspect=stale_source,
            )
            record = tracking.find_spacing_image(
                editor.review_tracking, entry.spec_id, entry.layer, entry.view_id
            )
            assert record is not None and not record.approved_digest
            assert not pane.entries and not editor._spacing_capture_hashes
        finally:
            editor.Destroy()
            pump(wx)

        def open_dialog(config: Any) -> Any:
            return dialogs.ImpedanceDialog(
                frame,
                config,
                current[0],
                preview=capture,
                refresh_snapshot=lambda: current[0],
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(model.Config(specifications=(spec,)))
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                table = dialog.sections
                assert table.GetColLabelValue(11) == "Pair spacing"
                assert table.GetColLabelValue(14) == "Spacing check"
                assert table.GetTextValue(0, 11) == "0.2 mm"
                assert table.GetTextValue(0, 12) == ""
                assert table.GetTextValue(0, 13) == "0.2 mm"
                assert table.GetTextValue(0, 14) == "Match"
                for offset, verdict in (
                    (250_000, "Deviation"),
                    (350_000, "Match"),
                    (250_000, "Deviation"),
                ):
                    current[0] = replace(
                        snapshot,
                        traces=tuple(
                            replace(
                                trace,
                                points=tuple(
                                    (x, y - 350_000 + offset) for x, y in trace.points
                                ),
                            )
                            if index % 2
                            else trace
                            for index, trace in enumerate(traces)
                        ),
                    )
                    assert dialog._refresh_rows(current[0])
                    pump(wx)
                    assert table.GetTextValue(0, 14).startswith(verdict)
                    assert table.GetTextValue(0, 11) == (
                        f"{model.format_length(offset - 150_000)} mm"
                    )
                    assert table.GetTextValue(0, 12) == (
                        "!=" if verdict == "Deviation" else ""
                    )
                    assert table.GetTextValue(0, 13) == "0.2 mm"
                    for column in (11, 12, 13):
                        assert table.GetCellTextColour(0, column) == (
                            wx.Colour(180, 140, 0)
                            if verdict == "Deviation"
                            else table.GetDefaultCellTextColour()
                        )
                    assert_columns_fit(table, expected_rows=2)
                pane = dialog.preview_pane
                entries = tuple(dialog.session.spacing_entries)
                assert len(entries) == 2
                assert len(dialog.session.pending_spacing_reviews) == len(entries)
                assert table.GetItemCount() == 2
                dialog.SetSize((980, 620))
                pump(wx)
                capture_window(
                    wx,
                    dialog,
                    artifacts / f"main-minimum-{layer.replace('.', '-')}.png",
                )
                for section in pane.sections:
                    pane.select(pane.entries.index(section))
                    wait_until(wx, lambda: pane.displayed)
                    assert not pane.caption.IsShown()
                assert not dialog.session.approved
                assert len(dialog.session.pending_spacing_reviews) == len(entries)
                for entry in entries:
                    pane.select(pane.entries.index(entry))
                    try:
                        wait_until(wx, lambda: pane.displayed)
                    except AssertionError as error:
                        raise AssertionError(
                            f"Spacing thumbnail failed: {pane.failure!r}; "
                            f"client={tuple(pane.canvas.GetClientSize())}; "
                            f"capture_loaded={pane.capture_loaded}; "
                            f"painted={pane.canvas._painted_size}; "
                            f"recorded={pane.canvas._recorded}; "
                            f"pane_size={tuple(pane.window.GetClientSize())}; "
                            f"caption={pane.caption.GetLabel()!r}; "
                            f"caption_best={tuple(pane.caption.GetBestSize())}; "
                            f"status_size={tuple(pane.status.GetSize())}"
                        ) from error
                    summary = pane.caption.GetLabel().replace("\n", " ")
                    assert "gap 0.1 mm" in summary and "target 0.2 mm" in summary
                    assert entry.caption not in summary
                    assert (
                        tracking.find_spacing_image(
                            dialog.session.config.review_tracking,
                            entry.spec_id,
                            entry.layer,
                            entry.view_id,
                        )
                        is None
                    )
                    assert (
                        dialog.spacing_approve_button.GetLabel()
                        == "Open spacing review"
                    )
                    wait_until(wx, lambda: dialog.spacing_approve_button.IsEnabled())
                    before = len(dialog.session.pending_spacing_reviews)
                    approve_spacing_review(
                        wx.GetApp(),
                        wx,
                        dialog,
                        dialog.spacing_approve_button,
                        entry,
                        inspect=inspect_large,
                    )
                    assert dialog.session.spacing_review_current(entry)
                    assert len(dialog.session.pending_spacing_reviews) == before - 1
                assert not dialog.session.pending_spacing_reviews
                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                assert dialog.session.approved
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK and len(saved) == 1
        finally:
            dialog.Destroy()
            pump(wx)
        history = saved[0].review_tracking.spacing_images
        saved_entries = tuple(
            (entry.view_id, entry.input_digest, capture(entry).sha256)
            for entry in dialog.session.spacing_entries
        )
        assert len(history) == 2 and all(record.approved_at_utc for record in history)
        reopened = open_dialog(model.Config.from_dict(saved[0].to_dict()))
        try:
            reopened.Show()
            wait_until(wx, lambda: not reopened._capture_restore_pending)
            assert not reopened.session.pending_spacing_reviews
            assert reopened.session.config.review_tracking.spacing_images == history
            assert reopened.session.approved
            assert (
                tuple(
                    (entry.view_id, entry.input_digest, capture(entry).sha256)
                    for entry in reopened.session.spacing_entries
                )
                == saved_entries
            )
            for entry in reopened.session.spacing_entries:
                digest = tracking.spacing_capture_fingerprint(
                    entry.input_digest, capture(entry).sha256
                )
                assert reopened.session.spacing_capture_digests[entry.view_id] == digest
            assert_columns_fit(reopened.sections, expected_rows=2)
        finally:
            reopened.Destroy()
            pump(wx)
        assert maximize_restore_count == 1, "Native maximize/restore was not exercised."
        assert current[0].traces == tuple(
            replace(trace, points=tuple((x, y - 100_000) for x, y in trace.points))
            if index % 2
            else trace
            for index, trace in enumerate(traces)
        )

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]
