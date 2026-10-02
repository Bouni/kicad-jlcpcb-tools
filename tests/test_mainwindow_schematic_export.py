"""Tests for exporting LCSC assignments to schematics from the main window."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, call

import pytest

from schematic_safety import SchematicLockedError, authenticated_project_name

from .wx_harness import load_mainwindow, wx_stubs

_PACKAGE = "mainwindow_schematic_export_tests"


@pytest.fixture
def mainwindow_module() -> tuple[Any, Any]:
    """Provide an isolated mainwindow module and its wx stub."""
    module = load_mainwindow(
        _PACKAGE,
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            FileDialog=MagicMock(),
            MessageBox=MagicMock(),
            GenericMessageDialog=MagicMock(),
            MessageDialog=MagicMock(),
        ),
    )
    return module, module.wx


def _export(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    board_schematic: bool = True,
    load_results: tuple = (None,),
    extra_roots: tuple = (),
    controller: Optional[MagicMock] = None,  # noqa: UP045
    interactive: bool = True,
    pcbnew: Optional[SimpleNamespace] = None,  # noqa: UP045
    original_board_identity: Optional[str] = None,  # noqa: UP045
    resolution_error: Optional[Exception] = None,  # noqa: UP045
    backup: Any = FileExistsError("already backed up"),
) -> tuple[SimpleNamespace, MagicMock]:
    """Run export_to_schematic on a minimal window and return it and the exporter."""
    mainwindow, _wx = mainwindow_module
    root = str(tmp_path / "board.kicad_sch")
    roots = [root, *extra_roots] if board_schematic else []
    monkeypatch.setattr(
        mainwindow,
        "discover_project_schematics",
        MagicMock(
            return_value=SimpleNamespace(
                paths=tuple(roots),
                status="present" if roots else "absent",
                diagnostics=(),
                source_state=(),
            ),
            side_effect=resolution_error,
        ),
    )
    exporter = MagicMock()
    exporter.load_schematic.side_effect = [
        SimpleNamespace(retirement_eligible=True, diagnostics=())
        if result is None
        else result
        for result in load_results
    ]
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))
    monkeypatch.setattr(mainwindow, "SchematicLockedError", SchematicLockedError)
    backup_schematics = MagicMock(side_effect=backup)
    monkeypatch.setattr(mainwindow, "backup_schematics", backup_schematics)

    window = SimpleNamespace(
        project_path=str(tmp_path),
        board_name="board.kicad_pcb",
        schematic_name="board.kicad_sch",
        logger=MagicMock(),
        backup_schematics=backup_schematics,
        store=SimpleNamespace(dbfile=str(tmp_path / "jlcpcb" / "project.db")),
    )
    if controller is not None:
        window._variant_controller = controller
    if pcbnew is not None:
        window.pcbnew = pcbnew
    if original_board_identity is not None:
        window._schematic_board_identity = original_board_identity
    window.confirm_locked_schematic_export = lambda error: (
        mainwindow.JLCPCBTools.confirm_locked_schematic_export(window, error)
    )
    window._backup_schematics_before_first_write = (
        lambda *, interactive, approved_locks=(): (
            mainwindow.JLCPCBTools._backup_schematics_before_first_write(
                window, interactive=interactive, approved_locks=approved_locks
            )
        )
    )
    window._report_schematic_save = lambda diagnostics, **kwargs: (
        mainwindow.JLCPCBTools._report_schematic_save(window, diagnostics, **kwargs)
    )
    window._finalize_legacy_assignments = lambda outcome, **kwargs: (
        mainwindow.JLCPCBTools._finalize_legacy_assignments(window, outcome, **kwargs)
    )
    window._show_pending_legacy_migration_audit = lambda **kwargs: (
        mainwindow.JLCPCBTools._show_pending_legacy_migration_audit(window, **kwargs)
    )
    window._legacy_migration_state = lambda: (
        mainwindow.JLCPCBTools._legacy_migration_state(window)
    )
    window.save_result = mainwindow.JLCPCBTools.export_to_schematic(
        window, interactive=interactive
    )
    return window, exporter


def _lock_dialog(wx: Any, result: int) -> MagicMock:
    """Configure and return the fake lock prompt."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = result
    wx.GenericMessageDialog.return_value = dialog
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
    wx.GenericMessageDialog.assert_not_called()
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


