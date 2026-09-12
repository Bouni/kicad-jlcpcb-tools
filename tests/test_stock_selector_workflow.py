"""Stock presentation survives complete selector and assignment workflows."""

from collections.abc import Callable, Iterator, Sequence
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
        assert (
            _stock_label(window.partlist_data_model, main_item, "STOCK_COL") == "22 k"
        )
        assert _stock_label(selector.part_list_model, selected, "stock") == "22 k"

        dialog = settings_ui.SettingsDialog(window)
        for enabled in (False, True, False):
            dialog.simplify_stock_setting.SetValue(enabled)
            events = settings_ui._fire(dialog.simplify_stock_setting)
            assert len(events) == 1
            window.update_settings(events[0])
            expected = "22 k" if enabled else "22095"
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
            "22 k" if simplified else "22095"
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
            "22 k" if simplified else "22095"
        )
        reopened = mainwindow.Store(
            window, window.project_path, window.pcbnew.GetBoard()
        )
        assert reopened.get_part("R1")["stock"] == 22095
        assert reopened.get_part("R1")["lcsc"] == "C200"
        layout_ui._drain_callbacks()


class _CatalogComboBox:
    """Retain read-only choices and dispatch Cocoa's synchronous Clear text event."""

    def __init__(
        self, *_args: Any, choices: Sequence[str] = (), **_kwargs: Any
    ) -> None:
        self.items = list(choices)
        self.value = ""
        self.selection = -1
        self.handlers: dict[Any, Callable[..., None]] = {}
        self.blocked = 0

    def Bind(self, event: Any, handler: Callable[..., None]) -> None:
        self.handlers[event] = handler

    def SetHint(self, _hint: str) -> None:
        pass

    def GetValue(self) -> str:
        return self.value

    def SetValue(self, value: str) -> None:
        # A read-only native combo only accepts an existing choice and is silent.
        if value in self.items:
            self.value = value
            self.selection = self.items.index(value)

    def GetSelection(self) -> int:
        return self.selection

    def Clear(self) -> None:
        self.items.clear()
        self.value = ""
        self.selection = -1
        self.emit(layout_ui.partselector.wx.EVT_TEXT)

    def AppendItems(self, items: list[str]) -> None:
        self.items.extend(items)

    def emit(self, event: Any) -> None:
        if not self.blocked and event in self.handlers:
            self.handlers[event](MagicMock())

    def select(self, value: str) -> None:
        assert value in self.items
        self.value = value
        self.selection = self.items.index(value)
        self.emit(layout_ui.partselector.wx.EVT_COMBOBOX)


class _CatalogEventBlocker:
    """Suppress dispatch in scope and restore it even when a refresh raises."""

    def __init__(self, control: _CatalogComboBox) -> None:
        self.control = control
        self.control.blocked += 1

    def __enter__(self) -> "_CatalogEventBlocker":
        return self

    def __exit__(self, *_exception: Any) -> None:
        self.control.blocked -= 1


class _SelectorCatalog:
    """Record real search parameters and inject metadata failures on demand."""

    def __init__(self) -> None:
        self.category_names = ["All", "", "Resistors", "Capacitors", "Old category"]
        self.subcategories = {
            "": [],
            "Resistors": ["Chip resistors"],
            "Capacitors": ["Ceramic capacitors"],
            "Old category": ["Old subcategory"],
        }
        self.fail_at = ""
        self.queries: list[dict[str, Any]] = []
        self.subcategory_queries: list[str] = []

    @property
    def categories(self) -> list[str]:
        if self.fail_at == "categories":
            raise RuntimeError("catalog categories failed")
        return self.category_names

    def get_subcategories(self, category: str) -> list[str]:
        if self.fail_at == "subcategories":
            raise RuntimeError("catalog subcategories failed")
        self.subcategory_queries.append(category)
        return self.subcategories[category]

    def search(self, parameters: dict[str, Any]) -> list[tuple[Any, ...]]:
        self.queries.append(parameters)
        return []


