"""Read real workbook paint pixels for alternating rows and native selection."""

from base64 import b64decode
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts.linux_native_impedance_smoke import event

from .native_wx_support import isolated_native, pump, run_native
from .test_impedance_draft_dialog import _intent
from .test_impedance_inclusion_order import _mixed_board
from .test_impedance_row_index_native import _assert_rows, _include

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_LABELS = (
    "Incl.",
    "Row",
    "Layer",
    "Net class",
    "Target Ω",
    "Length",
    "Net",
    "Width",
    "",
    "Target width",
    "Segments",
)


def _api(monkeypatch: pytest.MonkeyPatch, name: str) -> SimpleNamespace:
    """Load production controls without importing the KiCad plugin entry point."""
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "lib"))
    package = ModuleType(name)
    package.__path__ = [str(root / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    return SimpleNamespace(
        **{
            module: importlib.import_module(f"{name}.{module}")
            for module in ("model", "dialog", "service", "workbook_grid")
        }
    )


def _rgb(colour: Any) -> tuple[int, int, int]:
    """Read only RGB components, independently of native alpha conventions."""
    return tuple(colour[index] for index in range(3))


def _expected_row(grid: Any, row: int) -> tuple[int, int, int]:
    """Specify variants' existing 10% light / 16% dark alternate-row policy."""
    base = _rgb(grid.GetDefaultCellBackgroundColour())
    if not row % 2:
        return base
    text = _rgb(grid.GetDefaultCellTextColour())
    amount = 0.16 if sum(base) < 384 else 0.10
    return tuple(
        round(back * (1 - amount) + ink * amount) for back, ink in zip(base, text)
    )


def _solid_rgb(colour: Any) -> tuple[int, int, int]:
    """Account for the native bitmap's colour profile using an independent fill."""
    import wx

    bitmap = wx.Bitmap(12, 12)
    dc = wx.MemoryDC(bitmap)
    try:
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(colour))
        dc.DrawRectangle(wx.Rect(0, 0, 12, 12))
    finally:
        dc.SelectObject(wx.NullBitmap)
    image = bitmap.ConvertToImage()
    return (image.GetRed(5, 5), image.GetGreen(5, 5), image.GetBlue(5, 5))


def _paint_cell(grid: Any, row: int, col: int, selected: bool) -> tuple[Any, ...]:
    """Use the real cell renderer, native checkbox, and bitmap RGB pixel reads."""
    import wx
    import wx.grid as gridlib

    rect = wx.Rect(4, 4, 240, 40)
    bitmap = wx.Bitmap(248, 48)
    dc = wx.MemoryDC(bitmap)
    attr = gridlib.GridCellAttr()
    attr.SetFont(grid.GetCellFont(row, col))
    attr.SetTextColour(grid.GetCellTextColour(row, col))
    attr.SetBackgroundColour(grid.GetCellBackgroundColour(row, col))
    text_colours: list[tuple[int, int, int]] = []
    checkboxes: list[int] = []
    fills: list[tuple[int, int, int]] = []
    draw_text = dc.DrawText
    draw_rectangle = dc.DrawRectangle
    draw_checkbox = wx.RendererNative.Get().DrawCheckBox

    def observe_text(text: str, x: int, y: int) -> None:
        text_colours.append(_rgb(dc.GetTextForeground()))
        draw_text(text, x, y)

    def observe_fill(bounds: Any) -> None:
        fills.append(_rgb(dc.GetBrush().GetColour()))
        draw_rectangle(bounds)

    def observe_checkbox(
        _renderer: Any, window: Any, context: Any, bounds: Any, flags: int = 0
    ) -> None:
        checkboxes.append(flags)
        draw_checkbox(window, context, bounds, flags)

    try:
        dc.SetBackground(wx.Brush(wx.Colour(255, 0, 255)))
        dc.Clear()
        with (
            patch.object(dc, "DrawText", observe_text),
            patch.object(dc, "DrawRectangle", observe_fill),
            patch.object(wx.RendererNative, "DrawCheckBox", observe_checkbox),
        ):
            grid.GetCellRenderer(row, col).Draw(
                grid, attr, dc, rect, row, col, selected
            )
    finally:
        dc.SelectObject(wx.NullBitmap)
    image = bitmap.ConvertToImage()
    # Sample all four corners inside the fill, outside text and checkbox artwork.
    colours = tuple(
        (image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y))
        for x, y in ((5, 5), (242, 5), (5, 42), (242, 42))
    )
    assert all(colour == colours[0] for colour in colours), (row, col, colours)
    assert bool(checkboxes) == (col == 0)
    if col == 0:
        assert len(checkboxes) == 1
        assert bool(checkboxes[0] & wx.CONTROL_CHECKED) == grid.GetToggleValue(row, 0)
    else:
        assert len(text_colours) == 1
    assert len(fills) == 1
    return colours[0], text_colours, fills[0]


