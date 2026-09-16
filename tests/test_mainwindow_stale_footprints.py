"""Real action handlers skip stale selections and mutate only live board items."""

from collections.abc import Callable
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Footprint
from .stock_test_support import stock_modules
from .wx_harness import track_selection

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.fixture(autouse=True)
def immediate_callbacks(mainwindow: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Most cases finish deferred work immediately; ordering cases supply a queue."""
    monkeypatch.setattr(
        mainwindow.wx, "CallAfter", lambda callback: callback(), raising=False
    )


def test_populate_footprint_list_skips_stale_row_and_retains_live_row(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion after reading a parts snapshot cannot hide the remaining live row."""
    window = make_window(footprints=[Footprint("R_REMOVED"), Footprint("R2")])
    snapshot = window.store.read_all()
    del window.pcbnew.GetBoard().footprints["R_REMOVED"]
    monkeypatch.setattr(window.store, "read_all", lambda: snapshot)

    mainwindow.JLCPCBTools.populate_footprint_list(window)

    added_references = [
        invocation.args[0][0]
        for invocation in window.partlist_data_model.AddEntry.call_args_list
    ]
    assert added_references == ["R2"]


def test_assign_parts_skips_stale_refs_and_continues_live_refs(
    make_window: Callable[..., Any],
) -> None:
    """Assignment must mutate and enrich only references still on the board."""
    live_footprint = Footprint("R2")
    window = make_window(footprints=[live_footprint])

    window.assign_parts(
        SimpleNamespace(
            lcsc="C12345", stock="27", type="Basic", references=["R_REMOVED", "R2"]
        )
    )

    assert live_footprint.field.text == "C12345"
    assert all(part["reference"] != "R_REMOVED" for part in window.store.read_all())
    assert window.store.read_all()[0]["lcsc"] == "C12345"
    assert window.store.read_all()[0]["stock"] is None
    assert window.test_rows["R2"]["stock"] == "27"
    assert window.test_rows["R2"]["lcsc"] == "C12345"
    window.start_assembly_enrichment.assert_called_once_with(["R2"])
    window.library.save_part_preferences.assert_called_once_with(
        [("R_0603", "10k", "C12345")]
    )


def test_assign_parts_with_only_stale_refs_does_not_start_enrichment(
    make_window: Callable[..., Any],
) -> None:
    """An all-stale selector result must leave board, assignment rows and preferences alone."""
    window = make_window(footprints=[])

    window.assign_parts(
        SimpleNamespace(
            lcsc="C12345", stock="27", type="Basic", references=["R_REMOVED"]
        )
    )

    assert window.pcbnew.GetBoard().GetFootprints() == []
    assert window.store.read_all() == []
    window.start_assembly_enrichment.assert_not_called()
    window.library.save_part_preferences.assert_not_called()


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos", "toggle_bom_pos"])
def test_toggle_handlers_skip_stale_refs_and_continue_live_refs(
    make_window: Callable[..., Any], handler_name: str
) -> None:
    """BOM/POS actions mutate native flags and displayed cells only for live selections."""
    stale_item, live_item = object(), object()
    live_footprint = Footprint("R2")
    window = make_window(footprints=[live_footprint])
    window.footprint_list.GetSelections.return_value = [stale_item, live_item]
    references = {stale_item: "R_REMOVED", live_item: "R2"}
    window.partlist_data_model.get_reference.side_effect = references.__getitem__

    getattr(window, handler_name)()

    window.populate_footprint_list.assert_called_once_with()
    part = window.store.read_all()[0]
    assert part["exclude_from_bom"] is (handler_name != "toggle_pos")
    assert part["exclude_from_pos"] is (handler_name != "toggle_bom")
    assert window.test_rows["R2"]["exclude_from_bom"] is (handler_name != "toggle_pos")
    assert window.test_rows["R2"]["exclude_from_pos"] is (handler_name != "toggle_bom")
    assert all(part["reference"] != "R_REMOVED" for part in window.store.read_all())


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos", "toggle_bom_pos"])
def test_toggle_with_only_stale_selection_has_no_native_or_model_changes(
    make_window: Callable[..., Any], handler_name: str
) -> None:
    """A deleted selection cannot toggle an unrelated footprint still on the board."""
    live_footprint = Footprint("R2")
    window = make_window(footprints=[live_footprint])
    window.footprint_list.GetSelections.return_value = ["R_REMOVED"]

    getattr(window, handler_name)()

    assert live_footprint.GetAttributes() == 0
    window.populate_footprint_list.assert_not_called()


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos", "toggle_bom_pos"])
def test_toggle_refreshes_both_exclusion_cells_from_current_board_flags(
    make_window: Callable[..., Any], mainwindow: Any, handler_name: str
) -> None:
    """External flag edits cannot make subsequent native and displayed toggles diverge."""
    footprint = Footprint("R1")
    window = make_window(footprints=[footprint])
    with stock_modules() as modules:
        model = window.partlist_data_model = modules.datamodel.PartListDataModel(1)
        window.populate_footprint_list = MethodType(
            mainwindow.JLCPCBTools.populate_footprint_list, window
        )
        window.populate_footprint_list()
        selected = [model.ObjectToItem(model.data[0])]
        track_selection(window, model, selected)
        footprint.SetAttributes((1 << 3) | (1 << 2))

        getattr(window, handler_name)()

        expected_bom = handler_name == "toggle_pos"
        expected_pos = handler_name == "toggle_bom"
        part = window.store.read_all()[0]
        assert part["exclude_from_bom"] is expected_bom
        assert part["exclude_from_pos"] is expected_pos
        assert (
            model.data[0][model.columns["BOM_COL"]]
            == model.bom_pos_icons[int(expected_bom)]
        )
        assert (
            model.data[0][model.columns["POS_COL"]]
            == model.bom_pos_icons[int(expected_pos)]
        )
        assert len(selected) == 1
        assert model.ItemToObject(selected[0]) is model.data[0]


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos"])
def test_toggle_reapplies_exclusion_filter_and_drops_only_hidden_selection(
    make_window: Callable[..., Any], mainwindow: Any, handler_name: str
) -> None:
    """Newly excluded rows leave a filtered view without selecting an unrelated row."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.hide_bom_parts = handler_name == "toggle_bom"
    window.hide_pos_parts = handler_name == "toggle_pos"
    with stock_modules() as modules:
        model = window.partlist_data_model = modules.datamodel.PartListDataModel(1)
        window.populate_footprint_list = MethodType(
            mainwindow.JLCPCBTools.populate_footprint_list, window
        )
        window.populate_footprint_list()
        selected = [model.ObjectToItem(model.data[0])]
        track_selection(window, model, selected)

        getattr(window, handler_name)()

        assert [row[model.columns["REF_COL"]] for row in model.data] == ["R2"]
        assert selected == []
        assert len(window.store.read_all()) == 2


@pytest.mark.parametrize(
    "state",
    ["normal", "new_selection", "closing", "unavailable", "no_store", "refresh_again"],
)
def test_deferred_selection_restore_uses_fresh_rows_and_respects_window_changes(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Queued selection restoration cannot retain stale items or override a new user choice."""
    callbacks = []
    monkeypatch.setattr(mainwindow.wx, "CallAfter", callbacks.append)
    first, second = Footprint("R1"), Footprint("R2")
    window = make_window(footprints=[first, second])
    window.auto_select_alike = True
    window.select_alike_in_progress = False
    window.select_alike_parts = MagicMock()
    with stock_modules() as modules:
        model = window.partlist_data_model = modules.datamodel.PartListDataModel(1)
        window.populate_footprint_list = MethodType(
            mainwindow.JLCPCBTools.populate_footprint_list, window
        )
        window.populate_footprint_list()
        old_row = model.data[0]
        selected = [model.ObjectToItem(old_row)]
        track_selection(window, model, selected, notify=True)
        first.SetSelected()

        window.toggle_bom()
        assert selected == []
        assert not first.selected
        if state == "new_selection":
            selected[:] = [model.ObjectToItem(model.data[1])]
        elif state == "closing":
            window._closing = True
        elif state == "unavailable":
            window._board_context_unavailable = True
        elif state == "no_store":
            window.store = None
        elif state == "refresh_again":
            window._refresh_footprints_preserving_selection()
        while callbacks:
            callbacks.pop(0)()

        if state in {"closing", "unavailable", "no_store"}:
            assert selected == []
            window.footprint_list.SetSelections.assert_not_called()
        else:
            assert len(selected) == 1
            expected = 1 if state == "new_selection" else 0
            assert model.ItemToObject(selected[0]) is model.data[expected]
            assert model.ItemToObject(selected[0]) is not old_row
            if state == "new_selection":
                window.footprint_list.SetSelections.assert_not_called()
            else:
                window.footprint_list.SetSelections.assert_called_once()
            assert [fp.selected for fp in (first, second)] == [
                expected == 0,
                expected == 1,
            ]
            window.pcbnew.Refresh.assert_called()
            window.select_alike_parts.assert_not_called()
