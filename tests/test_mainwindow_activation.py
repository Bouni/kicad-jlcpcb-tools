"""Live board refresh and native edit boundaries for modeless ordinary windows."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Board, Footprint

mainwindow = support.mainwindow
make_window = support.make_window


def test_activation_refreshes_native_edits_without_refilling_cleared_assignments(
    make_window: Callable[..., Any],
) -> None:
    """Return from PCB editing refreshes rows and estimates without another auto-fill."""
    window = make_window(
        footprints=[Footprint("R1", lcsc=""), Footprint("R2", lcsc="C300")],
        part_preferences={("R_0603", "10k"): "C200"},
    )
    window.init_store()
    assert window.test_rows["R1"]["lcsc"] == "C200"
    board = window.pcbnew.GetBoard()
    board.footprints["R1"].SetField("LCSC", "")
    board.footprints["R2"].SetField("LCSC", "C400")
    board.footprints["R2"].value = "20k"
    board.footprints["R2"].SetAttributes(1 << 3)
    window.start_assembly_enrichment.reset_mock()
    window.recompute_bom_estimate.reset_mock()
    window.recompute_stock_concerns = MagicMock()
    event = SimpleNamespace(Skip=MagicMock(), GetActive=lambda: True)

    window.on_window_activated(event)

    assert window.test_rows["R1"]["lcsc"] == ""
    assert window.test_rows["R2"]["lcsc"] == "C400"
    assert window.test_rows["R2"]["value"] == "20k"
    assert window.test_rows["R2"]["exclude_from_bom"]
    window.library.get_part_preference.assert_called_once_with("R_0603", "10k")
    window.start_assembly_enrichment.assert_called_once_with()
    window.recompute_bom_estimate.assert_called_once_with()
    window.recompute_stock_concerns.assert_called_once_with()
    assert {call.args[0] for call in window.footprint_list.Select.call_args_list} == {
        "R1",
        "R2",
    }
    event.Skip.assert_called_once_with()
    assert window._refreshing_board is False


@pytest.mark.parametrize("blocked", ["inactive", "closing", "refreshing", "generating"])
def test_activation_skips_unusable_or_nested_events(
    make_window: Callable[..., Any], blocked: str
) -> None:
    """Native activation does not reenter a refresh or an ongoing board edit."""
    window = make_window()
    window._closing = blocked == "closing"
    window._refreshing_board = blocked == "refreshing"
    window._ordinary_generating = blocked == "generating"
    event = SimpleNamespace(Skip=MagicMock(), GetActive=lambda: blocked != "inactive")

    window.on_window_activated(event)

    window.populate_footprint_list.assert_not_called()
    window.recompute_bom_estimate.assert_not_called()
    event.Skip.assert_called_once_with()


@pytest.mark.parametrize("change", ["replaced", "renamed", "variants"])
@pytest.mark.parametrize(
    "action", ["activate", "assign", "clear", "toggle", "save", "apply"]
)
def test_stale_window_refuses_board_replacement_save_as_and_new_variants(
    make_window: Callable[..., Any], change: str, action: str
) -> None:
    """An old modeless window cannot map references onto a changed board context."""
    window = make_window()
    original = window.pcbnew.GetBoard()
    current = original
    if change == "replaced":
        current = Board([Footprint("R1", lcsc="C200")])
        current.filename = original.filename
        window.pcbnew.GetBoard = lambda: current
    elif change == "renamed":
        current.filename = current.filename.replace(
            "board.kicad_pcb", "renamed.kicad_pcb"
        )
    else:
        current.GetVariantNamesForUI = lambda: ["Default", "Alternative"]
    before = current.footprints["R1"].field.text
    if action == "activate":
        window.on_window_activated(
            SimpleNamespace(Skip=MagicMock(), GetActive=lambda: True)
        )
    elif action == "assign":
        window.assign_parts(
            SimpleNamespace(references=["R1"], lcsc="C999", type="Basic", stock=27)
        )
    elif action == "clear":
        window.remove_lcsc_number()
    elif action == "toggle":
        window.toggle_bom()
    elif action == "save":
        window.save_selected_part_preferences()
    else:
        window.apply_selected_part_preferences()

    assert current.footprints["R1"].field.text == before
    assert original.footprints["R1"].field.text == "C100"
    assert current.footprints["R1"].GetAttributes() == 0
    assert window._project_storage_unavailable is True
    window.library.save_part_preferences.assert_not_called()
    window.start_assembly_enrichment.assert_not_called()


def test_assignment_runs_in_native_action_before_publishing_model_and_preferences(
    make_window: Callable[..., Any],
) -> None:
    """Native action ownership encloses the edit before GUI success is observable."""
    window = make_window()
    calls: list[str] = []

    def board_action(change: Callable[[], None]) -> None:
        assert window.pcbnew.GetBoard().footprints["R1"].field.text == "C100"
        calls.append("begin")
        change()
        assert window.pcbnew.GetBoard().footprints["R1"].field.text == "C200"
        window.partlist_data_model.set_lcsc.assert_not_called()
        window.library.save_part_preferences.assert_not_called()
        calls.append("end")

    window._board_action = board_action
    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C200", type="Basic", stock=27)
    )

    assert calls == ["begin", "end"]
    assert window.test_rows["R1"]["lcsc"] == "C200"
    window.library.save_part_preferences.assert_called_once_with(
        [("R_0603", "10k", "C200")]
    )
