"""Keep upgrade override audits visible once through actual native close events."""

from importlib import import_module
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import modal_handler, window_ui
from .test_part_info_autosave_retirement import has_legacy, seed_project

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("native_value", ["", "C111"])
@pytest.mark.parametrize("forced_first", [False, True])
def test_override_audit_survives_archive_and_acknowledges_only_interactively(
    window_ui: Any,
    variants: bool,
    native_value: str,
    forced_first: bool,
) -> None:
    """Old/new decisions survive transient cleanup and forced shutdown until displayed."""
    database = seed_project(window_ui, variants)
    window_ui.acknowledge_legacy_audits = False
    window_ui.board.parts[0].SetField("LCSC", native_value)
    window_ui.save_board()
    report = window_ui.path / "jlcpcb" / "legacy-migration-report.json"
    transient = window_ui.path / "jlcpcb" / "schematic-save-report.txt"
    transient.write_text("old partial failure", encoding="utf-8")
    notifications = []

    def acknowledge(ui: Any, dialog: Any) -> None:
        assert dialog.GetCaption() == "Legacy assignment migration"
        message = dialog.GetMessage()
        assert "C999" in message and "board.kicad_pcb" in message
        assert ("clear" in message.lower()) if native_value == "" else "C111" in message
        assert report.exists() and not transient.exists()
        assert not has_legacy(database)
        notifications.append(message)
        dialog.EndModal(ui.wx.ID_OK)

    def first(ui: Any) -> None:
        if forced_first:
            with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                assert ui.dialog.Close(force=True) is True
                show.assert_not_called()
        else:
            with modal_handler(
                ui, ui.wx.GenericMessageDialog, lambda dialog: acknowledge(ui, dialog)
            ):
                assert ui.dialog.Close() is True
        assert report.exists() and not has_legacy(database)

    def reopened(ui: Any) -> None:
        if forced_first:
            with modal_handler(
                ui, ui.wx.GenericMessageDialog, lambda dialog: acknowledge(ui, dialog)
            ):
                assert ui.dialog.Close() is True
        else:
            with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                assert ui.dialog.Close() is True
                show.assert_not_called()
        assert len(notifications) == 1
        helper = import_module(ui.mainwindow.__package__ + ".legacy_migration_report")
        assert not helper.load_pending_legacy_migration_reports(str(ui.path))
        assert report.exists() and "C999" in report.read_text(encoding="utf-8")

    window_ui.run(first, reopened, reopened)
