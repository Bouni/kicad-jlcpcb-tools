"""Tests for empty-zone warnings during fabrication-data generation."""

from collections.abc import Callable
from types import MethodType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .wx_harness import load_mainwindow, wx_stubs

_PACKAGE = "mainwindow_empty_zone_tests"


@pytest.fixture
def mainwindow_module():
    """Provide an isolated mainwindow module and its wx stub."""
    module = load_mainwindow(
        _PACKAGE,
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            BeginBusyCursor=MagicMock(),
            EndBusyCursor=MagicMock(),
            IsBusy=MagicMock(return_value=True),
            MessageBox=MagicMock(),
            MessageDialog=MagicMock(),
        ),
    )
    return module, module.wx


def _make_window(
    runtime: Any,
    empty_pours: list[str],
    fill_zones: Optional[bool] = None,  # noqa: UP045
) -> tuple[SimpleNamespace, list[str]]:
    """Build the smallest object needed by generate_fabrication_data()."""
    fabrication = SimpleNamespace(
        begin_ordinary_generation=MagicMock(return_value=SimpleNamespace(cpl_rows=())),
        end_ordinary_generation=MagicMock(),
        validate_generation=MagicMock(),
        get_part_consistency_warnings=MagicMock(return_value=""),
        fill_zones=MagicMock(return_value=empty_pours),
        generate_geber=MagicMock(),
        generate_excellon=MagicMock(),
        zip_gerber_excellon=MagicMock(),
        prepare_cpl=MagicMock(return_value=()),
        write_cpl=MagicMock(),
        generate_bom=MagicMock(),
    )
    settings = {"general": {}, "gerber": {}}
    if fill_zones is not None:
        settings["gerber"]["fill_zones"] = fill_zones

    window = SimpleNamespace(
        _project_storage_unavailable=False,
        _get_current_board=MagicMock(return_value=SimpleNamespace()),
        generate_button=MagicMock(),
        reset_gauge=MagicMock(),
        settings=settings,
        fabrication=fabrication,
        pcbnew=SimpleNamespace(GetBoard=MagicMock(return_value=object())),
        logger=MagicMock(),
        run_drc_before_gerber_export=MagicMock(return_value=True),
        layer_selection=MagicMock(),
        count_order_number_placeholders=MagicMock(return_value=0),
        store=MagicMock(),
        build_generate_hook_env=MagicMock(return_value={}),
        run_generate_hook=MagicMock(return_value=True),
        report_generation_step=MagicMock(),
        library=SimpleNamespace(
            read_correction_data=MagicMock(return_value=SimpleNamespace(corrections=()))
        ),
    )
    window.read_valid_corrections_for_generation = (
        lambda: window.library.read_correction_data().corrections
    )
    window.layer_selection.GetSelection.return_value = 0
    window.layer_selection.GetString.return_value = "Auto"
    window.store.get_generation_count.return_value = 0
    window.store.increment_generation_count.return_value = 1

    generation_steps = []

    def run_generation_step(
        description: str, function: Callable[..., Any], *args: Any
    ) -> Any:
        window._current_generation_step = description
        generation_steps.append(description)
        return function(*args)

    window.run_generation_step = run_generation_step
    window.prepare_copper_zones = MethodType(
        runtime.JLCPCBTools.prepare_copper_zones, window
    )
    return window, generation_steps


