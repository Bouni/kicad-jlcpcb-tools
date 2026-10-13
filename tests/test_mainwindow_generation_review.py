"""Generation runs every check, then asks once, in one pane, when anything needs it."""

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .modal_test_support import review_pane
from .test_mainwindow_empty_zone_warning import _make_window, mainwindow_module

__all__ = ["mainwindow_module"]

DRC_MESSAGES = [
    "[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; "
    "actual 0.1500 mm)",
    "[courtyards_overlap]: Courtyards overlap",
]


def _install_pane(mainwindow: Any, monkeypatch: pytest.MonkeyPatch, result: int):
    """Replace the review pane with a double that closes with ``result``."""
    pane = review_pane(result)
    monkeypatch.setattr(mainwindow, "GenerationReviewDialog", pane)
    return pane


def _bind(mainwindow: Any, window: SimpleNamespace, name: str) -> None:
    """Use the real window method ``name`` on the test window."""
    setattr(window, name, MethodType(getattr(mainwindow.JLCPCBTools, name), window))


def test_clean_board_generates_straight_through_without_extra_work(
    mainwindow_module, monkeypatch
):
    """No findings, no pane; checks that are off do no work and add nothing."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_CANCEL)
    window, steps = _make_window(mainwindow, [])
    window.settings["gerber"]["force_drc"] = False
    window.save_board_for_drc = MagicMock()
    counter = MagicMock()
    monkeypatch.setattr(mainwindow, "DRCViolationCounter", counter)
    _bind(mainwindow, window, "drc_review_findings")

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert pane.instances == []
    assert steps == [
        "Validating corrections",
        "Preparing placement data",
        "Checking part consistency",
        "Filling copper zones",
        "Running pre-export DRC check",
        "Counting order/serial placeholders",
        "Plotting Gerbers",
        "Generating Excellon drill/map files",
        "Creating Gerber archive (.zip)",
        "Generating placement file (CPL)",
        "Generating BOM",
    ]
    window.save_board_for_drc.assert_not_called()
    counter.assert_not_called()
    window.count_order_number_placeholders.assert_called_once_with()
    window.logger.warning.assert_not_called()
    window.store.increment_generation_count.assert_called_once_with()
    wx.MessageBox.assert_not_called()
    wx.MessageDialog.assert_not_called()


def test_info_alone_shows_no_pane_and_is_logged(mainwindow_module, monkeypatch):
    """An info finding never asks for a decision, but it always reaches the log."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_CANCEL)
    window, _ = _make_window(mainwindow, [])
    # The findings module mainwindow imported, reached through one of its functions.
    severity = mainwindow.needs_review.__globals__["Severity"]
    info = mainwindow.Finding(
        severity.INFO, "JLC footprint", "Q7: JLC's pin-1 mark sits on another pin"
    )
    window.drc_review_findings.return_value = [info]

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert pane.instances == []
    window.logger.info.assert_any_call(
        "%s: %s", "JLC footprint", "Q7: JLC's pin-1 mark sits on another pin"
    )
    window.logger.warning.assert_not_called()
    window.store.increment_generation_count.assert_called_once_with()


def test_findings_from_every_check_arrive_in_one_pane(mainwindow_module, monkeypatch):
    """Four checks find something; one pane shows it all, in the order they ran."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_CANCEL)
    window, steps = _make_window(mainwindow, ["GND on B.Cu"])
    window.settings["general"]["order_number"] = True
    window.fabrication.get_part_consistency_warnings.return_value = {
        "C14663": {"100n": ["C3", "C4"], "0.1uF": ["C7"]}
    }
    window.drc_review_findings.return_value = mainwindow.drc_findings(2, DRC_MESSAGES)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    (shown,) = pane.instances
    assert [(f.severity.name, f.check, f.title) for f in shown.findings] == [
        ("WARNING", "Part values", "C14663: 100n on C3, C4; 0.1uF on C7"),
        (
            "WARNING",
            "Order number",
            "No order-number placeholder (JLCJLCJLCJLC) on silkscreen",
        ),
        ("ERROR", "Copper zones", "Copper zone GND on B.Cu has no fill"),
        ("ERROR", "DRC", "DRC found 2 error violations"),
    ]
    assert shown.findings[0].references == ("C3", "C4", "C7")
    assert shown.lifecycle == ["show", "end modal", "exit hook", "destroy"]
    assert steps[-1] == "Running pre-export DRC check"
    window.report_generation_step.assert_any_call("Cancelled at review")
    wx.MessageBox.assert_not_called()
    wx.MessageDialog.assert_not_called()
    window.fabrication.generate_geber.assert_not_called()


def test_order_number_count_is_checked_once_and_reused_for_the_hooks(
    mainwindow_module, monkeypatch
):
    """With the setting on, the reviewed count is the one the hooks receive."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_OK)
    window, steps = _make_window(mainwindow, [])
    window.settings["general"]["order_number"] = True
    window.count_order_number_placeholders.return_value = 2

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert [f.title for f in pane.instances[0].findings] == [
        "2 order-number placeholders on silkscreen"
    ]
    window.count_order_number_placeholders.assert_called_once_with()
    assert "Checking order/serial placeholders" in steps
    assert "Counting order/serial placeholders" not in steps
    assert [
        c.kwargs["placeholder_count"]
        for c in window.build_generate_hook_env.call_args_list
    ] == [2, 2]
    window.store.increment_generation_count.assert_called_once_with()


