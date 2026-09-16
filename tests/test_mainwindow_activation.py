"""Refresh board-derived rows and estimates when a modeless window regains focus."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import test_mainwindow_enrichment as enrichment
from .wx_harness import load, track_selection

workflow = enrichment.workflow
database_mainwindow = enrichment.database_mainwindow


@pytest.fixture
def activation(
    workflow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Use the real window, model, Store and estimator with explicit event queues."""
    window, main = workflow.window, workflow.main
    model = window.partlist_data_model
    selected = [model.ObjectToItem(row) for row in model.data]
    for footprint in workflow.board.GetFootprints():
        footprint.SetSelected()
    track_selection(window, model, selected, notify=True)
    events: list[Any] = []
    callbacks: list[Callable[[], None]] = []
    monkeypatch.setattr(
        main.wx, "PostEvent", lambda _target, event: events.append(event)
    )
    monkeypatch.setattr(main.wx, "CallAfter", callbacks.append, raising=False)
    widget = load(
        workflow.db.__package__, "bom_widget", {"pcbnew": MagicMock(), "wx": main.wx}
    )
    summaries: list[str] = []
    window.bom_estimator_controller = widget.BomEstimatorController(
        read_parts=window.store.read_all,
        get_part_details=window._bom_get_part_details,
        get_board=window._get_current_board,
        is_force_standard_enabled=lambda: False,
        set_price_label=model.set_bom_price,
        set_standard_only_refs=model.set_standard_only_refs,
        set_summary_text=summaries.append,
        set_details_button_label=lambda _label: None,
    )
    window._part_preferences_applied_on_open = True
    window._fill_empty_lcsc_assignments_from_part_preferences = MagicMock()
    window.init_store = MagicMock()
    window.library.get_part_details.return_value = {"type": "Basic", "stock": 0}
    window._invalidate_catalog_details()
    window.recompute_stock_concerns()
    window.recompute_bom_estimate()

    def focus(active: bool = True) -> Any:
        """Deliver only the activation handler actually bound by the constructor."""
        event = SimpleNamespace(GetActive=lambda: active, Skip=MagicMock())
        handler = window._bindings.get(main.wx.EVT_ACTIVATE)
        if handler is not None:
            handler(event)
        return event

    def drain() -> None:
        """Deliver posted BOM events and then drain native-style deferred callbacks."""
        while events or callbacks:
            while events:
                window.on_bom_data_changed(events.pop(0))
            if callbacks:
                callbacks.pop(0)()

    return SimpleNamespace(
        window=window,
        main=main,
        board=workflow.board,
        selected=selected,
        summaries=summaries,
        focus=focus,
        drain=drain,
    )


def row_for(activation: SimpleNamespace, reference: str) -> list[Any]:
    """Read visible cells from the current real model row."""
    model = activation.window.partlist_data_model
    return model.data[model.find_index(reference)]


def test_activation_refreshes_undo_redo_assignments_exclusions_and_estimate(
    activation: SimpleNamespace,
) -> None:
    """Returning from the editor must display its current fields and excluded BOM."""
    window, board = activation.window, activation.board
    model = window.partlist_data_model
    original_rows = list(activation.selected)
    before_summary = activation.summaries[-1]
    assert model.stock_concern_refs == {"R1", "R2"}
    board.footprints["R1"].SetField("LCSC", "")
    board.footprints["R2"].SetAttributes((1 << 3) | (1 << 2))

    event = activation.focus()
    activation.drain()

    assert row_for(activation, "R1")[model.columns["LCSC_COL"]] == ""
    assert row_for(activation, "R2")[model.columns["BOM_COL"]] == model.bom_pos_icons[1]
    assert row_for(activation, "R2")[model.columns["POS_COL"]] == model.bom_pos_icons[1]
    assert "no assigned BOM parts" in activation.summaries[-1]
    assert activation.summaries[-1] != before_summary
    assert model.stock_concern_refs == set()
    assert [model.get_reference(item) for item in activation.selected] == ["R1", "R2"]
    assert all(footprint.selected for footprint in board.GetFootprints())
    assert all(new is not old for new, old in zip(activation.selected, original_rows))
    event.Skip.assert_called_once()
    window._fill_empty_lcsc_assignments_from_part_preferences.assert_not_called()
    window.init_store.assert_not_called()

    board.footprints["R1"].SetField("LCSC", "C100")
    board.footprints["R2"].SetAttributes(0)
    activation.focus()
    activation.drain()
    assert row_for(activation, "R1")[model.columns["LCSC_COL"]] == "C100"
    assert activation.summaries[-1] == before_summary
    assert model.stock_concern_refs == {"R1", "R2"}
    assert {"C100", "C200"} <= window.pending_assembly_enrichment


