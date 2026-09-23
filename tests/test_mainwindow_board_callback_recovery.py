"""Queued ordinary consumers must retire a stale native board view safely."""

from collections.abc import Callable
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Board, Footprint
from .test_bom_estimator_controller import BomEstimatorController

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.mark.parametrize("change", ["replaced", "renamed", "variants"])
@pytest.mark.parametrize(
    "callback", ["stock", "enrichment", "queued", "estimate", "details"]
)
def test_ordinary_callbacks_disable_stale_board_view(
    make_window: Callable[..., Any],
    mainwindow: ModuleType,
    change: str,
    callback: str,
) -> None:
    """Pending consumers cannot retain enabled rows after their native source changes."""
    window = make_window()
    window.start_assembly_enrichment = MethodType(
        mainwindow.JLCPCBTools.start_assembly_enrichment, window
    )
    window.recompute_bom_estimate = MethodType(
        mainwindow.JLCPCBTools.recompute_bom_estimate, window
    )
    window.bom_estimator_board_count = 5
    window.bom_estimator_controller = BomEstimatorController(
        read_parts=window.read_assembly_parts,
        get_part_details=window.library.get_part_details,
        get_board=window._get_current_board,
        is_force_standard_enabled=lambda: False,
        set_price_label=MagicMock(),
        set_standard_only_refs=MagicMock(),
        set_summary_text=MagicMock(),
        set_details_button_label=MagicMock(),
    )
    window.bom_estimator_decision = None
    window._why_standard_dialog = MagicMock()
    window._bom_recompute_scheduled = True
    window.assembly_lookup.request = MagicMock()
    board = window.pcbnew.GetBoard()
    if change == "replaced":
        replacement = Board([Footprint("R1", lcsc="C200")])
        replacement.filename = board.filename
        window.pcbnew.GetBoard = lambda: replacement
    elif change == "renamed":
        board.filename = board.filename.replace("board.kicad_pcb", "renamed.kicad_pcb")
    else:
        board.GetVariantNamesForUI = lambda: ["Default", "A"]

    {
        "stock": window.recompute_stock_concerns,
        "enrichment": window.start_assembly_enrichment,
        "queued": window._run_coalesced_bom_recompute,
        "estimate": window.recompute_bom_estimate,
        "details": window.show_assembly_mode_details,
    }[callback]()

    assert window._project_storage_unavailable
    assert window.store is None
    assert not window.test_rows
    assert not window.upper_toolbar.enabled[mainwindow.ID_GENERATE]
    window.footprint_list.Enable.assert_called_with(False)
    window.assembly_lookup.request.assert_not_called()
    window._why_standard_dialog.Raise.assert_not_called()
    if callback == "details":
        window._why_standard_dialog.Close.assert_called_once_with()
    if callback == "queued":
        assert not window._bom_recompute_scheduled


def test_queued_recompute_after_close_does_not_touch_controls(
    make_window: Callable[..., Any],
) -> None:
    """A callback queued before Destroy cannot access the window's native widgets."""
    window = make_window()
    window._closing = True
    window._bom_recompute_scheduled = True
    window.recompute_stock_concerns = MagicMock()

    window._run_coalesced_bom_recompute()

    assert not window._bom_recompute_scheduled
    window.recompute_stock_concerns.assert_not_called()
    window.recompute_bom_estimate.assert_not_called()


def test_enrichment_after_close_does_not_start_new_work(
    make_window: Callable[..., Any], mainwindow: ModuleType
) -> None:
    """A late catalog completion cannot launch a new lookup after window teardown."""
    window = make_window()
    window._closing = True
    window.assembly_lookup.request = MagicMock()

    mainwindow.JLCPCBTools.start_assembly_enrichment(window)

    window.assembly_lookup.request.assert_not_called()


@pytest.mark.parametrize("change", ["replaced", "renamed", "variants"])
def test_stale_footprint_selection_preserves_current_pcb_selection(
    make_window: Callable[..., Any], mainwindow: ModuleType, change: str
) -> None:
    """An old window cannot clear selection on a board context it no longer owns."""
    window = make_window()
    window.select_alike_in_progress = False
    window.auto_select_alike = False
    window.footprint_list.GetSelectedItemsCount.return_value = 1
    board = window.pcbnew.GetBoard()
    if change == "replaced":
        replacement = Board([Footprint("R1", lcsc="C200")])
        replacement.filename = board.filename
        window.pcbnew.GetBoard = lambda: replacement
    elif change == "renamed":
        board.filename = board.filename.replace("board.kicad_pcb", "renamed.kicad_pcb")
    else:
        board.GetVariantNamesForUI = lambda: ["Default", "A"]
    selected = SimpleNamespace(selected=True)
    selected.ClearSelected = lambda: setattr(selected, "selected", False)
    window.pcbnew.GetCurrentSelection = MagicMock(return_value=[selected])
    window.pcbnew.Refresh = MagicMock()
    errors = []

    try:
        window.OnFootprintSelected()
    except mainwindow.BoardContextChanged as error:
        errors.append(error)

    assert selected.selected
    assert not errors
    window.pcbnew.GetCurrentSelection.assert_not_called()
    window.pcbnew.Refresh.assert_not_called()
    assert window._project_storage_unavailable
    assert window.store is None
    assert not window.test_rows
    window.footprint_list.Enable.assert_called_with(False)
