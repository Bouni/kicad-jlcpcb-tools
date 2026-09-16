"""Authoritative assembly choices and asynchronous notice lifecycle regressions."""

from collections.abc import Callable
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import (
    Footprint,
    project_rows,
    reject_second_project_update,
)

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.mark.parametrize(
    "action,fields",
    [
        ("toggle_bom", ("exclude_from_bom",)),
        ("toggle_pos", ("exclude_from_pos",)),
        ("toggle_bom_pos", ("exclude_from_bom", "exclude_from_pos")),
        ("toggle_dnp", ("is_dnp",)),
    ],
)
@pytest.mark.parametrize("fail", [False, True])
def test_flag_actions_are_atomic_and_never_write_native_settings(
    make_window: Callable[..., Any], action: str, fields: tuple[str, ...], fail: bool
) -> None:
    """A complete selection commits together; native edits cannot participate."""
    window = make_window(
        footprints=[Footprint(), Footprint("R2", bom=True, pos=True, dnp=True)]
    )
    board = window.pcbnew.GetBoard()
    for fp in board.GetFootprints():
        fp.SetAttributes = fp.SetDNP = fp.SetField = MagicMock(
            side_effect=AssertionError("native assembly mutation")
        )
    before = project_rows(window)
    window._refresh_footprints_preserving_selection = MagicMock(
        side_effect=window.populate_footprint_list
    )
    if fail:
        reject_second_project_update(window, fields[-1])

    getattr(window, action)()

    if fail:
        assert project_rows(window) == before
        window._refresh_footprints_preserving_selection.assert_not_called()
        assert "later assignment rejected" in str(window.logger.warning.call_args)
    else:
        for old, new in zip(before, project_rows(window)):
            assert all(new[field] == (not old[field]) for field in fields)
        window._refresh_footprints_preserving_selection.assert_called_once_with()
        reopened = type(window.store)(window, window.project_path, board)
        assert [
            (p["exclude_from_bom"], p["exclude_from_pos"], p["is_dnp"])
            for p in reopened.read_all()
        ] == [
            (p["exclude_from_bom"], p["exclude_from_pos"], p["is_dnp"])
            for p in project_rows(window)
        ]
    assert [(fp.attributes, fp.dnp) for fp in board.GetFootprints()] == [
        (0, False),
        (12, True),
    ]


@pytest.mark.parametrize("change", ["replace", "save_as", "closed"])
def test_old_window_disables_assignments_when_board_context_changes(
    make_window: Callable[..., Any], mainwindow: Any, change: str
) -> None:
    """A modeless window cannot rebind saved choices to another board."""
    window = make_window()
    board = window.pcbnew.GetBoard()
    before = project_rows(window)
    if change == "save_as":
        board.filename += ".renamed.kicad_pcb"
    else:
        window.pcbnew.GetBoard = (
            lambda: None if change == "closed" else support.Board([Footprint()])
        )
    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C999", type="Basic", stock=1)
    )
    assert window.store is None
    window.partlist_data_model.set_lcsc.assert_not_called()
    assert before[0]["lcsc"] == "C100"
    assert window.upper_toolbar.enabled[mainwindow.ID_GENERATE] is False


@pytest.mark.parametrize(
    "result,ack", [(True, True), (True, False), (False, False), (None, False)]
)
def test_schematic_notice_acknowledges_only_explicit_understood(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
    ack: bool,
) -> None:
    """Cancellation, absence and scan errors do not consume the per-board notice."""
    window = make_window()
    key = "schematic_lcsc_notice_v1:" + window.store.board_key
    dialog = MagicMock()
    dialog.ShowModal.return_value = (
        mainwindow.wx.ID_OK if ack else mainwindow.wx.ID_CANCEL
    )
    monkeypatch.setattr(
        mainwindow.wx, "MessageDialog", MagicMock(return_value=dialog), raising=False
    )
    window._show_schematic_notice(
        window.store, key, SimpleNamespace(found=result, detail="unavailable")
    )
    assert bool(window.store.get_metadata(key)) is ack
    assert mainwindow.wx.MessageDialog.called is (result is True)
    if result is True:
        dialog.SetOKLabel.assert_called_once_with("Understood")
        dialog.Destroy.assert_called_once_with()
    if ack:
        window._schematic_notice_started = False
        monkeypatch.setattr(mainwindow, "Thread", MagicMock())
        window._begin_schematic_notice()
        mainwindow.Thread.assert_not_called()


