"""Footprint presentation preferences and tooltips through actual native controls."""

from types import ModuleType
from typing import Any

import pytest

from .native_wx_support import pump
from .variant_matrix_native_test_support import matrix, modules, native_marks
from .variant_matrix_render_test_support import resize, sample_model

__all__ = ["matrix", "modules"]
pytestmark = native_marks


@pytest.mark.parametrize("saved_mode", [True, False])
@pytest.mark.parametrize("current_mode", [True, False])
def test_footprint_width_only_survives_the_same_display_format(
    matrix: Any,
    modules: tuple[ModuleType, ModuleType],
    saved_mode: bool,
    current_mode: bool,
) -> None:
    def check(h: Any) -> None:
        view = h.view
        view.restore_preferences(
            {
                "widths": {"footprint": 480, "ref": 130},
                "show_footprint_library": saved_mode,
            }
        )
        assert view.capture_preferences()["show_footprint_library"] is current_mode
        assert view.capture_preferences()["widths"] == (
            {"footprint": 480, "ref": 130}
            if saved_mode == current_mode
            else {"ref": 130}
        )
        assert view.GetColSize(0) == view.FromDIP(130)
        assert (view.GetColSize(1) == view.FromDIP(480)) is (saved_mode == current_mode)

    matrix(check, model=sample_model(modules[1], show_library=current_mode))


def test_footprint_hover_and_width_follow_mode_refresh_reopening_and_fonts(
    matrix: Any, modules: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    short, full = sample_model(modules[1]), sample_model(modules[1], show_library=True)

    def check(h: Any) -> None:
        view, wx = h.view, h.wx
        col, lcsc = 1, short.column_for("A", "lcsc")
        identifier = short.get_value(0, col)
        short_width = view.GetColSize(col)
        h.click(1, lcsc)
        target = view.selected_target
        selection = h.selection()
        chosen = view.GetColSize(lcsc) + view.FromDIP(40)
        resize(h, lcsc, chosen)
        font, label = wx.Font(view.GetDefaultCellFont()), wx.Font(view.GetLabelFont())
        for scale in (1, 1.5, 1):
            view.SetDefaultCellFont(font.Scaled(scale))
            view.SetLabelFont(label.Scaled(scale))
            pump(wx)
            compact = view.GetColSize(col)
            for model in (short, full, full, short):
                view.set_model(model)
                assert view.selected_target == target and h.selection() == selection
                assert view.GetColSize(lcsc) == chosen
                assert (view.GetColSize(col) > compact) is model.show_footprint_library
                expected = "" if model.show_footprint_library else identifier
                window, _point = h.cell_point(0, col)
                setter, calls = window.SetToolTip, []

                def set_tip(
                    text: str, captured: list[str] = calls, original: Any = setter
                ) -> None:
                    captured.append(text)
                    original(text)

                with monkeypatch.context() as patch:
                    patch.setattr(window, "SetToolTip", set_tip)
                    tooltip = h.hover(0, col)
                    assert tooltip.endswith(expected) if expected else tooltip == ""
                    count = len(calls)
                    assert h.hover(0, col) == tooltip and len(calls) == count
            if scale == 1:
                assert view.GetColSize(col) == short_width
        view.set_model(full)
        resize(h, col, view.GetColSize(col) + 50)
        saved = view.capture_preferences()
        view.set_model(short)
        assert (
            view.GetColSize(col) == short_width
            and "footprint" not in view.capture_preferences()["widths"]
        )
        view.set_model(sample_model(modules[1], rows=0))
        view.set_model(short)
        assert h.hover(0, col).endswith(identifier)
        h.reopen(model=short, preferences=saved)
        assert (
            h.view.GetColSize(col) == short_width and h.view.GetColSize(lcsc) == chosen
        )
        assert h.hover(0, col).endswith(identifier)
        window, _point = h.cell_point(0, col)
        h.mouse(window, wx.wxEVT_MOTION, wx.Point(-2, -2))
        tooltip = window.GetToolTip()
        assert tooltip is None or tooltip.GetTip() == ""

    matrix(check, model=short)
