"""Exercise empty-zone selection and decisions through native wx controls."""

from collections.abc import Iterator
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from copper_zones import EmptyCopperZone
from impedance.service import CapturedImage

from .native_wx_support import pump, run_native, wait_until
from .wx_harness import load_siblings, module

pytestmark = pytest.mark.native_wx


@pytest.fixture
def native() -> Iterator[SimpleNamespace]:
    """Use the real dialog/canvas and a deterministic capture provider."""
    wx = pytest.importorskip("wx")

    class Renderer:
        """Own current image bytes and record actual selection/zoom requests."""

        instances: list[Any] = []

        def __init__(self, board: Any, pcbnew_module: Any) -> None:
            self.board, self.pcbnew = board, pcbnew_module
            self.calls: list[tuple[str, bool]] = []
            self.closed = 0
            self.instances.append(self)

        def render(
            self, region: EmptyCopperZone, *, zoom: bool = False
        ) -> CapturedImage:
            self.calls.append((region.zone_id, zoom))
            if region.geometry_error:
                raise ValueError(region.geometry_error)
            image = wx.Image(400, 240)
            image.SetRGB(wx.Rect(0, 0, 400, 240), 20, 40, 60)
            output = BytesIO()
            assert image.SaveFile(output, wx.BITMAP_TYPE_PNG)
            return CapturedImage(output.getvalue())

        def close(self) -> None:
            self.closed += 1

    package = "_copper_zone_dialog_native"
    with load_siblings(
        package,
        ("copper_zone_dialog",),
        {
            f"{package}.copper_zone_preview": module(
                f"{package}.copper_zone_preview", ZonePreviewRenderer=Renderer
            )
        },
    ) as modules:
        yield SimpleNamespace(
            wx=wx, dialog=modules["copper_zone_dialog"], renderer=Renderer
        )


def _regions() -> list[EmptyCopperZone]:
    """Two identically named pours occupy different parts of the board."""
    return [
        EmptyCopperZone(
            "left", "VIN", 0, "F.Cu", (((0, 0), (1000000, 0), (0, 1000000)),)
        ),
        EmptyCopperZone(
            "right",
            "VIN",
            0,
            "F.Cu",
            (((20000000, 0), (21000000, 0), (20000000, 1000000)),),
        ),
        EmptyCopperZone("missing", "GND", 2, "B.Cu", (), "No boundary available"),
    ]


def _select(wx: Any, dialog: Any, row: int) -> None:
    """Deliver the list event after changing the native selection state."""
    dialog.zones_list.SetSelection(row)
    event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, dialog.zones_list.GetId())
    event.SetEventObject(dialog.zones_list)
    dialog.zones_list.GetEventHandler().ProcessEvent(event)
    pump(wx)


def _click(wx: Any, button: Any) -> None:
    """Run a real button handler through the control's event path."""
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    event.SetEventObject(button)
    button.GetEventHandler().ProcessEvent(event)
    pump(wx)


def _assert_text_fits(wx: Any, control: Any, expected: str) -> None:
    """Check the full label against independently measured native font metrics."""
    assert control.GetLabel().split() == expected.split()
    dc = wx.ClientDC(control)
    dc.SetFont(control.GetFont())
    required = dc.GetMultiLineTextExtent(control.GetLabel())
    allocated = control.GetClientSize()
    assert allocated.GetHeight() >= required.GetHeight(), (
        f"Label needs {required.GetHeight()} pixels, has {allocated.GetHeight()}: "
        f"{control.GetLabel()!r}"
    )
    assert allocated.GetWidth() >= required.GetWidth()