def test_a_pane_that_fails_to_open_is_reported_as_the_review(
    mainwindow_module, monkeypatch
):
    """The error box names the review, and the findings are already logged."""
    mainwindow, wx = mainwindow_module

    def broken_pane(*_args: Any) -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(mainwindow, "GenerationReviewDialog", broken_pane)
    window, steps = _make_window(mainwindow, ["GND on B.Cu"])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert steps[-1] == "Running pre-export DRC check"
    message = wx.MessageBox.call_args.args[0]
    assert message.startswith(
        "Fabrication data generation failed during: Reviewing findings"
    )
    window.fabrication.generate_geber.assert_not_called()
    window.logger.warning.assert_any_call(
        "%s: %s", "Copper zones", "Copper zone GND on B.Cu has no fill"
    )


@pytest.mark.parametrize("busy", [True, False])
def test_busy_cursor_is_lifted_only_while_the_pane_is_open(
    mainwindow_module, monkeypatch, busy
):
    """The interactive pane gets the ordinary cursor; generation keeps the busy one."""
    mainwindow, wx = mainwindow_module
    pane = _install_pane(mainwindow, monkeypatch, wx.ID_OK)
    window, _ = _make_window(mainwindow, ["GND on B.Cu"])
    events: list[str] = []
    monkeypatch.setattr(wx, "IsBusy", lambda: busy)
    monkeypatch.setattr(wx, "BeginBusyCursor", lambda: events.append("busy"))
    monkeypatch.setattr(wx, "EndBusyCursor", lambda: events.append("ordinary"))
    show = pane.ShowModal
    monkeypatch.setattr(
        pane, "ShowModal", lambda dialog: events.append("pane") or show(dialog)
    )

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    if busy:
        assert events == ["busy", "ordinary", "pane", "busy", "ordinary"]
    else:
        assert events == ["busy", "pane"]


def _drc_window(mainwindow: Any, monkeypatch: pytest.MonkeyPatch, filename: str):
    """Bind the real forced-DRC findings method to a window with DRC on."""
    window, _ = _make_window(mainwindow, [])
    window.settings["gerber"]["force_drc"] = True
    window._get_current_board.return_value = SimpleNamespace(
        GetFileName=lambda: filename
    )
    window.save_board_for_drc = MagicMock()
    window.flush_generation_ui = MagicMock()
    window.pcbnew = object()
    window.project_path = "/project"
    counter = MagicMock()
    monkeypatch.setattr(mainwindow, "DRCViolationCounter", counter)
    _bind(mainwindow, window, "drc_review_findings")
    return window, counter


def test_drc_errors_become_one_red_finding_with_their_messages(
    mainwindow_module, monkeypatch
):
    """The board is saved, DRC runs once, and its messages reach the finding."""
    mainwindow, wx = mainwindow_module
    window, counter = _drc_window(mainwindow, monkeypatch, "/project/board.kicad_pcb")
    counter.return_value.get_error_violations.return_value = (2, DRC_MESSAGES)

    findings = window.drc_review_findings()

    assert findings == mainwindow.drc_findings(2, DRC_MESSAGES)
    window.save_board_for_drc.assert_called_once_with()
    counter.assert_called_once_with(pcbnew_module=window.pcbnew, working_dir="/project")
    counter.return_value.get_error_violations.assert_called_once_with(
        "/project/board.kicad_pcb"
    )
    counter.return_value.get_error_violations.return_value = (0, [])
    assert window.drc_review_findings() == []
    wx.MessageBox.assert_not_called()


@pytest.mark.parametrize("api", ["Save", "SaveBoard"])
@pytest.mark.parametrize("saved", [False, True])
def test_a_refused_save_is_drc_that_couldnt_run(
    mainwindow_module, monkeypatch, api, saved
):
    """KiCad can refuse a save without raising; DRC must not check the old file."""
    mainwindow, _wx = mainwindow_module
    filename = "/project/board.kicad_pcb"
    window, counter = _drc_window(mainwindow, monkeypatch, filename)
    save = MagicMock(return_value=saved)
    if api == "Save":
        board = SimpleNamespace(GetFileName=lambda: filename, Save=save)
    else:
        board = SimpleNamespace(GetFileName=lambda: filename)
        window.pcbnew = SimpleNamespace(SaveBoard=save)
    window._get_current_board.return_value = board
    counter.return_value.get_error_violations.return_value = (0, [])
    _bind(mainwindow, window, "save_board_for_drc")

    findings = window.drc_review_findings()

    save.assert_called_once()
    if saved:
        assert findings == []
        counter.assert_called_once()
    else:
        assert [finding.title for finding in findings] == [
            f"DRC couldn't run: saving the board failed: KiCad did not save {filename}"
        ]
        counter.assert_not_called()


@pytest.mark.parametrize("failure", ["unsaved", "save", "crash"])
def test_each_way_drc_fails_to_run_is_a_red_finding(
    mainwindow_module, monkeypatch, failure
):
    """An unsaved board, a failed save and a DRC crash no longer stop generation."""
    mainwindow, wx = mainwindow_module
    filename = "" if failure == "unsaved" else "/project/board.kicad_pcb"
    window, counter = _drc_window(mainwindow, monkeypatch, filename)
    if failure == "save":
        window.save_board_for_drc.side_effect = OSError("board.kicad_pcb is read-only")
    counter.return_value.get_error_violations.side_effect = RuntimeError(
        "WriteDRCReport returned failure"
    )

    (finding,) = window.drc_review_findings()

    assert (finding.severity.name, finding.check) == ("ERROR", "DRC")
    assert (
        finding.title
        == {
            "unsaved": "DRC couldn't run: the board must be saved first",
            "save": "DRC couldn't run: saving the board failed: "
            "board.kicad_pcb is read-only",
            "crash": "DRC couldn't run: WriteDRCReport returned failure",
        }[failure]
    )
    assert window.save_board_for_drc.call_count == (failure != "unsaved")
    assert counter.call_count == (failure == "crash")
    assert window.logger.exception.call_count == (failure == "crash")
    wx.MessageBox.assert_not_called()
