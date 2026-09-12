"""Tests for empty-zone warnings during fabrication-data generation."""

from types import SimpleNamespace
from typing import Optional
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
    empty_pours: list[str],
    fill_zones: Optional[bool] = None,  # noqa: UP045
) -> tuple[SimpleNamespace, list[str]]:
    """Build the smallest object needed by generate_fabrication_data()."""
    fabrication = SimpleNamespace(
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
        generate_button=MagicMock(),
        reset_gauge=MagicMock(),
        settings=settings,
        fabrication=fabrication,
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

    def run_generation_step(description, function, *args):
        window._current_generation_step = description
        generation_steps.append(description)
        return function(*args)

    window.run_generation_step = run_generation_step
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


def test_no_empty_zones_skips_warning_and_completes_generation(mainwindow_module):
    """A board without empty fills proceeds without creating a dialog."""
    mainwindow, wx = mainwindow_module
    window, steps = _make_window([])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert "Filling copper zones" in steps
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
    mainwindow_module,
    fill_zones,
    expected_step,
):
    """The warning is neutral, complete, and records the continue decision."""
    mainwindow, wx = mainwindow_module
    empty_pours = ["GND on F.Cu", "VCC on In1.Cu"]
    window, steps = _make_window(empty_pours, fill_zones=fill_zones)
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
    mainwindow_module,
    result_name,
):
    """Cancel, close, and unexpected modal results all fail closed."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    window, _ = _make_window(["GND on F.Cu"])
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
    mainwindow_module,
    failing_method,
):
    """A dialog exception still releases it and restores UI state."""
    mainwindow, wx = mainwindow_module
    window, _ = _make_window(["GND on F.Cu"])
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