def _set_dialog_result(wx, result):
    """Configure and return the fake empty-zone dialog."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = result
    wx.MessageDialog.return_value = dialog
    return dialog


def _warning_text(logger):
    """Render lazy logger arguments into searchable warning text."""
    messages = []
    for logged in logger.warning.call_args_list:
        message, *args = logged.args
        messages.append(message % tuple(args) if args else message)
    return "\n".join(messages)


def test_no_empty_zones_skips_warning_and_completes_generation(
    mainwindow_module: Any,
) -> None:
    """A board without empty fills proceeds without creating a dialog."""
    mainwindow, wx = mainwindow_module
    window, steps = _make_window(mainwindow, [])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert "Filling copper zones" in steps
    window.fabrication.fill_zones.assert_called_once_with()
    wx.MessageDialog.assert_not_called()
    window.logger.warning.assert_not_called()
    window.run_drc_before_gerber_export.assert_called_once_with()
    window.fabrication.generate_geber.assert_called_once_with(None)
    window.generate_button.Enable.assert_any_call(False)
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize(
    ("fill_zones", "expected_step"),
    [
        (None, "Filling copper zones"),
        (False, "Checking copper zone fills"),
    ],
)
def test_continue_logs_all_zones_and_uses_refill_aware_wording(
    mainwindow_module: Any,
    fill_zones: Optional[bool],  # noqa: UP045
    expected_step: str,
) -> None:
    """The warning is neutral, complete, and records the continue decision."""
    mainwindow, wx = mainwindow_module
    empty_pours = ["GND on F.Cu", "VCC on In1.Cu"]
    window, steps = _make_window(mainwindow, empty_pours, fill_zones=fill_zones)
    dialog = _set_dialog_result(wx, wx.ID_YES)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert expected_step in steps
    message = wx.MessageDialog.call_args.args[1]
    assert "contain no filled copper" in message
    assert "poured" not in message.lower()
    assert all(pour in message for pour in empty_pours)
    style = wx.MessageDialog.call_args.args[3]
    assert style & wx.NO_DEFAULT
    assert style & wx.ICON_WARNING
    warnings = _warning_text(window.logger)
    assert all(pour in warnings for pour in empty_pours)
    assert "chose to continue export" in warnings
    dialog.SetYesNoLabels.assert_called_once_with("Continue Anyway", "Cancel Export")
    dialog.Destroy.assert_called_once_with()
    window.run_drc_before_gerber_export.assert_called_once_with()
    window.fabrication.generate_geber.assert_called_once_with(None)


@pytest.mark.parametrize("result_name", ["ID_NO", "ID_CANCEL", "unexpected"])
def test_non_affirmative_dialog_results_stop_export(
    mainwindow_module: Any,
    result_name: str,
) -> None:
    """Cancel, close, and unexpected modal results all fail closed."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    window, _ = _make_window(mainwindow, ["GND on F.Cu"])
    dialog = _set_dialog_result(wx, result)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    window.run_drc_before_gerber_export.assert_not_called()
    window.fabrication.generate_geber.assert_not_called()
    warnings = _warning_text(window.logger)
    assert "GND on F.Cu" in warnings
    assert "chose to stop export" in warnings
    window.report_generation_step.assert_any_call(
        "Export stopped by empty copper zones"
    )
    dialog.Destroy.assert_called_once_with()
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize("failing_method", ["SetYesNoLabels", "ShowModal"])
def test_dialog_is_destroyed_when_setup_or_display_raises(
    mainwindow_module: Any,
    failing_method: str,
) -> None:
    """A dialog exception still releases it and restores UI state."""
    mainwindow, wx = mainwindow_module
    window, _ = _make_window(mainwindow, ["GND on F.Cu"])
    dialog = MagicMock()
    getattr(dialog, failing_method).side_effect = RuntimeError("dialog failed")
    wx.MessageDialog.return_value = dialog

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    dialog.Destroy.assert_called_once_with()
    window.run_drc_before_gerber_export.assert_not_called()
    window.logger.exception.assert_called_once()
    wx.MessageBox.assert_called_once()
    assert "dialog failed" in wx.MessageBox.call_args.args[0]
    wx.EndBusyCursor.assert_called_once_with()
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize("fill_zones", [True, False])
def test_review_preparation_reuses_fill_without_generating_outputs(
    mainwindow_module: Any, fill_zones: bool
) -> None:
    """Review runs the existing operation on the active board, not export work."""
    mainwindow, wx = mainwindow_module
    window, steps = _make_window(mainwindow, [], fill_zones=fill_zones)

    assert window.prepare_copper_zones(for_review=True) is True

    window.fabrication.fill_zones.assert_called_once_with(window.pcbnew.GetBoard())
    assert steps == [
        "Filling copper zones" if fill_zones else "Checking copper zone fills"
    ]
    wx.MessageDialog.assert_not_called()
    window.run_drc_before_gerber_export.assert_not_called()
    window.run_generate_hook.assert_not_called()
    for name in (
        "generate_geber",
        "generate_excellon",
        "zip_gerber_excellon",
        "write_cpl",
        "generate_bom",
    ):
        getattr(window.fabrication, name).assert_not_called()
    assert window.store.mock_calls == []


