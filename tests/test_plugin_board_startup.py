"""Saved-board and catalog recovery contracts through actual plugin constructors."""

from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import Mock, patch

import pytest

from .native_window_support import focus, window_ui
from .native_wx_support import run_native

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("filename", ["", "missing.kicad_pcb", "notes.txt"])
def test_unsaved_board_rejected_before_wx_or_database_initialization(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    ui = window_ui
    board_filename = str(ui.path / filename) if filename else ""
    if filename == "notes.txt":
        Path(board_filename).write_text("not a PCB")
    monkeypatch.setattr(ui.board, "GetFileName", lambda: board_filename)

    def check(host: Any, wx: Any) -> None:
        with (
            patch.object(
                wx, "GetApp", side_effect=AssertionError("Window creation began")
            ),
            pytest.raises(ValueError, match="[Ss]ave|existing"),
        ):
            ui.mainwindow.JLCPCBTools(host, ui.provider)
        assert not (ui.path / "jlcpcb").exists()

    run_native(check)


@pytest.mark.parametrize(
    "error", [ValueError("Save the PCB first"), RuntimeError("Database is unavailable")]
)
def test_plugin_reports_startup_error_without_showing_partial_window(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    constructor = Mock(side_effect=error)
    monkeypatch.setattr(window_ui.mainwindow, "JLCPCBTools", constructor)
    window_ui.plugin.JLCPCBPlugin().Run()
    constructor.assert_called_once_with(None)
    assert window_ui.messages == [str(error)]


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("database_state", ["missing", "read_only", "invalid"])
def test_existing_project_database_cannot_abort_plugin_action(
    window_ui: Any, variants: bool, database_state: str
) -> None:
    """Native variants ignore old storage; ordinary startup exposes storage failure."""
    ui = window_ui
    if not variants:
        ui.board.names.clear()
        ui.board.current = ""
    database = ui.path / "jlcpcb" / "project.db"
    before = None
    if database_state != "missing":
        database.parent.mkdir()
        if database_state == "read_only":
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE part_info (reference TEXT PRIMARY KEY)"
                )
            database.chmod(0o444)
        else:
            database.write_bytes(b"historical optional data unavailable")
        before = database.read_bytes()

    def check(ui: Any) -> None:
        assert ui.dialog.settings_button.IsEnabled()
        assert ui.dialog._variant_mode is variants
        if variants:
            assert ui.controller.session.reliable
            assert ui.dialog.generate_button.IsEnabled()
        else:
            assert ui.dialog._variant_controller is None
            assert ui.dialog._project_storage_unavailable is (before is not None)
        if before is None and variants:
            assert not database.exists()
        elif before is not None:
            assert database.read_bytes() == before

    try:
        ui.run(check)
    finally:
        if database_state == "read_only":
            database.chmod(0o644)


def test_startup_failure_stops_library_download(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = window_ui
    ui.failure = "missing"
    monkeypatch.setattr(
        ui.mainwindow.JLCPCBTools,
        "init_store",
        Mock(side_effect=ValueError("The native board is unavailable")),
    )

    def check(ui: Any) -> None:
        assert ui.controller is None and ui.dialog._project_storage_unavailable
        ui.catalog.update.assert_not_called()
        assert ui.dialog.settings_button.IsEnabled()

    ui.run(check)


@pytest.mark.parametrize(
    "failure",
    ["healthy", "missing", "unreadable", "invalid_metadata", "metadata_io_error"],
)
def test_catalog_recovery_and_edit_work_after_plugin_action_returns(
    window_ui: Any, failure: str
) -> None:
    """Failed startup recovers and edits through the actual modeless window."""
    window_ui.failure = failure

    def check(ui: Any) -> None:
        frame = ui.dialog
        failed = failure not in {"healthy", "missing"}
        assert frame.IsShown() and frame.IsEnabled()
        assert frame._project_storage_unavailable == failed
        assert (frame._variant_controller is None) == failed
        assert ui.catalog.update.call_count == int(failure == "missing")
        if failed:
            frame._publish_catalog()
            assert frame._project_storage_unavailable
            assert frame._variant_controller is None
        ui.failure = "healthy"
        ui.catalog.usable = True
        ui.catalog.state = ui.mainwindow.LibraryState.INITIALIZED
        frame._publish_catalog()
        controller = ui.controller = frame._variant_controller
        assert controller is not None and controller.view._mutations_enabled
        assert frame.content_panel.GetParent() is frame
        assert controller.panel.GetParent() is frame.content_panel
        assert controller.view.GetParent() is controller.panel
        assert controller.panel.GetContainingSizer() is not None
        assert not frame.footprint_list.IsShown()
        assert frame.is_catalog_available() and not frame._project_storage_unavailable
        controller._on_edit(focus(ui, "A"), "C999")
        assert ui.board.parts[0].GetFieldValueForVariant("A", "LCSC") == "C999"
        assert ui.board.parts[0].GetFieldValueForVariant("B", "LCSC") == "C1"
        assert ui.board.modified
        frame.Close()
        assert controller.closed and not controller.timer.IsRunning()
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("started_with_variants", [False, True])
def test_catalog_recovery_keeps_mode_chosen_when_action_started(
    window_ui: Any, started_with_variants: bool
) -> None:
    """Catalog recovery preserves the mode chosen when the window was opened."""
    ui = window_ui
    if not started_with_variants:
        ui.board.names.clear()
    ui.failure = "unreadable"

    def check(ui: Any) -> None:
        frame = ui.dialog
        assert frame._variant_controller is None
        ui.board.names = [] if started_with_variants else ["A"]
        ui.board.current = ""
        ui.failure = "healthy"
        ui.catalog.usable = True
        ui.catalog.state = ui.mainwindow.LibraryState.INITIALIZED
        frame._publish_catalog()
        assert frame._variant_mode == started_with_variants
        if started_with_variants:
            controller = ui.controller = frame._variant_controller
            assert [
                variant.name for variant in controller.session.snapshot.variants
            ] == [""]
            assert controller.view._mutations_enabled
            controller._on_edit(focus(ui, ""), "C999")
            assert ui.board.parts[0].GetFieldValueForVariant("", "LCSC") == "C999"
        else:
            assert frame._variant_controller is None and frame.store is None
            assert frame._project_storage_unavailable
            assert "reopen" in frame.project_storage_status.GetToolTipText().lower()
            frame.init_store()
            assert (
                frame._variant_controller is None and frame._project_storage_unavailable
            )
            assert not ui.board.modified

    ui.run(check)