def _assert_rows_painted(grid: Any) -> None:
    """Check all eleven columns in every unselected and selected displayed row."""
    import wx

    for selected in (False, True):
        for row in range(grid.GetNumberRows()):
            background = (
                _rgb(grid.GetSelectionBackground())
                if selected
                else _expected_row(grid, row)
            )
            native_background = (
                grid.GetSelectionBackground()
                if selected
                else wx.Colour(*background)
                if row % 2
                else grid.GetDefaultCellBackgroundColour()
            )
            for col in range(grid.GetNumberCols()):
                diagnostic = (
                    (120, 120, 120)
                    if grid.GetTextValue(row, 7) == "Covered stub"
                    else (180, 140, 0)
                    if col in (7, 8, 9) and grid.GetTextValue(row, 8) == "!="
                    else None
                )
                assert _rgb(grid.GetCellTextColour(row, col)) == (
                    diagnostic or _rgb(grid.GetDefaultCellTextColour())
                )
                painted, foreground, fill = _paint_cell(grid, row, col, selected)
                assert fill == background, (row, col, selected, fill, background)
                assert painted == _solid_rgb(native_background), (
                    row,
                    col,
                    selected,
                    painted,
                    background,
                )
                if col == 0:
                    continue
                expected = (
                    _rgb(grid.GetSelectionForeground())
                    if selected
                    else diagnostic or _rgb(grid.GetDefaultCellTextColour())
                )
                assert foreground == [expected], (
                    row,
                    col,
                    selected,
                    foreground,
                    expected,
                )


@pytest.mark.parametrize(
    "base,text",
    [((250, 248, 246), (20, 40, 60)), ((32, 34, 36), (220, 225, 230))],
    ids=["light", "dark"],
)
def test_native_alternating_fill_all_columns_preserves_selection_and_cues(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    base: tuple[int, int, int],
    text: tuple[int, int, int],
) -> None:
    """Native fills use displayed parity while muted and amber glyphs remain intact."""
    if isolated_native(request, "KICAD_IMPEDANCE_STRIPE_PAINT_CHILD"):
        return
    api = _api(monkeypatch, "_impedance_stripe_paint_native")

    def exercise(frame: Any, wx: Any) -> None:
        grid = api.workbook_grid.WorkbookGrid(frame, _LABELS)
        grid.SetDefaultCellBackgroundColour(wx.Colour(*base))
        grid.SetDefaultCellTextColour(wx.Colour(*text))
        for row, (width, marker) in enumerate(
            (("0.15 mm", ""), ("Covered stub", ""), ("0.15 mm", "!="), ("0.2 mm", "!="))
        ):
            grid.AppendItem(
                [
                    bool(row % 2),
                    str(row + 1),
                    "F.Cu",
                    "RF",
                    "50",
                    "1 mm",
                    "NET",
                    width,
                    marker,
                    "0.18 mm",
                    "1",
                ]
            )
        grid.SelectRow(1)
        grid.fit_columns()
        pump(wx)
        _assert_rows_painted(grid)
        next_base, next_text = (
            ((32, 34, 36), (220, 225, 230))
            if sum(base) >= 384
            else ((250, 248, 246), (20, 40, 60))
        )
        get_colour = wx.SystemSettings.GetColour

        def theme_colour(identifier: int) -> Any:
            """Provide deterministic system colours while retaining native fallbacks."""
            if identifier == wx.SYS_COLOUR_LISTBOX:
                return wx.Colour(*next_base)
            if identifier == wx.SYS_COLOUR_LISTBOXTEXT:
                return wx.Colour(*next_text)
            return get_colour(identifier)

        changed = wx.SysColourChangedEvent()
        changed.SetEventObject(grid)
        with patch.object(wx.SystemSettings, "GetColour", theme_colour):
            grid.GetEventHandler().ProcessEvent(changed)
            pump(wx)
        assert _rgb(grid.GetDefaultCellBackgroundColour()) == next_base
        assert _rgb(grid.GetDefaultCellTextColour()) == next_text
        assert grid.GetSelectedRow() == 1
        _assert_rows_painted(grid)

    run_native(exercise)