@pytest.mark.parametrize("for_review", [False, True])
def test_preparation_board_selection_preserves_export_board_ownership(
    mainwindow_module: Any, for_review: bool
) -> None:
    """Export fills its retained board; review explicitly prepares the active board."""
    mainwindow, _wx = mainwindow_module
    window, _steps = _make_window(mainwindow, [])
    active_board = window.pcbnew.GetBoard.return_value
    window.fabrication.board = object()
    assert active_board is not window.fabrication.board

    assert window.prepare_copper_zones(for_review=for_review) is True

    if for_review:
        window.fabrication.fill_zones.assert_called_once_with(active_board)
        window.pcbnew.GetBoard.assert_called_once_with()
    else:
        window.fabrication.fill_zones.assert_called_once_with()
        window.pcbnew.GetBoard.assert_not_called()


@pytest.mark.parametrize("result_name", ["ID_YES", "ID_NO", "ID_CANCEL", "unexpected"])
def test_review_empty_zone_warning_is_explicit_and_fails_closed(
    mainwindow_module: Any, result_name: str
) -> None:
    """Only Continue Anyway proceeds with missing copper in review previews."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    window, _steps = _make_window(mainwindow, ["GND on F.Cu", "VCC on In1.Cu"])
    dialog = _set_dialog_result(wx, result)

    assert window.prepare_copper_zones(for_review=True) is (result == wx.ID_YES)

    message = wx.MessageDialog.call_args.args[1]
    assert "GND on F.Cu" in message
    assert "VCC on In1.Cu" in message
    assert "preview" in message.lower()
    assert "without that copper" in message.lower()
    assert "ships" not in message.lower()
    assert wx.MessageDialog.call_args.args[3] & wx.NO_DEFAULT
    dialog.SetYesNoLabels.assert_called_once_with("Continue Anyway", "Cancel Review")
    dialog.Destroy.assert_called_once_with()
    decision = "continue" if result == wx.ID_YES else "stop"
    assert f"chose to {decision} review" in _warning_text(window.logger)


@pytest.mark.parametrize("failing_method", ["SetYesNoLabels", "ShowModal"])
def test_review_warning_errors_propagate_and_destroy_dialog(
    mainwindow_module: Any, failing_method: str
) -> None:
    """The caller handles errors, while shared preparation owns dialog cleanup."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, ["GND on F.Cu"])
    dialog = _set_dialog_result(wx, wx.ID_YES)
    getattr(dialog, failing_method).side_effect = RuntimeError("dialog failed")

    with pytest.raises(RuntimeError, match="dialog failed"):
        window.prepare_copper_zones(for_review=True)

    dialog.Destroy.assert_called_once_with()


def test_review_fill_error_propagates_without_showing_empty_zone_warning(
    mainwindow_module: Any,
) -> None:
    """A failed fill cannot be converted into permission to review stale copper."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, [])
    window.fabrication.fill_zones.side_effect = RuntimeError("fill failed")

    with pytest.raises(RuntimeError, match="fill failed"):
        window.prepare_copper_zones(for_review=True)

    wx.MessageDialog.assert_not_called()