def test_activation_drops_deleted_or_filtered_selections(
    activation: SimpleNamespace,
) -> None:
    """A refresh cannot restore removed rows or rows hidden by active exclusions."""
    window = activation.window
    window.hide_bom_parts = True
    del activation.board.footprints["R1"]
    activation.board.footprints["R2"].SetAttributes(1 << 3)
    activation.focus()
    activation.drain()
    assert window.partlist_data_model.data == []
    assert activation.selected == []
    assert not any(footprint.selected for footprint in activation.board.GetFootprints())


@pytest.mark.parametrize(
    "fields", [{}, {"LCSC": ""}, {"LCSC": "C300"}], ids=["removed", "blank", "replaced"]
)
def test_activation_observes_external_field_update_without_restoring_assignment(
    activation: SimpleNamespace, fields: dict[str, str]
) -> None:
    """Model post-update fields, not F8 itself: only the display waits for activation."""
    window, board = activation.window, activation.board
    model = window.partlist_data_model
    window.library.merge_lcsc_metadata(
        "C100", {"assembly_process": "SMT", "component_product_type": 2}
    )
    window.recompute_bom_estimate()
    previous_summary = activation.summaries[-1]
    assert model.standard_only_refs == {"R1"}
    window.library.get_part_details.side_effect = lambda code: {
        "type": "Basic",
        "stock": 100 if code == "C300" else 0,
    }
    footprint = board.footprints["R1"]
    footprint.fields.clear()
    for name, text in fields.items():
        footprint.SetField(name, text)
    expected = fields.get("LCSC", "")
    part = window.store.read_all()[0]
    assert part["lcsc"] == expected
    assert part["component_product_type"] is None
    assert row_for(activation, "R1")[model.columns["LCSC_COL"]] == "C100"

    activation.focus()
    activation.drain()

    assert row_for(activation, "R1")[model.columns["LCSC_COL"]] == expected
    assert row_for(activation, "R2")[model.columns["LCSC_COL"]] == "C200"
    assert {
        part["lcsc"] for part in window.store.read_bom_parts(include_unassigned=False)
    } == ({"C200", expected} if expected else {"C200"})
    assert model.stock_concern_refs == {"R2"}
    assert model.standard_only_refs == set()
    assert activation.summaries[-1] != previous_summary
    assert {
        field.GetName(): field.GetText() for field in footprint.GetFields()
    } == fields
    assert (
        window.library.get_lcsc_metadata(["C100"])["C100"]["component_product_type"]
        == 2
    )
    window._fill_empty_lcsc_assignments_from_part_preferences.assert_not_called()
    window.init_store.assert_not_called()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_activation_restores_native_selections_without_auto_expansion(
    activation: SimpleNamespace, count: int
) -> None:
    """Empty, single and multiple selections restore through the native array API."""
    window = activation.window
    activation.selected[:] = activation.selected[:count]
    references = {
        window.partlist_data_model.get_reference(item) for item in activation.selected
    }
    window.auto_select_alike = True
    window.select_alike_parts = MagicMock()

    activation.focus()
    activation.drain()

    assert {
        window.partlist_data_model.get_reference(item) for item in activation.selected
    } == references
    assert {
        fp.GetReference() for fp in activation.board.GetFootprints() if fp.selected
    } == references
    window.select_alike_parts.assert_not_called()
    assert not window.select_alike_in_progress


@pytest.mark.parametrize("state", ["inactive", "closing", "unavailable", "no_store"])
def test_irrelevant_activation_does_not_refresh_or_reinitialize(
    activation: SimpleNamespace, state: str
) -> None:
    """Deactivation and unavailable windows cannot trigger reads or preference autofill."""
    window = activation.window
    if state == "closing":
        window._closing = True
    elif state == "unavailable":
        window._board_context_unavailable = True
    elif state == "no_store":
        window.store = None
    window.populate_footprint_list = MagicMock()
    window.start_assembly_enrichment = MagicMock()
    event = activation.focus(active=state != "inactive")
    activation.drain()
    window.populate_footprint_list.assert_not_called()
    window.start_assembly_enrichment.assert_not_called()
    window.init_store.assert_not_called()
    window._fill_empty_lcsc_assignments_from_part_preferences.assert_not_called()
    event.Skip.assert_called_once()


def test_activation_after_board_close_disables_old_window(
    activation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A focus event must reject a closed PCB before any stale native enumeration."""
    window = activation.window
    monkeypatch.setattr(
        activation.board,
        "GetFootprints",
        MagicMock(side_effect=AssertionError("destroyed native board accessed")),
    )
    window.pcbnew.GetBoard = lambda: None
    activation.focus()
    activation.drain()
    assert window._board_context_unavailable
    assert window.store is None
    assert window.partlist_data_model.data == []
    window.init_store.assert_not_called()