@pytest.mark.parametrize("variant", [False, True])
def test_authenticated_project_without_root_uuid_still_shares_component_context(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: bool,
) -> None:
    """Authenticated multi-root membership does not require project UUID overrides."""
    mainwindow, _wx = mainwindow_module
    monkeypatch.setattr(
        mainwindow, "authenticated_project_name", lambda *_args: "board"
    )
    controller = MagicMock() if variant else None
    if controller is not None:
        controller.export_to_schematic.return_value = SimpleNamespace(
            retirement_eligible=True, diagnostics=()
        )
    _window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        extra_roots=(str(tmp_path / "power.kicad_sch"),),
        controller=controller,
    )
    active = controller if controller is not None else exporter
    method = active.export_to_schematic if variant else active.load_schematic
    assert method.call_args.kwargs["shared_project"] is True


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("closed_board", [False, True])
def test_without_a_project_schematic_no_picker_or_export_is_opened(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interactive: bool,
    closed_board: bool,
) -> None:
    """A PCB-only project can close without inventing an unrelated export target."""
    _mainwindow, wx = mainwindow_module
    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        board_schematic=False,
        interactive=interactive,
        pcbnew=SimpleNamespace(GetBoard=lambda: None) if closed_board else None,
        original_board_identity="closed-board" if closed_board else None,
    )
    exporter.load_schematic.assert_not_called()
    wx.FileDialog.assert_not_called()
    wx.GenericMessageDialog.assert_not_called()
    assert window.save_result is True


def test_locked_schematic_is_left_alone_when_the_user_cancels(
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the lock prompt exports nothing."""
    _mainwindow, wx = mainwindow_module
    dialog = _lock_dialog(wx, wx.ID_CANCEL)

    window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(_locked(tmp_path),)
    )

    assert exporter.load_schematic.call_count == 1
    message, title, style = wx.GenericMessageDialog.call_args.args[1:]
    assert "locked by alice@mac" in message
    assert "save and close it first" in message
    assert message.endswith(
        "See KiCad issue #2077: https://gitlab.com/kicad/code/kicad/-/issues/2077"
    )
    assert title == "Schematic Locked"
    assert style & wx.CANCEL_DEFAULT
    dialog.SetYesNoCancelLabels.assert_called_once_with(
        "Save Anyway", "Close without saving", "Cancel"
    )
    dialog.Destroy.assert_called_once_with()
    assert "User chose to stop" in _logged_warning(window.logger)
    wx.MessageBox.assert_not_called()
    assert window.save_result is False


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
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any other failure keeps its traceback in the log and is shown to the user."""
    _mainwindow, wx = mainwindow_module
    dialog = _lock_dialog(wx, wx.ID_NO)
    missing = FileNotFoundError("Sheet file 'gone.kicad_sch' does not exist")

    window, _exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(missing,)
    )

    window.logger.exception.assert_called_once_with("Automatic schematic save failed")
    message, title, style = wx.GenericMessageDialog.call_args.args[1:]
    assert str(missing) in message
    assert title == "Schematic save failed"
    assert style & wx.NO_DEFAULT
    dialog.SetYesNoLabels.assert_called_once_with("Close without saving", "Keep open")
    dialog.Destroy.assert_called_once_with()
    assert window.save_result is False


@pytest.mark.parametrize("failure", ["locked", "write"])
def test_noninteractive_failure_never_prompts_or_retries(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """Forced close cannot approve a lock or wait for recovery input."""
    _mainwindow, wx = mainwindow_module
    error = _locked(tmp_path) if failure == "locked" else OSError("Disk is full")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(error,),
        interactive=False,
    )

    assert window.save_result is False
    exporter.load_schematic.assert_called_once_with([str(tmp_path / "board.kicad_sch")])
    window.logger.exception.assert_called_once_with("Automatic schematic save failed")
    wx.GenericMessageDialog.assert_not_called()
    wx.MessageBox.assert_not_called()
    wx.FileDialog.assert_not_called()


@pytest.mark.parametrize("failure", ["locked", "write"])
def test_explicit_close_without_saving_does_not_retry_export(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """Users can discard the pending schematic save after either kind of failure."""
    _mainwindow, wx = mainwindow_module
    error = _locked(tmp_path) if failure == "locked" else OSError("Disk is full")
    # The lock prompt's NO and the failure prompt's YES both mean close unsaved.
    response = wx.ID_NO if failure == "locked" else wx.ID_YES
    dialog = _lock_dialog(wx, response)

    window, exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(error,)
    )

    assert window.save_result is None
    exporter.load_schematic.assert_called_once_with([str(tmp_path / "board.kicad_sch")])
    wx.GenericMessageDialog.assert_called_once()
    dialog.Destroy.assert_called_once_with()


