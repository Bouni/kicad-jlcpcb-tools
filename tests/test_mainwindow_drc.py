"""DRC must not read an older on-disk board after a native save fails."""

from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .wx_harness import load_mainwindow, wx_stubs


@pytest.fixture
def mainwindow() -> ModuleType:
    """Load the real controller with native UI boundaries isolated."""
    return load_mainwindow(
        "_mainwindow_drc_tests",
        wx=wx_stubs(
            Frame=type("Frame", (), {}),
            NewIdRef=MagicMock(side_effect=object),
            MessageBox=MagicMock(),
        ),
    )


def make_window(mainwindow: ModuleType, api: str, result: Any) -> Any:
    """Expose each supported save signature through the live board provider."""
    board = SimpleNamespace(GetFileName=lambda: "/project/board.kicad_pcb")
    pcbnew = SimpleNamespace(GetBoard=lambda: board)
    if api == "board":
        board.Save = MagicMock(return_value=result)
    elif api == "board_no_args":

        def save() -> Any:
            return result

        board.Save = save
    else:
        pcbnew.SaveBoard = MagicMock(return_value=result)
    window = object.__new__(mainwindow.JLCPCBTools)
    window.pcbnew = pcbnew
    window.settings = {"gerber": {"force_drc": True}}
    window.project_path = "/project"
    window.flush_generation_ui = MagicMock()
    return window


@pytest.mark.parametrize("api", ["board", "board_no_args", "module"])
def test_false_native_save_stops_drc(
    mainwindow: ModuleType, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    """A False result reports failure before DRC can inspect stale disk contents."""
    window = make_window(mainwindow, api, False)
    counter = MagicMock()
    counter.return_value.get_violation_count.return_value = 0
    monkeypatch.setattr(mainwindow, "DRCViolationCounter", counter)

    assert window.run_drc_before_gerber_export() is False

    counter.assert_not_called()
    message = mainwindow.wx.MessageBox.call_args.args[0]
    assert "Failed to save board" in message


@pytest.mark.parametrize("api", ["board", "board_no_args", "module"])
@pytest.mark.parametrize("result", [True, None])
def test_native_save_success_including_legacy_void_return(
    mainwindow: ModuleType, api: str, result: Any
) -> None:
    """Older void save methods remain supported alongside current bool returns."""
    make_window(mainwindow, api, result).save_board_for_drc()
