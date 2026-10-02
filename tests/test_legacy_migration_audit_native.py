"""Keep upgrade override audits visible once through actual native close events."""

from copy import deepcopy
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


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("first_close", ["acknowledge", "forced", "cancel_notice"])
def test_sibling_retention_notice_survives_close_and_is_acknowledged_once(
    window_ui: Any, variants: bool, first_close: str
) -> None:
    """Real close events disclose a sibling once while its saved data stays protected."""
    database = seed_project(window_ui, variants)
    window_ui.acknowledge_legacy_audits = False
    window_ui.board.parts[0].SetField("LCSC", "C999")
    window_ui.save_board()
    sibling = deepcopy(window_ui.board)
    sibling.parts[0].fields.pop("LCSC")
    sibling_path = window_ui.path / "tmp-recovery.kicad_pcb"
    window_ui.save_board(sibling, str(sibling_path))
    original_sibling = sibling_path.read_bytes()
    report = window_ui.path / "jlcpcb" / "legacy-migration-report.json"
    notifications = []

    def acknowledge(ui: Any, dialog: Any) -> None:
        assert dialog.GetCaption() == "Legacy assignment migration"
        message = dialog.GetMessage()
        assert sibling_path.name in message and "R1" in message and "C999" in message
        assert report.exists() and has_legacy(database)
        notifications.append(message)
        dialog.EndModal(ui.wx.ID_OK)

    def close_quietly(ui: Any, *, forced: bool = False) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close(force=forced) is True
            show.assert_not_called()

    def first(ui: Any) -> None:
        if first_close == "forced":
            close_quietly(ui, forced=True)
        elif first_close == "cancel_notice":

            def cancel(dialog: Any) -> None:
                assert sibling_path.name in dialog.GetMessage()
                dialog.EndModal(ui.wx.ID_CANCEL)

            with modal_handler(ui, ui.wx.GenericMessageDialog, cancel):
                assert ui.dialog.Close() is True
        else:
            with modal_handler(
                ui, ui.wx.GenericMessageDialog, lambda dialog: acknowledge(ui, dialog)
            ):
                assert ui.dialog.Close() is True
        assert has_legacy(database) and sibling_path.read_bytes() == original_sibling

    def reopened(ui: Any) -> None:
        if first_close != "acknowledge":
            with modal_handler(
                ui, ui.wx.GenericMessageDialog, lambda dialog: acknowledge(ui, dialog)
            ):
                assert ui.dialog.Close() is True
        else:
            close_quietly(ui)
        assert len(notifications) == 1 and has_legacy(database)

    def resolve(ui: Any) -> None:
        close_quietly(ui)
        assert has_legacy(database) and len(notifications) == 1
        sibling.parts[0].SetField("LCSC", "C999")
        ui.save_board(sibling, str(sibling_path))

    def archived(ui: Any) -> None:
        close_quietly(ui)
        assert not has_legacy(database) and len(notifications) == 1

    window_ui.run(first, reopened, resolve, archived, archived)
