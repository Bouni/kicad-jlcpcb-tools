"""Selection-independent table filters through real constructor and event handlers."""

from collections.abc import Callable, Iterator
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from . import test_window_layout as layout_support

mainwindow = layout_support.mainwindow
FILTERS = (mainwindow.ID_HIDE_BOM, mainwindow.ID_HIDE_POS)
PART_ACTIONS = (
    mainwindow.ID_SELECT_PART,
    mainwindow.ID_REMOVE_LCSC_NUMBER,
    mainwindow.ID_TOGGLE_BOM_POS,
    mainwindow.ID_TOGGLE_BOM,
    mainwindow.ID_TOGGLE_POS,
    mainwindow.ID_PART_DETAILS,
)


def _toolbar(*_args: Any, **_kwargs: Any) -> MagicMock:
    """Retain tool state and parent availability as independent native properties."""
    toolbar = MagicMock()
    toolbar.available = True
    toolbar.tools = {}

    def add(tool_id: int, label: str, *_args: Any) -> MagicMock:
        tool = MagicMock()
        tool.enabled, tool.checked, tool.label = True, False, label
        tool.GetId.return_value = tool_id
        tool.SetLabel.side_effect = lambda label: setattr(tool, "label", label)
        toolbar.tools[tool_id] = tool
        return tool

    toolbar.AddTool.side_effect = toolbar.AddCheckTool.side_effect = add
    toolbar.Enable.side_effect = lambda enabled: setattr(toolbar, "available", enabled)
    toolbar.EnableTool.side_effect = lambda tool_id, enabled: setattr(
        toolbar.tools[tool_id], "enabled", enabled
    )
    toolbar.ToggleTool.side_effect = lambda tool_id, checked: setattr(
        toolbar.tools[tool_id], "checked", checked
    )
    return toolbar