def _board(filename: Path) -> SimpleNamespace:
    """Provide stateful board identity without retaining a native KiCad handle."""
    board = SimpleNamespace(filename=str(filename))
    board.GetFileName = lambda: board.filename
    return board


def _change_board(pcbnew: SimpleNamespace, change: str, tmp_path: Path) -> None:
    """Apply an editor change observable through the production board identity."""
    if change == "replacement":
        pcbnew.board = _board(tmp_path / "board.kicad_pcb")
    elif change == "save_as":
        pcbnew.board.filename = str(tmp_path / "renamed.kicad_pcb")
    else:
        pcbnew.board = None


@pytest.mark.parametrize("change", ["replacement", "save_as", "closed"])
@pytest.mark.parametrize("after_approval", [False, True])
def test_changed_board_never_exports_stale_assignments(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
    after_approval: bool,
) -> None:
    """The original board must still be current before writing, including after a prompt."""
    mainwindow, wx = mainwindow_module
    pcbnew = SimpleNamespace(board=_board(tmp_path / "board.kicad_pcb"))
    pcbnew.GetBoard = lambda: pcbnew.board
    original_identity = mainwindow.board_identity(pcbnew.board)
    dialog = _lock_dialog(wx, wx.ID_NO)

    if after_approval:
        responses = iter([wx.ID_YES, wx.ID_NO])

        def approve_then_report_error() -> int:
            """Replace the current board while the lock warning owns the event loop."""
            _change_board(pcbnew, change, tmp_path)
            return next(responses)

        dialog.ShowModal.side_effect = approve_then_report_error
    else:
        _change_board(pcbnew, change, tmp_path)

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(_locked(tmp_path), None),
        pcbnew=pcbnew,
        original_board_identity=original_identity,
    )

    assert window.save_result is False
    if after_approval:
        # Only the first, locked attempt reaches the exporter; approval does not
        # authorize exporting data belonging to a board that is no longer current.
        exporter.load_schematic.assert_called_once_with(
            [str(tmp_path / "board.kicad_sch")]
        )
        assert wx.GenericMessageDialog.call_count == 2
    else:
        mainwindow.SchematicExport.assert_not_called()
        exporter.load_schematic.assert_not_called()
        wx.GenericMessageDialog.assert_called_once()
    assert wx.GenericMessageDialog.call_args.args[2] == "Schematic save failed"
    window.logger.exception.assert_called_once_with("Automatic schematic save failed")


