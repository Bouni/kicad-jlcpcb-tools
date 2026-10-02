"""Complete native assignment and exclusion workflows after footprint deletion."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import call

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Footprint

mainwindow = support.mainwindow
make_window = support.make_window


def test_populate_footprint_list_skips_deleted_and_retains_live_row(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """A refresh must omit deleted board rows without losing live rows."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    del window.pcbnew.GetBoard().footprints["R1"]

    mainwindow.JLCPCBTools.populate_footprint_list(window)

    assert [
        call.args[0][0] for call in window.partlist_data_model.AddEntry.call_args_list
    ] == ["R2"]
    assert window.store.get_part("R1") is None


def test_assign_parts_skips_stale_refs_and_continues_live_refs(
    make_window: Callable[..., Any],
) -> None:
    """Assignment mutates and enriches only references still on the board."""
    removed, live = Footprint("R1"), Footprint("R2")
    window = make_window(footprints=[removed, live])
    del window.pcbnew.GetBoard().footprints["R1"]

    window.assign_parts(
        SimpleNamespace(
            lcsc="C12345", stock="27", type="Basic", references=["R1", "R2"]
        )
    )

    assert removed.field.text == "C100"
    assert live.field.text == "C12345"
    assert window.store.get_part("R2")["lcsc"] == "C12345"
    assert window.partlist_data_model.set_lcsc.call_args_list == [
        call("R2", "C12345", "Basic", "27", "10k resistor")
    ]
    window.start_assembly_enrichment.assert_called_once_with(["R2"])


def test_assign_parts_with_only_stale_refs_does_not_start_enrichment(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """An all-stale selector result is a no-op, including notifications."""
    removed = Footprint("R1")
    window = make_window(footprints=[removed])
    window.pcbnew.GetBoard().footprints.clear()

    window.assign_parts(
        SimpleNamespace(lcsc="C12345", stock="27", type="Basic", references=["R1"])
    )

    assert removed.field.text == "C100"
    assert window.store.read_all() == []
    window.partlist_data_model.set_lcsc.assert_not_called()
    window.start_assembly_enrichment.assert_not_called()
    mainwindow.wx.PostEvent.assert_not_called()


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos", "toggle_bom_pos"])
def test_toggle_handlers_skip_stale_refs_and_continue_live_refs(
    make_window: Callable[..., Any], handler_name: str
) -> None:
    """Exclusion actions change live native attributes without touching deleted objects."""
    removed, live = Footprint("R1"), Footprint("R2")
    window = make_window(footprints=[removed, live])
    del window.pcbnew.GetBoard().footprints["R1"]

    getattr(window, handler_name)()

    expected_bom = handler_name in {"toggle_bom", "toggle_bom_pos"}
    expected_pos = handler_name in {"toggle_pos", "toggle_bom_pos"}
    assert removed.GetAttributes() == 0
    assert live.GetAttributes() == ((int(expected_bom) << 3) | (int(expected_pos) << 2))
    part = window.store.get_part("R2")
    assert bool(part["exclude_from_bom"]) == expected_bom
    assert bool(part["exclude_from_pos"]) == expected_pos
    getattr(window.partlist_data_model, handler_name).assert_called_once_with("R2")
