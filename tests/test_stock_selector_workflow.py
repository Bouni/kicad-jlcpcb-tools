"""Stock presentation survives complete selector and assignment workflows."""

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import (
    part_preferences_test_support as storage,
    test_settings_dialog as settings_ui,
    test_window_layout as layout_ui,
)
from .stock_test_support import board_row, stock_modules

mainwindow = storage.mainwindow
make_window = storage.make_window


def _populate_selector(selector: Any) -> Any:
    """Pass a raw catalog result through the actual selector population handler."""
    catalog_row = {
        "LCSC Part": "C200",
        "MFR.Part": "Example part",
        "Library Type": "Basic",
        "Stock": 22095,
        "Price": "1-:0.5",
    }
    selector.populate_part_list(
        [
            tuple(
                catalog_row.get(field, "") for field in layout_ui.partselector.DB_FIELDS
            )
        ],
        search_duration=0,
    )
    return selector.part_list_model.ObjectToItem(selector.part_list_model.data[0])


def _stock_label(model: Any, item: Any, key: str) -> str:
    """Read rendered stock through the real model's public view interface."""
    return model.GetValue(item, model.columns[key])


def test_open_selector_updates_with_main_setting_and_reopens_exact_stock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real constructors, setting handlers and close events retain the saved choice."""
    with stock_modules() as modules:
        monkeypatch.setattr(
            layout_ui.mainwindow,
            "PartListDataModel",
            modules.datamodel.PartListDataModel,
        )
        monkeypatch.setattr(
            layout_ui.partselector,
            "PartSelectorDataModel",
            modules.datamodel.PartSelectorDataModel,
        )
        monkeypatch.setattr(layout_ui.mainwindow, "PLUGIN_PATH", str(tmp_path))
        settings = settings_ui._settings(True)
        settings["partselector"] = {}
        window = layout_ui._open_main(monkeypatch, settings)
        window.save_settings = layout_ui.mainwindow.JLCPCBTools.save_settings.__get__(
            window
        )
        window.partlist_data_model.AddEntry(board_row("R1", "22095"))
        selector = layout_ui._open_selector(monkeypatch, {}, parent=window)
        selected = _populate_selector(selector)
        main_item = window.partlist_data_model.data[0]
        assert _stock_label(window.partlist_data_model, main_item, "STOCK_COL") == "22k"
        assert _stock_label(selector.part_list_model, selected, "stock") == "22k"

        dialog = settings_ui.SettingsDialog(window)
        for enabled in (False, True, False):
            dialog.simplify_stock_setting.SetValue(enabled)
            events = settings_ui._fire(dialog.simplify_stock_setting)
            assert len(events) == 1
            window.update_settings(events[0])
            expected = "22k" if enabled else "22095"
            assert (
                _stock_label(window.partlist_data_model, main_item, "STOCK_COL")
                == expected
            )
            assert _stock_label(selector.part_list_model, selected, "stock") == expected
            assert selector.part_list_model.get_stock(selected) == "22095"

        window.Close()
        assert window._part_selector is None
        assert not selector
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["general"]["simplify_stock"] is False
        reopened = layout_ui._open_main(monkeypatch, saved)
        reopened.partlist_data_model.AddEntry(board_row("R1", "22095"))
        reopened_selector = layout_ui._open_selector(
            monkeypatch, saved["partselector"], parent=reopened
        )
        reopened_selected = _populate_selector(reopened_selector)
        assert (
            _stock_label(
                reopened.partlist_data_model,
                reopened.partlist_data_model.data[0],
                "STOCK_COL",
            )
            == "22095"
        )
        assert (
            _stock_label(reopened_selector.part_list_model, reopened_selected, "stock")
            == "22095"
        )
        reopened.Close()
        layout_ui._drain_callbacks()


@pytest.mark.parametrize("simplified", [False, True])
def test_selector_assignment_keeps_exact_stock_in_board_model_and_database(
    monkeypatch: pytest.MonkeyPatch,
    make_window: Callable[..., Any],
    mainwindow: Any,
    simplified: bool,
) -> None:
    """The real selection event carries raw stock through durable assignment."""
    with stock_modules() as modules:
        monkeypatch.setattr(
            layout_ui.partselector,
            "PartSelectorDataModel",
            modules.datamodel.PartSelectorDataModel,
        )
        window = make_window(settings={"general": {"simplify_stock": simplified}})
        window.window = layout_ui._Dialog()
        window.scale_factor = 2
        window.display_index = 0
        window.library.category_map = {"": []}
        window.save_settings = MagicMock()
        window.partlist_data_model = modules.datamodel.PartListDataModel(
            1.0, simplify_stock=simplified
        )
        window.partlist_data_model.AddEntry(board_row("R1", "27", "C100"))
        selector = layout_ui._open_selector(monkeypatch, {}, parent=window)
        selector.update_for({"R1": "C100"})
        item = _populate_selector(selector)
        selector.part_list.GetSelectedItemsCount.return_value = 1
        selector.part_list.GetSelection.return_value = item
        queued: list[tuple[Any, Any]] = []
        monkeypatch.setattr(
            layout_ui.partselector.wx,
            "PostEvent",
            lambda target, event: queued.append((target, event)),
            raising=False,
        )

        assert _stock_label(selector.part_list_model, item, "stock") == (
            "22k" if simplified else "22095"
        )
        selector.select_part()
        assert len(queued) == 1
        target, event = queued[0]
        assert target is window
        assert event.stock == "22095"
        assert not selector
        assert window._part_selector is None
        target.assign_parts(event)

        assert storage.project_rows(window)[0]["stock"] == 22095
        assert (
            window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C200"
        )
        model = window.partlist_data_model
        assert model.get_all()[0][model.columns["STOCK_COL"]] == "22095"
        assert _stock_label(model, model.data[0], "STOCK_COL") == (
            "22k" if simplified else "22095"
        )
        reopened = mainwindow.Store(
            window, window.project_path, window.pcbnew.GetBoard()
        )
        assert reopened.get_part("R1")["stock"] == 22095
        assert reopened.get_part("R1")["lcsc"] == "C200"
        layout_ui._drain_callbacks()