@pytest.fixture
def open_window(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """Construct the dialog with stateful selection, tool bindings and visible rows."""
    layout_support._after.clear()
    bind_dialog = layout_support._Dialog.Bind

    def bind(
        window: Any,
        event_type: Any,
        handler: Callable,
        source: Any = None,
        **kwargs: Any,
    ) -> None:
        if event_type == mainwindow.wx.EVT_TOOL:
            window._bindings[event_type, source.GetId()] = handler
        else:
            bind_dialog(window, event_type, handler, **kwargs)

    monkeypatch.setattr(layout_support._Dialog, "Bind", bind)
    monkeypatch.setattr(mainwindow.wx, "ToolBar", _toolbar)
    monkeypatch.setattr(mainwindow.wx, "PostEvent", MagicMock(), raising=False)

    def open_dialog(references: Optional[list[str]] = None) -> Any:  # noqa: UP045
        model = MagicMock()
        monkeypatch.setattr(
            mainwindow,
            "PartListDataModel",
            MagicMock(
                columns=layout_support.datamodel.PartListDataModel.columns,
                return_value=model,
            ),
        )
        window = layout_support._open_main(monkeypatch, {})
        parts = [
            {
                "reference": reference,
                "value": "10k",
                "footprint": "R_0603",
                "lcsc": "",
                "exclude_from_bom": bom,
                "exclude_from_pos": pos,
            }
            for reference, bom, pos in (
                ("R1", 0, 0),
                ("R2", 1, 0),
                ("R3", 0, 1),
                ("R4", 1, 1),
            )
            if references is None or reference in references
        ]
        window.store = SimpleNamespace(read_all=lambda: parts)
        window.library.read_correction_data = lambda: SimpleNamespace(corrections=())
        footprints = {part["reference"]: MagicMock() for part in parts}
        for footprint in footprints.values():
            footprint.GetLayer.return_value = 0
        window.pcbnew.GetBoard().FindFootprintByReference.side_effect = footprints.get
        window.pcbnew.GetCurrentSelection.return_value = []
        selections: list[str] = []
        rows: dict[str, list[Any]] = {}
        table = window.footprint_list
        table.GetSelections.side_effect = lambda: list(selections)
        table.GetSelectedItemsCount.side_effect = lambda: len(selections)
        selection_handler = next(
            call.args[1]
            for call in table.Bind.call_args_list
            if call.args[0] == mainwindow.dv.EVT_DATAVIEW_SELECTION_CHANGED
        )

        def select(*references: str) -> None:
            assert set(references) <= rows.keys()
            selections[:] = references
            selection_handler(MagicMock())

        def clear_rows() -> None:
            rows.clear()
            if selections:
                # Model reset can notify deselection; state changes before dispatch.
                selections.clear()
                selection_handler(MagicMock())

        def click(tool_id: int) -> None:
            toolbar = window.right_toolbar
            tool = toolbar.tools[tool_id]
            assert toolbar.available and tool.enabled, "Filter is disabled"
            tool.checked = not tool.checked
            window._bindings[mainwindow.wx.EVT_TOOL, tool_id](
                SimpleNamespace(IsChecked=lambda: tool.checked)
            )

        model.RemoveAll.side_effect = clear_rows
        model.AddEntry.side_effect = lambda row: rows.update({row[0]: row})
        model.get_reference.side_effect = lambda item: item
        window.populate_footprint_list()
        return SimpleNamespace(
            window=window, rows=rows, parts=parts, select=select, click=click
        )

    yield open_dialog
    layout_support._after.clear()


def test_filters_available_on_open_reopen_and_selection_changes(
    open_window: Callable[..., Any],
) -> None:
    """Opening and selecting zero, one or multiple rows never disables filters."""
    for _ in range(2):
        ui = open_window()
        toolbar = ui.window.right_toolbar
        assert not ui.window.hide_bom_parts and not ui.window.hide_pos_parts
        assert set(ui.rows) == {"R1", "R2", "R3", "R4"}
        assert all(toolbar.tools[tool].enabled for tool in FILTERS)
        assert not any(toolbar.tools[tool].enabled for tool in PART_ACTIONS)
        for references in (("R1",), ("R1", "R2"), ()):
            ui.select(*references)
            assert all(toolbar.tools[tool].enabled for tool in FILTERS)
            assert all(
                toolbar.tools[tool].enabled == bool(references) for tool in PART_ACTIONS
            )
        for tool in FILTERS:
            ui.click(tool)
        assert set(ui.rows) == {"R1"}
        ui.window.Close()


@pytest.mark.parametrize("first", FILTERS)
def test_filters_combine_without_selection_or_changing_exclusions(
    open_window: Callable[..., Any],
    first: int,
) -> None:
    """Both filter orders produce the union of exclusions and restore original rows."""
    ui = open_window()
    before = deepcopy(ui.parts)
    second = next(tool for tool in FILTERS if tool != first)
    single_filter_rows = {
        mainwindow.ID_HIDE_BOM: {"R1", "R3"},
        mainwindow.ID_HIDE_POS: {"R1", "R2"},
    }
    for tool, expected in (
        (first, single_filter_rows[first]),
        (second, {"R1"}),
        (first, single_filter_rows[second]),
        (second, {"R1", "R2", "R3", "R4"}),
    ):
        ui.click(tool)
        assert set(ui.rows) == expected
        assert ui.parts == before
        assert ui.window.footprint_list.GetSelectedItemsCount() == 0
        for tool_id, suffix in zip(FILTERS, ("BOM", "POS")):
            button = ui.window.right_toolbar.tools[tool_id]
            hidden = getattr(ui.window, f"hide_{suffix.lower()}_parts")
            assert button.checked == hidden
            assert button.label == f"{'Show' if hidden else 'Hide'} excluded {suffix}"


@pytest.mark.parametrize("tool", FILTERS)
def test_last_visible_selected_row_can_be_hidden_and_restored(
    open_window: Callable[..., Any],
    tool: int,
) -> None:
    """An empty filtered table keeps its Show control reachable after deselection."""
    ui = open_window(["R4"])
    before = deepcopy(ui.parts)
    ui.select("R4")
    ui.click(tool)
    assert not ui.rows
    assert ui.window.footprint_list.GetSelectedItemsCount() == 0
    assert not any(ui.window.right_toolbar.tools[tool].enabled for tool in PART_ACTIONS)

    ui.click(tool)

    assert set(ui.rows) == {"R4"}
    assert ui.parts == before


def test_storage_failure_still_disables_toolbar_until_recovery(
    open_window: Callable[..., Any],
) -> None:
    """Selection-independent filters respect the separate whole-toolbar failure state."""
    ui = open_window()
    ui.window._set_project_storage_error(sqlite3.OperationalError("database is locked"))
    assert not ui.window.right_toolbar.available
    assert not ui.rows
    ui.select()
    assert not ui.window.right_toolbar.available

    ui.window._set_project_storage_error(None)

    assert ui.window.right_toolbar.available
    assert all(ui.window.right_toolbar.tools[tool].enabled for tool in FILTERS)
    assert not any(ui.window.right_toolbar.tools[tool].enabled for tool in PART_ACTIONS)
