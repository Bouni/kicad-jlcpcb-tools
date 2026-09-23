"""Tests for exporting LCSC assignments to schematics from the main window."""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, call

import pytest

from schematic_safety import SchematicLockedError

from .wx_harness import load_mainwindow, wx_stubs

_PACKAGE = "mainwindow_schematic_export_tests"


@pytest.fixture
def mainwindow_module():
    """Provide an isolated mainwindow module and its wx stub."""
    module = load_mainwindow(
        _PACKAGE,
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            FileDialog=MagicMock(),
            MessageBox=MagicMock(),
            MessageDialog=MagicMock(),
        ),
    )
    return module, module.wx


def _export(
    mainwindow_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    board_schematic: bool = True,
    load_results: tuple = (None,),
    extra_roots: tuple = (),
    controller: Optional[MagicMock] = None,  # noqa: UP045
) -> tuple[SimpleNamespace, MagicMock]:
    """Run export_to_schematic on a minimal window and return it and the exporter."""
    mainwindow, _wx = mainwindow_module
    root = str(tmp_path / "board.kicad_sch")
    roots = [root, *extra_roots] if board_schematic else []
    monkeypatch.setattr(
        mainwindow, "resolve_project_schematics", lambda _project, _board: roots
    )
    exporter = MagicMock()
    exporter.load_schematic.side_effect = list(load_results)
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))
    monkeypatch.setattr(mainwindow, "SchematicLockedError", SchematicLockedError)

    window = SimpleNamespace(
        project_path=str(tmp_path),
        board_name="board.kicad_pcb",
        schematic_name="board.kicad_sch",
        logger=MagicMock(),
    )
    if controller is not None:
        window._variant_controller = controller
    window.confirm_locked_schematic_export = lambda error: (
        mainwindow.JLCPCBTools.confirm_locked_schematic_export(window, error)
    )
    mainwindow.JLCPCBTools.export_to_schematic(window)
    return window, exporter


def _lock_dialog(wx, result):
    """Configure and return the fake lock prompt."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = result
    wx.MessageDialog.return_value = dialog
    return dialog


def _logged_warning(logger: MagicMock) -> str:
    """Render the last lazy warning call into its message text."""
    message, *args = logger.warning.call_args.args
    return message % tuple(args)


def _locked(tmp_path: Path, *names: str) -> SchematicLockedError:
    """Return the error for locks held by alice on mac (board.kicad_sch by default)."""
    return SchematicLockedError(
        [
            (str(tmp_path / name), {"username": "alice", "hostname": "mac"})
            for name in names or ("board.kicad_sch",)
        ]
    )


def test_board_schematic_is_exported_without_a_file_dialog(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The schematic named after the board is exported straight away."""
    _mainwindow, wx = mainwindow_module

    _window, exporter = _export(mainwindow_module, monkeypatch, tmp_path)

    wx.FileDialog.assert_not_called()
    assert exporter.load_schematic.call_args_list == [
        call([str(tmp_path / "board.kicad_sch")])
    ]
    wx.MessageDialog.assert_not_called()
    wx.MessageBox.assert_not_called()


def test_every_top_level_schematic_is_exported(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All top-level sheets of the project go to one export."""
    _mainwindow, wx = mainwindow_module
    power = str(tmp_path / "power.kicad_sch")

    _window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, extra_roots=(power,)
    )

    wx.FileDialog.assert_not_called()
    assert exporter.load_schematic.call_args_list == [
        call([str(tmp_path / "board.kicad_sch"), power])
    ]


def test_without_a_board_schematic_the_user_picks_the_files(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no schematic named after the board, the chosen files are exported."""
    _mainwindow, wx = mainwindow_module
    picked = [str(tmp_path / "a.kicad_sch"), str(tmp_path / "b.kicad_sch")]
    picker = MagicMock()
    picker.__enter__.return_value = picker
    picker.ShowModal.return_value = wx.ID_OK
    picker.GetPaths.return_value = picked
    wx.FileDialog.return_value = picker

    window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, board_schematic=False
    )

    assert wx.FileDialog.call_args.args[:4] == (
        window,
        "Select Schematics",
        str(tmp_path),
        "board.kicad_sch",
    )
    assert exporter.load_schematic.call_args_list == [call(picked)]


