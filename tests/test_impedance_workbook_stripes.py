"""Observe the real workbook renderer using explicit stateful drawing doubles."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from .test_impedance_automatic_rows import _show
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_inclusion_order import _click, _mixed_board
from .test_impedance_width_mismatch import _config

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import Config

draft_api = _draft_api


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep color workflows independent of unrelated catalog requests."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _paint(
    api: SimpleNamespace,
    dialog: ImpedanceDialog,
    row: int,
    column: int,
    *,
    selected: bool = False,
) -> tuple[object, object]:
    """Record actual renderer fill/text decisions, without claiming native pixels."""
    grid = dialog.sections
    dc = api.wx.ClientDC(grid)
    grid.GetCellRenderer(row, column).Draw(
        grid,
        grid.GetCellAttr(row, column),
        dc,
        api.wx.Rect(0, 0, grid.GetColSize(column), grid.GetRowSize(row)),
        row,
        column,
        selected,
    )
    return dc.brush, getattr(dc, "foreground", None)


@pytest.mark.parametrize(
    "background,foreground,stripe",
    [
        ((255, 255, 255), (0, 0, 0), (230, 230, 230)),
        ((0, 0, 0), (240, 240, 240), (38, 38, 38)),
        ((30, 30, 30), (240, 240, 240), (64, 64, 64)),
    ],
)
def test_all_columns_share_variant_style_stripes_and_native_selection(
    draft_api: SimpleNamespace,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
    stripe: tuple[int, int, int],
) -> None:
    """A checkbox and every text cell use the same displayed-row fill in each theme."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    grid = dialog.sections
    grid.SetDefaultCellBackgroundColour(background)
    grid.SetDefaultCellTextColour(foreground)
    for selected in (False, True):
        for row in (0, 1):
            expected = (
                grid.GetSelectionBackground()
                if selected
                else (stripe if row else background)
            )
            for column in range(11):
                fill, text = _paint(api, dialog, row, column, selected=selected)
                assert fill == expected, (row, column, selected)
                if not selected and row == 0:
                    assert fill is background
                if column:
                    assert text == (
                        grid.GetSelectionForeground() if selected else foreground
                    )


@pytest.mark.parametrize(
    "selection_background,selection_foreground",
    [
        ((0, 120, 215), (255, 255, 255)),
        ((210, 225, 250), (0, 0, 0)),
        ((55, 55, 55), (255, 255, 255)),
    ],
)
@pytest.mark.parametrize("covered_stub", [False, True])
def test_native_selection_text_overrides_warning_and_stub_colours(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    selection_background: tuple[int, int, int],
    selection_foreground: tuple[int, int, int],
    covered_stub: bool,
) -> None:
    """Selecting and deselecting preserves native contrast and diagnostic text."""
    api = draft_api
    dialog = _open(api, _config(api), _mixed_board(api))
    monkeypatch.setattr(
        dialog.sections, "GetSelectionBackground", lambda: selection_background
    )
    monkeypatch.setattr(
        dialog.sections, "GetSelectionForeground", lambda: selection_foreground
    )
    painted = 0
    for row, section_id in enumerate(dialog._row_ids):
        section = next(
            section
            for section in dialog.session.analysis.sections
            if section.section_id == section_id
        )
        if bool(section.exclusion_reason) != covered_stub:
            continue
        painted += 1
        before = tuple(dialog.sections.GetTable().rows[row])
        for selected in (False, True, False):
            expected_fill = (
                dialog.sections.GetSelectionBackground()
                if selected
                else ((230, 230, 230) if row % 2 else (255, 255, 255))
            )
            for column in range(11):
                fill, text = _paint(api, dialog, row, column, selected=selected)
                assert fill == expected_fill
                if column:
                    expected_text = (
                        selection_foreground
                        if selected
                        else (120, 120, 120)
                        if section.exclusion_reason
                        else (180, 140, 0)
                        if column in (7, 8, 9)
                        else (0, 0, 0)
                    )
                    assert text == expected_text
            assert tuple(dialog.sections.GetTable().rows[row]) == before
    assert painted > 0


def test_stripes_follow_display_position_after_toggle_refresh_and_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """Section Row numbers remain canonical while backgrounds follow sorted position."""
    api = draft_api
    board = _mixed_board(api)
    saved: list[Config] = []
    dialog = _open(api, _intent(api), board, saved.append)
    _show(api, dialog)
    moved_id = dialog._row_ids[1]
    moved_number = dialog.sections.GetTextValue(1, 1)
    assert moved_number == "3"
    assert _paint(api, dialog, 1, 1)[0] == (230, 230, 230)
    _click(dialog, 1)
    api.drain()
    api.paint(dialog.preview_pane)
    moved_row = dialog._row_ids.index(moved_id)
    assert moved_row == 4
    assert dialog.sections.GetSelectedRow() == moved_row
    assert dialog.sections.GetTextValue(moved_row, 1) == moved_number
    assert _paint(api, dialog, moved_row, 1)[0] == (255, 255, 255)
    assert _paint(api, dialog, 0, 1)[0] == (255, 255, 255)
    assert _paint(api, dialog, 1, 1)[0] == (230, 230, 230)
    assert dialog._refresh_rows()
    assert _paint(api, dialog, 1, 1)[0] == (230, 230, 230)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    reopened = _open(api, api.model.Config.from_dict(saved[-1].to_dict()), board)
    assert reopened._row_ids == dialog._row_ids
    assert reopened.session.approved
    for row in range(6):
        assert _paint(api, reopened, row, 1)[0] == (
            (230, 230, 230) if row % 2 else (255, 255, 255)
        )


def test_theme_change_repaints_existing_rows_without_editing_configuration(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Light/dark transitions refresh ordinary text as well as alternating fills."""
    api = draft_api
    dialog = _open(api, _config(api), _mixed_board(api))
    _show(api, dialog)
    grid = dialog.sections
    before = (dialog.session.config, dialog._row_ids, dialog.sections.GetSelectedRow())
    colors = {}
    original = api.wx.SystemSettings.GetColour
    monkeypatch.setattr(
        api.wx.SystemSettings,
        "GetColour",
        lambda kind: colors[kind] if kind in colors else original(kind),
    )
    for background, foreground, stripe in (
        ((30, 30, 30), (240, 240, 240), (64, 64, 64)),
        ((255, 255, 255), (0, 0, 0), (230, 230, 230)),
    ):
        colors[api.wx.SYS_COLOUR_LISTBOX] = background
        colors[api.wx.SYS_COLOUR_LISTBOXTEXT] = foreground
        grid.emit(api.wx.EVT_SYS_COLOUR_CHANGED)
        api.drain()
        assert grid.GetDefaultCellBackgroundColour() == background
        assert grid.GetDefaultCellTextColour() == foreground
        assert _paint(api, dialog, 1, 1) == (stripe, foreground)
        assert _paint(api, dialog, 1, 7) == (stripe, (180, 140, 0))
        assert grid.GetCellTextColour(1, 1) == foreground
        assert (dialog.session.config, dialog._row_ids, grid.GetSelectedRow()) == before