@pytest.mark.parametrize("interactive", [False, True])
def test_path_resolution_failure_uses_the_save_failure_path(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interactive: bool,
) -> None:
    """Failures before an exporter exists still keep a normal close recoverable."""
    mainwindow, wx = mainwindow_module
    dialog = _lock_dialog(wx, wx.ID_NO)
    error = OSError("Cannot read the project directory")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        interactive=interactive,
        resolution_error=error,
    )

    assert window.save_result is False
    mainwindow.SchematicExport.assert_not_called()
    exporter.load_schematic.assert_not_called()
    window.logger.exception.assert_called_once_with("Automatic schematic save failed")
    if interactive:
        message, title, _style = wx.GenericMessageDialog.call_args.args[1:]
        assert str(error) in message
        assert title == "Schematic save failed"
        dialog.Destroy.assert_called_once_with()
    else:
        wx.GenericMessageDialog.assert_not_called()


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

    message = wx.GenericMessageDialog.call_args.args[1]
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
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lock that appears after Export Anyway stops the export as a failure."""
    _mainwindow, wx = mainwindow_module
    _lock_dialog(wx, wx.ID_YES).ShowModal.side_effect = [wx.ID_YES, wx.ID_NO]
    later = _locked(tmp_path, "power.kicad_sch")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(_locked(tmp_path), later),
    )

    assert exporter.load_schematic.call_count == 2
    assert wx.GenericMessageDialog.call_count == 2
    message, title, _style = wx.GenericMessageDialog.call_args.args[1:]
    assert str(later) in message
    assert title == "Schematic save failed"
    window.logger.exception.assert_called_once_with("Automatic schematic save failed")
    assert window.save_result is False


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


def test_roots_come_from_the_loaded_project_for_a_renamed_board(
    mainwindow_module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The project is identified through pcbnew, not assumed from the board name."""
    mainwindow, _wx = mainwindow_module
    (tmp_path / "realproject.kicad_pro").write_text("{}", encoding="utf-8")
    project = object()
    pcbnew = SimpleNamespace(
        GetBoard=lambda: SimpleNamespace(GetProject=lambda: project),
        GetSettingsManager=lambda: SimpleNamespace(
            GetProject=lambda path: project
            if path.endswith("realproject.kicad_pro")
            else None
        ),
    )
    asked: list[tuple] = []

    def resolve(project_path: str, board: str, project_name: str) -> Any:
        asked.append((project_path, board, project_name))
        return SimpleNamespace(
            paths=(str(tmp_path / "power.kicad_sch"),),
            status="present",
            diagnostics=(),
            source_state=(),
        )

    monkeypatch.setattr(mainwindow, "discover_project_schematics", resolve)
    monkeypatch.setattr(
        mainwindow, "authenticated_project_name", authenticated_project_name
    )
    exporter = MagicMock()
    exporter.load_schematic.return_value = SimpleNamespace(
        retirement_eligible=True, diagnostics=()
    )
    monkeypatch.setattr(mainwindow, "SchematicExport", MagicMock(return_value=exporter))
    monkeypatch.setattr(
        mainwindow, "backup_schematics", MagicMock(side_effect=FileExistsError())
    )
    window = SimpleNamespace(
        project_path=str(tmp_path),
        board_name="board.kicad_pcb",
        schematic_name="board.kicad_sch",
        logger=MagicMock(),
        pcbnew=pcbnew,
        store=SimpleNamespace(dbfile=str(tmp_path / "jlcpcb" / "project.db")),
    )
    window._finalize_legacy_assignments = lambda outcome, **kwargs: (
        mainwindow.JLCPCBTools._finalize_legacy_assignments(window, outcome, **kwargs)
    )
    window._show_pending_legacy_migration_audit = lambda **kwargs: (
        mainwindow.JLCPCBTools._show_pending_legacy_migration_audit(window, **kwargs)
    )
    window._legacy_migration_state = lambda: (
        mainwindow.JLCPCBTools._legacy_migration_state(window)
    )
    window._report_schematic_save = lambda diagnostics, **kwargs: (
        mainwindow.JLCPCBTools._report_schematic_save(window, diagnostics, **kwargs)
    )
    window._backup_schematics_before_first_write = (
        lambda *, interactive, approved_locks=(): (
            mainwindow.JLCPCBTools._backup_schematics_before_first_write(
                window, interactive=interactive, approved_locks=approved_locks
            )
        )
    )
    assert mainwindow.JLCPCBTools.export_to_schematic(window) is True

    assert asked == [(str(tmp_path), "board.kicad_pcb", "realproject")]
    exporter.load_schematic.assert_called_once_with(
        [str(tmp_path / "power.kicad_sch")], shared_project=True
    )


def test_first_export_creates_the_fixed_name_pre_write_backup(
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first successful save creates the permanent pre-write zip once."""
    mainwindow, _wx = mainwindow_module
    backup_path = str(tmp_path / "jlcpcb" / mainwindow.SCHEMATIC_PRE_WRITE_BACKUP_ZIP)

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        backup=lambda *args, **kwargs: backup_path,
    )

    exporter.load_schematic.assert_called_once()
    assert window.backup_schematics.call_count == 1
    assert window.backup_schematics.call_args.args[2] == (
        mainwindow.SCHEMATIC_PRE_WRITE_BACKUP_ZIP
    )
    assert window.save_result is True


def test_existing_pre_write_backup_is_not_recreated(
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second save treats FileExistsError as the durable one-time backup."""
    _mainwindow, _wx = mainwindow_module

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        backup=FileExistsError("already backed up"),
    )

    exporter.load_schematic.assert_called_once()
    window.backup_schematics.assert_called_once()
    assert window.save_result is True


