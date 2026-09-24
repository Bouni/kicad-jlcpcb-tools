"""Tests for empty-zone findings during fabrication-data generation."""

from collections.abc import Callable, Iterator
import logging
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from copper_zones import EmptyCopperZone

from .modal_test_support import ModalDialog, review_pane
from .wx_harness import load_siblings, mainwindow_stubs, wx_stubs

_PACKAGE = "mainwindow_empty_zone_tests"


@pytest.fixture
def mainwindow_module() -> Iterator[tuple[ModuleType, Any]]:
    """Provide an isolated mainwindow module and its wx stub."""
    replacements = mainwindow_stubs(
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
        copper_zone_dialog={"EmptyCopperZonesDialog": MagicMock()},
    )
    # Reports import vendored dependencies lazily, as they do while the plugin
    # package remains loaded. Scope the complete alias subtree to this fixture.
    levels = {name: logging.getLogger(name).level for name in ("requests", "urllib3")}
    try:
        with load_siblings(_PACKAGE, ("mainwindow",), replacements) as loaded:
            module = loaded["mainwindow"]
            yield module, module.wx
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


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
        fill_zone_regions=MagicMock(
            return_value=[
                EmptyCopperZone(
                    str(index),
                    pour.split(" on ")[0],
                    index,
                    pour.split(" on ")[1],
                    (((index * 1000000, 0), (10000000, 0), (10000000, 5000000)),),
                )
                for index, pour in enumerate(empty_pours)
            ]
        ),
        board=MagicMock(name="retained_board"),
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
        _legacy_board=MagicMock(return_value=SimpleNamespace()),
        generate_button=MagicMock(),
        reset_gauge=MagicMock(),
        settings=settings,
        fabrication=fabrication,
        pcbnew=SimpleNamespace(GetBoard=MagicMock(return_value=object())),
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
    window.prepare_copper_zones = MethodType(
        mainwindow.JLCPCBTools.prepare_copper_zones, window
    )
    window._impedance_plan_for_generation = MethodType(
        mainwindow.JLCPCBTools._impedance_plan_for_generation, window
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


def _install_pane(
    mainwindow: ModuleType, monkeypatch: pytest.MonkeyPatch, result: int
) -> type[ModalDialog]:
    """Replace the review pane with a double that closes with ``result``."""
    pane = review_pane(result)
    monkeypatch.setattr(mainwindow, "GenerationReviewDialog", pane)
    return pane


def _warning_text(logger: MagicMock) -> str:
    """Render lazy logger arguments into searchable warning text."""
    messages = []
    for logged in logger.warning.call_args_list:
        message, *args = logged.args
        messages.append(message % tuple(args) if args else message)
    return "\n".join(messages)


def test_no_empty_zones_generates_without_a_pane(
    mainwindow_module: tuple[ModuleType, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board without empty fills proceeds without opening the review pane."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_CANCEL)
    window, steps = _make_window(mainwindow, [])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert "Filling copper zones" in steps
    window.fabrication.fill_zones.assert_called_once_with()
    window.pcbnew.GetBoard.assert_not_called()
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
    mainwindow_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    fill_zones: Optional[bool],  # noqa: UP045
    expected_step: str,
) -> None:
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
    mainwindow_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    result_name: str,
) -> None:
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
    mainwindow_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
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


def _set_dialog_result(mainwindow: Any, result: int) -> MagicMock:
    """Configure and return the fake empty-zone dialog."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = result
    mainwindow.EmptyCopperZonesDialog.return_value = dialog
    return dialog


@pytest.mark.parametrize("fill_zones", [True, False])
def test_review_preparation_reuses_fill_without_generating_outputs(
    mainwindow_module: Any, fill_zones: bool
) -> None:
    """Review runs the existing operation on the active board, not export work."""
    mainwindow, wx = mainwindow_module
    window, steps = _make_window(mainwindow, [], fill_zones=fill_zones)

    assert window.prepare_copper_zones() is True

    window.fabrication.fill_zone_regions.assert_called_once_with(
        window.pcbnew.GetBoard()
    )
    window.fabrication.fill_zones.assert_not_called()
    assert steps == [
        "Filling copper zones" if fill_zones else "Checking copper zone fills"
    ]
    wx.MessageDialog.assert_not_called()
    mainwindow.EmptyCopperZonesDialog.assert_not_called()
    window.drc_review_findings.assert_not_called()
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
    window.reset_gauge.assert_called_once_with()


def test_standalone_review_prepares_the_active_board(
    mainwindow_module: Any,
) -> None:
    """Standalone review uses current editor copper, independent of retained exports."""
    mainwindow, _wx = mainwindow_module
    window, _steps = _make_window(mainwindow, [])
    active_board = window.pcbnew.GetBoard.return_value
    window.fabrication.board = object()
    assert active_board is not window.fabrication.board
    assert window.prepare_copper_zones() is True
    window.fabrication.fill_zone_regions.assert_called_once_with(active_board)
    window.pcbnew.GetBoard.assert_called_once_with()
    window.reset_gauge.assert_called_once_with()


@pytest.mark.parametrize("result_name", ["ID_YES", "ID_NO", "ID_CANCEL", "unexpected"])
def test_review_empty_zone_warning_is_explicit_and_fails_closed(
    mainwindow_module: Any, result_name: str
) -> None:
    """Only Continue Anyway proceeds with missing copper in review previews."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    window, _steps = _make_window(mainwindow, ["GND on F.Cu", "VCC on In1.Cu"])
    dialog = _set_dialog_result(mainwindow, result)

    assert window.prepare_copper_zones() is (result == wx.ID_YES)

    args = mainwindow.EmptyCopperZonesDialog.call_args.args
    assert args[:3] == (window, window.pcbnew.GetBoard.return_value, window.pcbnew)
    assert args[3] is window.fabrication.fill_zone_regions.return_value
    assert [region.label for region in args[3]] == ["GND on F.Cu", "VCC on In1.Cu"]
    assert mainwindow.EmptyCopperZonesDialog.call_args.kwargs == {"for_review": True}
    wx.MessageDialog.assert_not_called()
    dialog.Destroy.assert_called_once_with()
    decision = "continue" if result == wx.ID_YES else "stop"
    assert f"chose to {decision} review" in _warning_text(window.logger)
    window.reset_gauge.assert_called_once_with()


@pytest.mark.parametrize("failing_method", ["ShowModal"])
def test_review_warning_errors_propagate_and_destroy_dialog(
    mainwindow_module: Any, failing_method: str
) -> None:
    """The caller handles errors, while shared preparation owns dialog cleanup."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, ["GND on F.Cu"])
    dialog = _set_dialog_result(mainwindow, wx.ID_YES)
    getattr(dialog, failing_method).side_effect = RuntimeError("dialog failed")

    with pytest.raises(RuntimeError, match="dialog failed"):
        window.prepare_copper_zones()

    dialog.Destroy.assert_called_once_with()
    window.reset_gauge.assert_called_once_with()


def test_review_fill_error_propagates_without_showing_empty_zone_warning(
    mainwindow_module: Any,
) -> None:
    """A failed fill cannot be converted into permission to review stale copper."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, [])
    window.fabrication.fill_zone_regions.side_effect = RuntimeError("fill failed")

    with pytest.raises(RuntimeError, match="fill failed"):
        window.prepare_copper_zones()

    wx.MessageDialog.assert_not_called()
    mainwindow.EmptyCopperZonesDialog.assert_not_called()
    window.reset_gauge.assert_called_once_with()


def test_review_preserves_distinct_locations_and_refreshes_when_reopened(
    mainwindow_module: Any,
) -> None:
    """Same-net zones stay selectable, and reopening uses the latest fill result."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, [])
    first = EmptyCopperZone(
        "left", "VIN", 0, "F.Cu", (((0, 0), (1000000, 0), (0, 1000000)),)
    )
    second = EmptyCopperZone(
        "right",
        "VIN",
        0,
        "F.Cu",
        (((20000000, 0), (21000000, 0), (20000000, 1000000)),),
    )
    window.fabrication.fill_zone_regions.side_effect = [[first, second], [second], []]
    dialog = _set_dialog_result(mainwindow, wx.ID_YES)

    assert window.prepare_copper_zones()
    assert window.prepare_copper_zones()
    assert window.prepare_copper_zones()

    calls = mainwindow.EmptyCopperZonesDialog.call_args_list
    assert [call.args[3] for call in calls] == [[first, second], [second]]
    assert first.label == second.label
    assert first.bounds != second.bounds
    assert dialog.Destroy.call_count == 2
    assert window.fabrication.fill_zone_regions.call_count == 3
    assert window.reset_gauge.call_count == 3
    assert window.store.mock_calls == []


def test_visual_warning_construction_failure_restores_gauge(
    mainwindow_module: Any,
) -> None:
    """An unavailable dialog cannot silently authorize reviewing missing copper."""
    mainwindow, wx = mainwindow_module
    window, _steps = _make_window(mainwindow, ["VIN on F.Cu"])
    mainwindow.EmptyCopperZonesDialog.side_effect = RuntimeError("dialog failed")

    with pytest.raises(RuntimeError, match="dialog failed"):
        window.prepare_copper_zones()

    window.reset_gauge.assert_called_once_with()
    wx.MessageDialog.assert_not_called()