@pytest.mark.parametrize("font_points", [None, 18])
def test_native_caption_is_complete_on_open_resize_and_reopen(
    native: SimpleNamespace, font_points: Optional[int]
) -> None:
    """The final caption line remains visible with normal and larger text."""

    def exercise(frame: Any, wx: Any) -> None:
        expected = (
            "The yellow outline marks the empty zone's intended boundary. "
            "Other tracks or pads may occupy that region. The preview uses existing "
            "copper fills, which may be out of date if refilling is disabled."
        )
        for _ in range(2):
            dialog = native.dialog.EmptyCopperZonesDialog(
                frame, object(), object(), _regions()
            )
            if font_points is not None:
                for control in (dialog.caption, dialog.warning):
                    font = control.GetFont()
                    font.SetPointSize(font_points)
                    control.SetFont(font)
                dialog.SendSizeEvent()
            dialog.Show()
            pump(wx)
            try:
                if font_points is not None:
                    assert dialog.caption.GetFont().GetPointSize() == font_points
                for size in (None, (720, 480), (1100, 700), (720, 480)):
                    if size is not None:
                        dialog.SetSize(size)
                        pump(wx)
                    _assert_text_fits(wx, dialog.caption, expected)
                    _assert_text_fits(wx, dialog.warning, dialog._warning_text)
                    height = dialog.GetClientSize().GetHeight()
                    for button in (dialog.continue_button, dialog.cancel_button):
                        assert button.GetRect().GetBottom() <= height
                assert native.renderer.instances[-1].calls == [("left", False)]
            finally:
                dialog.Destroy()
                pump(wx)

    run_native(exercise)


def test_native_selection_zoom_and_failure_clear_current_preview(
    native: SimpleNamespace,
) -> None:
    """Duplicate labels have distinct locations and failed selections discard old pixels."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        dialog.Show()
        pump(wx)
        renderer = native.renderer.instances[-1]
        try:
            assert dialog.zones_list.GetSelection() == 0
            assert renderer.calls == [("left", False)]
            wait_until(wx, lambda: dialog.preview._bitmap is not None)
            assert dialog.zones_list.GetString(0) != dialog.zones_list.GetString(1)
            assert "VIN on F.Cu" in dialog.zones_list.GetString(0)
            _select(wx, dialog, 1)
            assert renderer.calls[-1] == ("right", False)
            assert "20.000" in dialog.detail.GetValue()
            _click(wx, dialog.zoom_button)
            assert renderer.calls[-1] == ("right", True)
            _click(wx, dialog.overview_button)
            assert renderer.calls[-1] == ("right", False)
            _select(wx, dialog, 2)
            assert dialog.preview._image is None
            assert "No boundary available" in dialog.preview_status.GetLabel()
            assert dialog.cancel_button.IsEnabled()
            assert dialog.continue_button.IsEnabled()
            dialog.SetSize((720, 500))
            pump(wx)
            assert renderer.calls[-1] == ("missing", False)
        finally:
            dialog.Destroy()
        assert renderer.closed == 1

    run_native(exercise)


def test_native_resize_keeps_warning_and_long_location_readable(
    native: SimpleNamespace,
) -> None:
    """Narrowing the dialog wraps explanations without clipping or replotting."""

    def exercise(frame: Any, wx: Any) -> None:
        region = _regions()[0]
        long_region = EmptyCopperZone(
            region.zone_id,
            "/" + "Long hierarchical net " * 12,
            region.layer_id,
            region.layer_name,
            region.contours,
        )
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), [long_region]
        )
        dialog.Show()
        pump(wx)
        try:
            dialog.SetSize((720, 500))
            pump(wx)
            assert dialog.GetSize() == wx.Size(720, 500)
            assert long_region.label in dialog.detail.GetValue()
            explanations = []
            for prefix in ("These copper zones", "The yellow outline"):
                text = next(
                    child
                    for child in dialog.GetChildren()
                    if isinstance(child, wx.StaticText)
                    and child.GetLabel().startswith(prefix)
                )
                assert text.GetBestSize().GetWidth() <= text.GetSize().GetWidth()
                explanations.append(text)
            width, height = dialog.GetClientSize()
            for control in (
                dialog.preview,
                dialog.detail,
                dialog.cancel_button,
                dialog.continue_button,
                *explanations,
            ):
                x, y = control.GetPosition()
                w, h = control.GetSize()
                assert x >= 0 and y >= 0
                assert x + w <= width and y + h <= height
            assert native.renderer.instances[-1].calls == [("left", False)]
        finally:
            dialog.Destroy()

    run_native(exercise)


def test_native_renderer_initialization_failure_keeps_locations(
    native: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing native plotting leaves an explicit warning and accurate board XY."""

    def unavailable(_board: Any, _pcbnew: Any) -> Any:
        raise RuntimeError("Native plotting unavailable")

    monkeypatch.setattr(native.dialog, "ZonePreviewRenderer", unavailable)

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        dialog.Show()
        pump(wx)
        try:
            assert "Native plotting unavailable" in dialog.preview_status.GetLabel()
            assert "Board X 0.000 to 1.000 mm" in dialog.detail.GetValue()
            _select(wx, dialog, 1)
            assert "Board X 20.000 to 21.000 mm" in dialog.detail.GetValue()
            assert dialog.preview._image is None
        finally:
            dialog.Destroy()

    run_native(exercise)