def test_native_dialog_stripes_follow_inclusion_reorder_refresh_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Moving one section changes its stripe by display index without changing intent."""
    if isolated_native(request, "KICAD_IMPEDANCE_STRIPE_WORKFLOW_CHILD"):
        return
    api = _api(monkeypatch, "_impedance_stripe_workflow_native")
    snapshot = _mixed_board(api)
    saved: list[Any] = []

    def exercise(frame: Any, wx: Any) -> None:
        def open_dialog(config: Any) -> Any:
            """Keep real constructor, events, section projection, and persistence."""
            return api.dialog.ImpedanceDialog(
                frame,
                config,
                snapshot,
                preview=lambda *_args, **_kwargs: api.service.CapturedImage(_PNG),
                refresh_snapshot=lambda: snapshot,
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(_intent(api))
        failures: list[BaseException] = []
        state: list[Any] = []

        def interact() -> None:
            try:
                _assert_rows(dialog, 6)
                before = dialog.session.config.to_dict()
                _assert_rows_painted(dialog.sections)
                assert dialog.session.config.to_dict() == before
                target_id = dialog._row_ids[1]
                before_background = _paint_cell(dialog.sections, 1, 1, False)[0]
                _include(dialog, 1, False)
                moved_row = dialog._row_ids.index(target_id)
                assert moved_row == 4
                assert (
                    _paint_cell(dialog.sections, moved_row, 1, False)[0]
                    != before_background
                )
                after = dialog.session.config.to_dict()
                canonical_ids = {
                    section.section_id for section in dialog.session.analysis.sections
                }
                assert all(
                    image["section_id"] in canonical_ids
                    for image in after["review_tracking"]["images"]
                )
                # Visiting the moved section records its captured image review.
                # Presentation stripes must add no persisted fields or alter intent.
                before.pop("review_tracking")
                after.pop("review_tracking")
                assert after == before
                _assert_rows(dialog, 6)
                before_draw = dialog.session.config.to_dict()
                _assert_rows_painted(dialog.sections)
                assert dialog.session.config.to_dict() == before_draw
                assert dialog._refresh_rows()
                pump(wx)
                _assert_rows(dialog, 6)
                _assert_rows_painted(dialog.sections)
                assert dialog._row_ids[dialog.sections.GetSelectedRow()] == target_id
                assert (
                    dialog.preview_pane._sections[dialog.preview_pane._index].section_id
                    == target_id
                )
                event(wx, dialog.approve_button, wx.EVT_BUTTON)
                assert dialog.session.approved
                state.append((dialog._row_ids, set(dialog.session.included)))
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK
            assert len(saved) == 1
        finally:
            dialog.Destroy()
            pump(wx)
        reopened = open_dialog(api.model.Config.from_dict(saved[0].to_dict()))
        try:
            _assert_rows(reopened, 6)
            assert (reopened._row_ids, reopened.session.included) == state[0]
            before_draw = reopened.session.config.to_dict()
            _assert_rows_painted(reopened.sections)
            assert reopened.session.config.to_dict() == before_draw
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)