def test_backup_lock_skips_without_prompt_and_still_exports(
    mainwindow_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A locked pre-write backup must not open a dialog or veto the schematic save."""
    mainwindow, wx = mainwindow_module

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        backup=_locked(tmp_path),
    )

    exporter.load_schematic.assert_called_once()
    wx.GenericMessageDialog.assert_not_called()
    assert "Schematic pre-write backup skipped while locked" in _logged_warning(
        window.logger
    )
    assert window.save_result is True


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("ID_YES", True),
        ("ID_NO", None),
        ("ID_CANCEL", False),
    ],
)
def test_backup_failure_prompts_before_overwriting_schematics(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: str,
    expected: Optional[bool],
) -> None:
    """Backup OSError/ValueError must ask before the first force-write proceeds."""
    _mainwindow, wx = mainwindow_module
    dialog = _lock_dialog(wx, getattr(wx, response))
    error = ValueError("duplicate member names in backup zip")

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        backup=error,
    )

    assert "Schematic pre-write backup failed" in _logged_warning(window.logger)
    message, title, style = wx.GenericMessageDialog.call_args.args[1:]
    assert str(error) in message
    assert title == "Schematic backup failed"
    assert style & wx.CANCEL_DEFAULT
    dialog.SetYesNoCancelLabels.assert_called_once_with(
        "Save without backup", "Close without saving", "Keep open"
    )
    dialog.Destroy.assert_called_once_with()
    assert window.save_result is expected
    if expected is True:
        exporter.load_schematic.assert_called_once()
    else:
        exporter.load_schematic.assert_not_called()


@pytest.mark.parametrize("error", [OSError("Disk is full"), ValueError("bad zip")])
def test_forced_backup_failure_skips_write_without_prompt(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
) -> None:
    """Forced close cannot acknowledge a missing pre-write backup."""
    _mainwindow, wx = mainwindow_module

    window, exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        backup=error,
        interactive=False,
    )

    assert window.save_result is False
    exporter.load_schematic.assert_not_called()
    wx.GenericMessageDialog.assert_not_called()
    assert "Schematic pre-write backup failed" in _logged_warning(window.logger)


@pytest.mark.parametrize("interactive", [False, True])
def test_partial_close_persists_every_diagnostic_before_window_disappears(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interactive: bool,
) -> None:
    """The full preservation report survives both ordinary and forced successful close."""
    _mainwindow, wx = mainwindow_module
    diagnostics = (
        "R1 [uuid-1]: conflicting assignment preserved",
        "R2 [uuid-2]: link unresolved",
    )
    outcome = SimpleNamespace(retirement_eligible=False, diagnostics=diagnostics)

    window, _exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(outcome,),
        interactive=interactive,
    )

    assert window.save_result is True
    report = tmp_path / "jlcpcb" / "schematic-save-report.txt"
    assert all(message in report.read_text(encoding="utf-8") for message in diagnostics)
    assert wx.GenericMessageDialog.call_count == int(interactive)
    if interactive:
        assert str(report) in wx.GenericMessageDialog.call_args.args[1]
        assert (
            wx.GenericMessageDialog.call_args.args[2]
            == "Schematic assignments preserved"
        )
        wx.GenericMessageDialog.return_value.ShowModal.assert_called_once()
        wx.GenericMessageDialog.return_value.Destroy.assert_called_once()


def test_complete_close_removes_stale_partial_report_without_prompt(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A previous warning cannot look current after every assignment is safely saved."""
    report = tmp_path / "jlcpcb" / "schematic-save-report.txt"
    report.parent.mkdir()
    report.write_text("old unresolved R1", encoding="utf-8")

    window, _exporter = _export(mainwindow_module, monkeypatch, tmp_path)

    assert window.save_result is True and not report.exists()
    mainwindow_module[1].GenericMessageDialog.assert_not_called()


@pytest.mark.parametrize("interactive", [False, True])
def test_report_write_failure_does_not_turn_completed_save_into_failure(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interactive: bool,
) -> None:
    """Show the full report synchronously when persistence fails and a UI is available."""
    (tmp_path / "jlcpcb").write_text("blocks directory creation", encoding="utf-8")
    diagnostics = ("R1 [uuid-1]: unresolved", "R2 [uuid-2]: conflicting aliases")
    outcome = SimpleNamespace(retirement_eligible=False, diagnostics=diagnostics)

    window, _exporter = _export(
        mainwindow_module,
        monkeypatch,
        tmp_path,
        load_results=(outcome,),
        interactive=interactive,
    )

    assert window.save_result is True
    wx = mainwindow_module[1]
    assert wx.GenericMessageDialog.call_count == int(interactive)
    if interactive:
        message = wx.GenericMessageDialog.call_args.args[1]
        assert "could not be saved" in message
        assert all(diagnostic in message for diagnostic in diagnostics)


@pytest.mark.parametrize("note", ["information", "advisory"])
def test_routine_and_nonblocking_preservation_notes_do_not_create_close_warning(
    mainwindow_module: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    note: str,
) -> None:
    """Routine no-ops and Update PCB notes leave normal close free of modal warnings."""
    outcome = SimpleNamespace(
        retirement_eligible=True,
        diagnostics=(),
        information=("PCB-only hardware preserved",) if note == "information" else (),
        advisory=("R1: run Update PCB to copy its schematic assignment",)
        if note == "advisory"
        else (),
    )
    window, _exporter = _export(
        mainwindow_module, monkeypatch, tmp_path, load_results=(outcome,)
    )

    assert window.save_result is True
    assert not (tmp_path / "jlcpcb" / "schematic-save-report.txt").exists()
    mainwindow_module[1].GenericMessageDialog.assert_not_called()