@pytest.mark.parametrize("closed", [True, False])
def test_notice_callback_after_close_or_replacement_has_no_dialog(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
) -> None:
    """Worker completion cannot show or acknowledge a notice for a stale window."""
    window = make_window()
    store = window.store
    if closed:
        window._closing = True
    else:
        window.pcbnew.GetBoard = lambda: support.Board([Footprint()])
    monkeypatch.setattr(mainwindow.wx, "MessageDialog", MagicMock(), raising=False)
    window._show_schematic_notice(store, "notice", SimpleNamespace(found=True))
    mainwindow.wx.MessageDialog.assert_not_called()


@pytest.mark.parametrize("closed", [False, True])
def test_deferred_bom_refresh_ignores_closed_or_replaced_board(
    make_window: Callable[..., Any], mainwindow: Any, closed: bool
) -> None:
    """An already-queued idle callback cannot access stale board controls."""
    window = make_window()
    window._bom_recompute_scheduled = True
    if closed:
        window._closing = True
    else:
        window.pcbnew.GetBoard = lambda: support.Board([Footprint()])
    window._run_coalesced_bom_recompute()
    window.partlist_data_model.set_stock_concern_refs.assert_not_called()
    if not closed:
        assert window.store is None


def test_explicit_clear_before_catalog_ready_stays_empty(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Deferred initial preferences cannot refill a deliberate empty choice."""
    window = make_window(
        footprints=[Footprint(lcsc="")], part_preferences={("R_0603", "10k"): "C200"}
    )
    window.library.state = mainwindow.LibraryState.UPDATE_NEEDED
    window.init_store()
    window.remove_lcsc_number()
    window.library.state = mainwindow.LibraryState.INITIALIZED
    window.init_store()
    assert window.store.get_part("R1")["lcsc"] == ""
    window.library.get_part_preference.assert_not_called()


def test_notice_delivery_observes_acknowledgment_from_another_window(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow scanner does not repeat a notice another window acknowledged."""
    window = make_window()
    key = "schematic_lcsc_notice_v1:" + window.store.board_key
    other_store = type(window.store)(
        window, window.project_path, window.pcbnew.GetBoard()
    )
    other_store.set_metadata(key, "1")
    monkeypatch.setattr(mainwindow.wx, "MessageDialog", MagicMock(), raising=False)
    window._show_schematic_notice(window.store, key, SimpleNamespace(found=True))
    mainwindow.wx.MessageDialog.assert_not_called()


@pytest.mark.parametrize("operation", ["get_metadata", "set_metadata"])
def test_notice_metadata_errors_are_reported_without_acknowledgment(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Notice bookkeeping failures stay within the asynchronous UI boundary."""
    window = make_window()
    dialog = MagicMock()
    dialog.ShowModal.return_value = mainwindow.wx.ID_OK
    monkeypatch.setattr(
        mainwindow.wx, "MessageDialog", MagicMock(return_value=dialog), raising=False
    )
    monkeypatch.setattr(
        window.store,
        operation,
        MagicMock(side_effect=sqlite3.OperationalError("notice database locked")),
    )
    window._show_schematic_notice(window.store, "notice", SimpleNamespace(found=True))
    assert "notice database locked" in str(window.logger.warning.call_args)
    if operation == "get_metadata":
        mainwindow.wx.MessageDialog.assert_not_called()
    else:
        dialog.Destroy.assert_called_once_with()