@pytest.fixture
def catalog_selector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Open the production constructor with controls that retain its bindings."""
    with stock_modules() as modules:
        monkeypatch.setattr(
            layout_ui.partselector,
            "PartSelectorDataModel",
            modules.datamodel.PartSelectorDataModel,
        )
        monkeypatch.setattr(layout_ui.partselector.wx, "ComboBox", _CatalogComboBox)
        monkeypatch.setattr(
            layout_ui.partselector.wx,
            "EventBlocker",
            _CatalogEventBlocker,
            raising=False,
        )
        window = layout_ui._open_main(monkeypatch, settings_ui._settings(True))
        window.library = _SelectorCatalog()
        selector = layout_ui._open_selector(
            monkeypatch, {}, parent=window, real_search=True
        )
        window.library.queries.clear()
        try:
            yield selector
        finally:
            selector.Close()
            window.Close()
            layout_ui._drain_callbacks()


def test_catalog_reset_repeatedly_unavailable_and_category_event_are_bounded(
    catalog_selector: Any,
) -> None:
    """Clearing populated or empty native-style controls cannot reenter refresh."""
    selector = catalog_selector
    selector.category.SetValue("Old category")
    selector.subcategory.AppendItems(["Old subcategory"])
    selector.subcategory.SetValue("Old subcategory")
    _populate_selector(selector)
    selector.parent._catalog_ready = False
    selector.parent.library.fail_at = "categories"

    for _attempt in range(2):
        selector.search_timer.StartOnce(750)
        selector.search_timer.Stop.reset_mock()
        selector.refresh_catalog()
        selector.search_timer.Stop.assert_called_once_with()
        assert selector.category.items == selector.subcategory.items == []
        assert selector.category.GetValue() == selector.subcategory.GetValue() == ""
        assert selector.part_list_model.data == []
        assert selector.category.blocked == selector.subcategory.blocked == 0
        selector.result_count.SetLabel.assert_called_with(
            "Parts catalog unavailable; download it to search."
        )

    selector.category.emit(layout_ui.partselector.wx.EVT_TEXT)
    selector.category.emit(layout_ui.partselector.wx.EVT_COMBOBOX)
    selector.search()
    assert selector.parent.library.queries == []
    assert selector.parent.library.subcategory_queries == []


@pytest.mark.parametrize(
    ("saved_category", "expected_category", "expected_subcategories"),
    [
        ("Resistors", "Resistors", ["Chip resistors"]),
        ("Old category", "All", []),
        ("All", "All", []),
    ],
)
def test_ready_catalog_reset_searches_only_after_filters_are_complete(
    catalog_selector: Any,
    saved_category: str,
    expected_category: str,
    expected_subcategories: list[str],
) -> None:
    """Preserved and removed categories each lead to one final filtered search."""
    selector = catalog_selector
    selector.category.SetValue(saved_category)
    selector.subcategory.AppendItems(["Old subcategory"])
    selector.subcategory.SetValue("Old subcategory")
    selector.parent.library.category_names.remove("Old category")

    for _attempt in range(2):
        selector.parent.library.queries.clear()
        selector.refresh_catalog()
        assert selector.category.GetValue() == expected_category
        assert selector.subcategory.GetValue() == ""
        assert selector.subcategory.items == expected_subcategories
        assert len(selector.parent.library.queries) == 1
        parameters = selector.parent.library.queries[0]
        assert parameters["category"] == expected_category
        assert parameters["subcategory"] == ""


def test_catalog_recovers_and_retains_real_category_selection_handler(
    catalog_selector: Any,
) -> None:
    """A pending download can recover without leaving category events blocked."""
    selector = catalog_selector
    selector.parent._catalog_ready = False
    selector.refresh_catalog()
    selector.parent._catalog_ready = True
    selector.refresh_catalog()
    assert len(selector.parent.library.queries) == 1
    assert selector.parent.library.queries[0]["category"] == ""
    assert selector.parent.library.queries[0]["subcategory"] == ""
    selector.parent.library.queries.clear()

    selector.category.select("Resistors")
    assert selector.subcategory.items == ["Chip resistors"]
    assert len(selector.parent.library.queries) == 1
    assert selector.parent.library.queries[0]["category"] == "Resistors"
    assert selector.parent.library.queries[0]["subcategory"] == ""


def test_selecting_all_after_a_category_uses_no_display_only_subcategory_key(
    catalog_selector: Any,
) -> None:
    """The All label is absent from Library.category_map and must not be queried."""
    selector = catalog_selector
    selector.category.select("Resistors")
    assert selector.subcategory.items == ["Chip resistors"]
    selector.parent.library.queries.clear()
    selector.parent.library.subcategory_queries.clear()

    selector.category.select("All")
    assert selector.subcategory.items == []
    assert selector.subcategory.GetValue() == ""
    assert selector.parent.library.subcategory_queries == []
    assert len(selector.parent.library.queries) == 1
    assert selector.parent.library.queries[0]["category"] == "All"
    assert selector.parent.library.queries[0]["subcategory"] == ""


@pytest.mark.parametrize("failure", ["categories", "subcategories"])
def test_catalog_reset_exception_restores_handlers_for_retry_and_selection(
    catalog_selector: Any, failure: str
) -> None:
    """Metadata errors cannot query half-reset filters or leave handlers blocked."""
    selector = catalog_selector
    selector.category.SetValue("Resistors")
    selector.parent.library.fail_at = failure
    with pytest.raises(RuntimeError, match=f"catalog {failure} failed"):
        selector.refresh_catalog()
    assert selector.parent.library.queries == []
    assert selector.category.blocked == selector.subcategory.blocked == 0
    selector.parent.library.fail_at = ""

    selector.refresh_catalog()
    assert len(selector.parent.library.queries) == 1
    selector.parent.library.queries.clear()
    selector.category.select("Capacitors")
    assert selector.subcategory.items == ["Ceramic capacitors"]
    assert len(selector.parent.library.queries) == 1
    assert selector.parent.library.queries[0]["category"] == "Capacitors"


def test_selector_constructor_never_queries_an_unavailable_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening a selector while a catalog download is pending must remain usable."""

    class PendingCatalog:
        """Fail immediately if constructor accidentally loads absent category tables."""

        @property
        def categories(self) -> list[str]:
            raise AssertionError("unavailable category table queried")

    window = layout_ui._open_main(monkeypatch, settings_ui._settings(True))
    window._catalog_ready = False
    window.library = PendingCatalog()
    selector = layout_ui._open_selector(
        monkeypatch, {}, parent=window, real_search=True
    )
    selector.result_count.SetLabel.assert_called_with(
        "Parts catalog unavailable; download it to search."
    )
    selector.Close()
    window.Close()
    layout_ui._drain_callbacks()