def test_native_parent_destruction_closes_renderer(native: SimpleNamespace) -> None:
    """Native parent teardown can bypass Python Destroy but still releases context."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        dialog.Show()
        pump(wx)
        renderer = native.renderer.instances[-1]
        frame.Destroy()
        wait_until(wx, lambda: not dialog)
        assert renderer.closed == 1

    run_native(exercise)


def test_native_deferred_paint_failure_clears_pixels(native: SimpleNamespace) -> None:
    """A failed display callback clears visible pixels while retaining zone context."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        dialog.Show()
        pump(wx)
        try:
            wait_until(wx, lambda: dialog.preview._bitmap is not None)
            wx.CallAfter(
                dialog.preview._paint_failed,
                dialog.preview._generation,
                "Display failed",
            )
            pump(wx)
            assert dialog.preview._image is None
            assert "Display failed" in dialog.preview_status.GetLabel()
            assert "VIN on F.Cu" in dialog.detail.GetValue()
            assert dialog.continue_button.IsEnabled()
        finally:
            dialog.Destroy()

    run_native(exercise)


def test_native_error_text_reflows_after_shrinking(native: SimpleNamespace) -> None:
    """A failed preview's explanation stays readable when its canvas narrows."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        dialog.Show()
        pump(wx)
        try:
            blank_height = dialog.preview_status.GetSize().GetHeight()
            message = "Native plotting could not display the selected copper zone. " * 5
            dialog._preview_failed(message)
            dialog.SetSize((720, 500))
            pump(wx)
            status = dialog.preview_status
            _assert_text_fits(wx, status, "Preview unavailable: " + message)
            assert status.GetBestSize().GetWidth() <= status.GetSize().GetWidth()
            assert (
                status.GetLabel().split() == ("Preview unavailable: " + message).split()
            )
            width, height = dialog.GetClientSize()
            for control in (
                dialog.detail,
                dialog.cancel_button,
                dialog.continue_button,
            ):
                x, y = control.GetPosition()
                w, h = control.GetSize()
                assert x >= 0 and y >= 0
                assert x + w <= width and y + h <= height
            _select(wx, dialog, 1)
            assert dialog.preview_status.GetLabel() == ""
            assert dialog.preview_status.GetSize().GetHeight() == blank_height
        finally:
            dialog.Destroy()

    run_native(exercise)


@pytest.mark.parametrize("action", ["continue", "cancel", "close", "escape"])
def test_native_modal_decisions_and_cleanup(
    native: SimpleNamespace, action: str
) -> None:
    """Only explicit Continue proceeds; Cancel is default and closing stops review."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.dialog.EmptyCopperZonesDialog(
            frame, object(), object(), _regions()
        )
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                assert dialog.GetEscapeId() == wx.ID_CANCEL
                assert dialog.GetDefaultItem() is dialog.cancel_button
                if action == "close":
                    dialog.Close()
                elif action == "escape":
                    event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                    event.SetKeyCode(wx.WXK_ESCAPE)
                    event.SetEventObject(dialog)
                    dialog.GetEventHandler().ProcessEvent(event)
                    assert not dialog.IsModal()
                else:
                    _click(
                        wx,
                        dialog.continue_button
                        if action == "continue"
                        else dialog.cancel_button,
                    )
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        timer = wx.CallLater(20, interact)
        try:
            result = dialog.ShowModal()
            assert not failures, failures
            assert result == (wx.ID_YES if action == "continue" else wx.ID_CANCEL)
        finally:
            timer.Stop()
            dialog.Destroy()
        assert native.renderer.instances[-1].closed == 1

    run_native(exercise)