def test_cancelling_the_file_dialog_exports_nothing(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Closing the schematic picker without choosing leaves the exporter unused."""
    _mainwindow, wx = mainwindow_module
    picker = MagicMock()
    picker.__enter__.return_value = picker
    picker.ShowModal.return_value = wx.ID_CANCEL
    wx.FileDialog.return_value = picker

    _window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, board_schematic=False
    )

    exporter.load_schematic.assert_not_called()
    picker.GetPaths.assert_not_called()


def test_locked_schematic_is_left_alone_when_the_user_cancels(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the lock prompt exports nothing."""
    _mainwindow, wx = mainwindow_module
    dialog = _lock_dialog(wx, wx.ID_NO)

    window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(_locked(tmp_path),)
    )

    assert exporter.load_schematic.call_count == 1
    message, title, style = wx.MessageDialog.call_args.args[1:]
    assert "locked by alice@mac" in message
    assert "save and close it first" in message
    assert message.endswith(
        "See KiCad issue #2077: https://gitlab.com/kicad/code/kicad/-/issues/2077"
    )
    assert title == "Schematic Locked"
    assert style & wx.NO_DEFAULT
    dialog.SetYesNoLabels.assert_called_once_with("Export Anyway", "Cancel")
    dialog.Destroy.assert_called_once_with()
    assert "User chose to stop" in _logged_warning(window.logger)
    wx.MessageBox.assert_not_called()


def test_locked_schematic_is_exported_when_the_user_insists(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Export Anyway repeats the export with the lock check turned off."""
    _mainwindow, wx = mainwindow_module
    _lock_dialog(wx, wx.ID_YES)
    root = str(tmp_path / "board.kicad_sch")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(_locked(tmp_path), None),
    )

    assert exporter.load_schematic.call_args_list == [
        call([root]),
        call([root], approved_locks=[root]),
    ]
    assert "User chose to continue" in _logged_warning(window.logger)
    wx.MessageBox.assert_not_called()


def test_export_failure_is_logged_and_reported(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any other failure keeps its traceback in the log and is shown to the user."""
    _mainwindow, wx = mainwindow_module
    missing = FileNotFoundError("Sheet file 'gone.kicad_sch' does not exist")

    window, _exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(missing,)
    )

    window.logger.exception.assert_called_once_with("Schematic export failed")
    message, title = wx.MessageBox.call_args.args
    assert message == f"Failed to export schematic: {missing}"
    assert title == "Schematic Export Error"
    wx.MessageDialog.assert_not_called()


def test_lock_prompt_lists_every_locked_schematic(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Export Anyway approves exactly the locks the prompt listed."""
    _mainwindow, wx = mainwindow_module
    _lock_dialog(wx, wx.ID_YES)
    error = _locked(tmp_path, "board.kicad_sch", "power.kicad_sch")

    _window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(error, None)
    )

    message = wx.MessageDialog.call_args.args[1]
    assert "'board.kicad_sch' by alice@mac" in message
    assert "'power.kicad_sch' by alice@mac" in message
    assert exporter.load_schematic.call_args_list[1] == call(
        [str(tmp_path / "board.kicad_sch")],
        approved_locks=[
            str(tmp_path / "board.kicad_sch"),
            str(tmp_path / "power.kicad_sch"),
        ],
    )


def test_lock_found_after_the_prompt_is_reported_not_written_past(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lock that appears after Export Anyway stops the export as a failure."""
    _mainwindow, wx = mainwindow_module
    _lock_dialog(wx, wx.ID_YES)
    later = _locked(tmp_path, "power.kicad_sch")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(_locked(tmp_path), later),
    )

    assert exporter.load_schematic.call_count == 2
    assert wx.MessageDialog.call_count == 1
    message, title = wx.MessageBox.call_args.args
    assert message == f"Failed to export schematic: {later}"
    assert title == "Schematic Export Error"
    window.logger.exception.assert_called_once_with("Schematic export failed")


def test_variant_boards_export_through_the_controller_with_the_lock_prompt(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With design variants, the controller exports and the lock prompt still applies."""
    mainwindow, wx = mainwindow_module
    _lock_dialog(wx, wx.ID_YES)
    root = str(tmp_path / "board.kicad_sch")
    controller = MagicMock()
    controller.export_to_schematic.side_effect = [_locked(tmp_path), None]

    _window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, controller=controller
    )

    assert controller.export_to_schematic.call_args_list == [
        call([root]),
        call([root], approved_locks=[root]),
    ]
    mainwindow.SchematicExport.assert_not_called()
    exporter.load_schematic.assert_not_called()
    wx.MessageBox.assert_not_called()
