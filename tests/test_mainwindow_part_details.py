"""Regression tests for issue #772's duplicate Part Details windows."""

from itertools import count
from typing import Optional
from unittest.mock import Mock, call

import pytest

from .wx_harness import load_mainwindow, wx_stubs

_ids = count(1)
mainwindow = load_mainwindow(
    "mainwindow_part_details_tests",
    wx=wx_stubs(
        Dialog=type("Dialog", (), {}),
        NewIdRef=lambda: next(_ids),
    ),
)
JLCPCBTools = mainwindow.JLCPCBTools


def _window(
    monkeypatch: pytest.MonkeyPatch, codes: list[Optional[str]]
) -> tuple[JLCPCBTools, Mock, list[Mock]]:
    """Capture dialogs while exercising the real handler and display helper."""
    window = object.__new__(JLCPCBTools)
    window.footprint_list = Mock(spec_set=["GetSelections"])
    window.footprint_list.GetSelections.return_value = list(range(len(codes)))
    window.partlist_data_model = Mock(spec_set=["get_lcsc", "get_footprint"])
    window.partlist_data_model.get_lcsc.side_effect = codes.__getitem__
    # Parts sharing a footprint can still have distinct LCSC assignments.
    window.partlist_data_model.get_footprint.return_value = "R_0603_1608Metric"
    dialogs: list[Mock] = []

    def create_dialog(_parent: JLCPCBTools, _part: str) -> Mock:
        dialog = Mock(spec_set=["Show", "ShowModal", "Destroy"])
        dialogs.append(dialog)
        return dialog

    factory = Mock(side_effect=create_dialog)
    monkeypatch.setattr(mainwindow, "PartDetailsDialog", factory)
    return window, factory, dialogs


def _assert_modeless(dialogs: list[Mock]) -> None:
    """Require each dialog to remain open without blocking for dismissal."""
    for dialog in dialogs:
        dialog.Show.assert_called_once_with()
        dialog.ShowModal.assert_not_called()
        dialog.Destroy.assert_not_called()


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (["C25804", "C25804", "C25804"], ["C25804"]),
        (
            ["C25804", "C100", "C25804", "C20", "C100"],
            ["C25804", "C100", "C20"],
        ),
        (["C25804", "C25803"], ["C25804", "C25803"]),
        (["C25804"], ["C25804"]),
        ([None, "", "C25804", "", None], ["C25804"]),
        ([None, ""], []),
        ([], []),
    ],
    ids=[
        "duplicate-id",
        "interleaved-ids-preserve-order",
        "distinct-ids-same-footprint",
        "single-id",
        "skip-unassigned",
        "only-unassigned",
        "empty-selection",
    ],
)
def test_part_details_opens_once_per_selected_lcsc(
    monkeypatch: pytest.MonkeyPatch,
    codes: list[Optional[str]],
    expected: list[str],
) -> None:
    """Open one modeless window per assigned code in first-selection order."""
    window, factory, dialogs = _window(monkeypatch, codes)

    window.get_part_details()

    assert factory.call_args_list == [call(window, code) for code in expected]
    _assert_modeless(dialogs)


def test_part_details_can_reopen_on_a_later_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limit deduplication to the current action so later actions can reopen."""
    window, factory, dialogs = _window(monkeypatch, ["C25804", "C25804"])

    window.get_part_details()
    window.get_part_details()

    assert factory.call_args_list == [call(window, "C25804"), call(window, "C25804")]
    _assert_modeless(dialogs)
