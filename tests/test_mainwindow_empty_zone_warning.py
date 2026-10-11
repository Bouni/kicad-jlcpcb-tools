"""Tests for empty-zone findings during fabrication-data generation."""

from collections.abc import Callable
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .modal_test_support import review_pane
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
    mainwindow: ModuleType,
    empty_pours: list[str],
    fill_zones: Optional[bool] = None,  # noqa: UP045
) -> tuple[SimpleNamespace, list[str]]:
    """Build the smallest object needed by generate_fabrication_data().

    The review decision is the window's real method, so a test sees the pane
    its findings open through ``mainwindow.GenerationReviewDialog``.
    """
    fabrication = SimpleNamespace(
        begin_ordinary_generation=MagicMock(return_value=SimpleNamespace(cpl_rows=())),
        end_ordinary_generation=MagicMock(),
        validate_generation=MagicMock(),
        get_part_consistency_warnings=MagicMock(return_value={}),
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
        logger=MagicMock(),
        drc_review_findings=MagicMock(return_value=[]),
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
    window.review_generation_findings = MethodType(
        mainwindow.JLCPCBTools.review_generation_findings, window
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
    return window, generation_steps


def _install_pane(mainwindow: ModuleType, monkeypatch: pytest.MonkeyPatch, result):
    """Replace the review pane with a double that closes with ``result``."""
    pane = review_pane(result)
    monkeypatch.setattr(mainwindow, "GenerationReviewDialog", pane)
    return pane


def _warning_text(logger):
    """Render lazy logger arguments into searchable warning text."""
    messages = []
    for logged in logger.warning.call_args_list:
        message, *args = logged.args
        messages.append(message % tuple(args) if args else message)
    return "\n".join(messages)


def test_no_empty_zones_generates_without_a_pane(mainwindow_module, monkeypatch):
    """A board without empty fills proceeds without opening the review pane."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_CANCEL)
    window, steps = _make_window(mainwindow, [])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert "Filling copper zones" in steps
    assert pane.instances == []
    wx.MessageDialog.assert_not_called()
    window.logger.warning.assert_not_called()
    window.drc_review_findings.assert_called_once_with()
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
def test_generate_anyway_logs_all_zones_and_uses_refill_aware_wording(
    mainwindow_module,
    monkeypatch,
    fill_zones,
    expected_step,
):
    """Every empty zone is a red finding; Generate anyway is logged and exports."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_OK)
    empty_pours = ["GND on F.Cu", "VCC on In1.Cu"]
    window, steps = _make_window(mainwindow, empty_pours, fill_zones=fill_zones)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert expected_step in steps
    (shown,) = pane.instances
    assert shown.parent is window
    assert list(shown.findings) == mainwindow.empty_zone_findings(empty_pours)
    assert [(f.severity.name, f.title) for f in shown.findings] == [
        ("ERROR", "Copper zone GND on F.Cu has no fill"),
        ("ERROR", "Copper zone VCC on In1.Cu has no fill"),
    ]
    assert shown.lifecycle == ["show", "end modal", "exit hook", "destroy"]
    warnings = _warning_text(window.logger)
    assert all(pour in warnings for pour in empty_pours)
    assert "user chose Generate anyway" in warnings
    wx.MessageDialog.assert_not_called()
    window.drc_review_findings.assert_called_once_with()
    window.fabrication.generate_geber.assert_called_once_with(None)


@pytest.mark.parametrize("result_name", ["ID_CANCEL", "unexpected"])
def test_anything_but_generate_anyway_stops_export(
    mainwindow_module,
    monkeypatch,
    result_name,
):
    """Cancel, Escape, the close box and unexpected results all fail closed."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    pane = _install_pane(mainwindow, monkeypatch, result)
    window, _ = _make_window(mainwindow, ["GND on F.Cu"])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    # Every check runs before the one decision, DRC included.
    window.drc_review_findings.assert_called_once_with()
    window.fabrication.generate_geber.assert_not_called()
    window.count_order_number_placeholders.assert_not_called()
    window.build_generate_hook_env.assert_not_called()
    window.run_generate_hook.assert_not_called()
    window.store.increment_generation_count.assert_not_called()
    warnings = _warning_text(window.logger)
    assert "GND on F.Cu" in warnings
    assert "user chose Cancel" in warnings
    window.report_generation_step.assert_any_call("Cancelled at review")
    assert pane.instances[0].destroyed
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize("failing", ["construct", "show"])
def test_pane_failure_reports_the_error_and_restores_the_window(
    mainwindow_module,
    monkeypatch,
    failing,
):
    """A pane that fails is still destroyed, and the UI state is restored."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_OK)
    if failing == "construct":
        monkeypatch.setattr(
            mainwindow,
            "GenerationReviewDialog",
            MagicMock(side_effect=RuntimeError("pane failed")),
        )
    else:
        pane.error = RuntimeError("pane failed")
    window, _ = _make_window(mainwindow, ["GND on F.Cu"])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert all(shown.destroyed for shown in pane.instances)
    window.fabrication.generate_geber.assert_not_called()
    window.logger.exception.assert_called_once()
    wx.MessageBox.assert_called_once()
    assert "pane failed" in wx.MessageBox.call_args.args[0]
    assert wx.BeginBusyCursor.call_count == wx.EndBusyCursor.call_count == 2
    window.generate_button.Enable.assert_any_call(True)
