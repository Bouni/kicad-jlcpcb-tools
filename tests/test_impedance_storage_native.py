"""Exercise real wx storage-recovery controls; PCB identity is a deterministic fixture."""

import importlib
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, run_native

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


@pytest.mark.parametrize(
    ("action", "allow_omit", "expected"),
    [
        ("default", True, "cancel"),
        ("retry", True, "retry"),
        ("omit", True, "omit"),
        ("close", True, "cancel"),
        ("default", False, "cancel"),
        ("retry", False, "retry"),
    ],
)
def test_native_storage_choice_defaults_to_cancel_and_close_never_omits(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    action: str,
    allow_omit: bool,
    expected: str,
) -> None:
    """Use native modal selection, button handlers and Close after a real schema error."""
    if isolated_native(request, "KICAD_IMPEDANCE_STORAGE_NATIVE_CHILD"):
        return
    package_name = "_impedance_storage_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    api = importlib.import_module(f"{package_name}.integration")
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    database_path = tmp_path / "project.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE boards (wrong TEXT)")
        connection.execute("INSERT INTO boards VALUES ('preserve')")
    previous = database_path.read_bytes()

    def exercise(frame: Any, wx: Any) -> None:
        frame.pcbnew = SimpleNamespace(
            GetBoard=lambda: SimpleNamespace(GetFileName=lambda: str(board_path))
        )
        toolbar = frame.CreateToolBar()
        controls = api.ImpedanceControls(frame, toolbar)
        controls.attach_store(SimpleNamespace(dbfile=database_path))
        toolbar.Realize()
        assert not controls.checkbox.IsEnabled()
        assert controls.configure_button.IsEnabled()
        errors: list[BaseException] = []

        def choose(dialog: Any) -> None:
            try:
                assert dialog.GetSelection() == 0
                choice = dialog.GetChildren()
                assert choice  # Real native child controls were constructed.
                if action in ("omit", "close"):
                    dialog.SetSelection(2)
                elif action == "retry":
                    dialog.SetSelection(1)
                if action == "close":
                    dialog.Close()
                else:
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                    event.SetEventObject(dialog.FindWindow(wx.ID_OK))
                    dialog.GetEventHandler().ProcessEvent(event)
            except BaseException as error:
                errors.append(error)
                dialog.EndModal(wx.ID_CANCEL)

        class NativeChoice(wx.SingleChoiceDialog):
            """Schedule input while retaining the native modal loop and handlers."""

            def ShowModal(self) -> int:
                timer = wx.CallLater(50, choose, self)
                try:
                    return super().ShowModal()
                finally:
                    timer.Stop()

        monkeypatch.setattr(api.wx, "SingleChoiceDialog", NativeChoice)
        result = controls.storage_error_action(
            controls._storage_error, allow_omit=allow_omit
        )
        if errors:
            raise errors[0]
        assert result == expected
        assert database_path.read_bytes() == previous
        assert controls.repository is None
        assert not controls.checkbox.GetValue()

    run_native(exercise)
