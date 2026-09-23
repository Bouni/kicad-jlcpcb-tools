"""Modeless action dispatch with a stateful simulation of KiCad's native wrapper.

These tests model the documented/source-inspected native snapshot, Run, dirty
sequence. They verify our routing contract, not native KiCad execution.
"""

from collections.abc import Callable, Iterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module, package_stubs


class MenuItem:
    """Expose public wx menu label, ID, and submenu accessors."""

    def __init__(self, label: str, item_id: int, submenu: Any = None) -> None:
        self.label, self.item_id, self.submenu = label, item_id, submenu

    def GetItemLabelText(self) -> str:
        """Return the label without accelerators."""
        return self.label

    def GetId(self) -> int:
        """Return the current native command ID."""
        return self.item_id

    def GetSubMenu(self) -> Any:
        """Return the optional nested menu."""
        return self.submenu


class Menu:
    """Keep nested native commands discoverable through wx accessors."""

    def __init__(self, *items: MenuItem) -> None:
        self.items = list(items)

    def GetMenuItems(self) -> list[MenuItem]:
        """Return the current command list."""
        return self.items


class Editor:
    """Model the relevant native state, including finalization after Python Run."""

    def __init__(self) -> None:
        self.menu = Menu(MenuItem("JLCPCB Tools", 71))
        self.bar = SimpleNamespace(
            GetMenus=lambda: [(Menu(MenuItem("Plugins", 70, self.menu)), "Tools")]
        )
        self.board = SimpleNamespace(
            m_Uuid=SimpleNamespace(AsString=lambda: "original-board"),
            this=1024,
            GetFileName=lambda: "original.kicad_pcb",
            fields={"R1": "C100", "R2": "C200"},
        )
        self.dirty = False
        self.deleted = False
        self.enabled = True
        self.running = False
        self.consume_event = True
        self.events: list[int] = []
        self.undo: list[dict[str, str]] = []
        self.redo: list[dict[str, str]] = []
        self.plugin: Any = None
        self.finalized = 0
        self.python_errors: list[BaseException] = []

    def GetMenuBar(self) -> Any:
        """Return the editor's menus."""
        return self.bar

    def IsBeingDeleted(self) -> bool:
        """Report a closing native editor."""
        return self.deleted

    def IsEnabled(self) -> bool:
        """Report whether a modal native dialog has disabled the editor."""
        return self.enabled

    def GetEventHandler(self) -> "Editor":
        """Expose the native menu handler."""
        return self

    def native_run(self) -> None:
        """Simulate KiCad's snapshot and finalization around a Python invocation."""
        previous = deepcopy(self.board.fields)
        self.running = True
        try:
            self.plugin.Run()
        except BaseException as error:
            # PYTHON_ACTION_PLUGIN::CallMethod consumes escaping Python errors.
            self.python_errors.append(error)
        finally:
            self.running = False
            self.undo.append(previous)
            self.redo.clear()
            self.dirty = True
            self.finalized += 1

    def undo_last(self) -> None:
        """Restore the snapshot associated with the last native action."""
        self.redo.append(deepcopy(self.board.fields))
        self.board.fields = self.undo.pop()

    def redo_last(self) -> None:
        """Reapply the complete state reversed by the last undo."""
        self.undo.append(deepcopy(self.board.fields))
        self.board.fields = self.redo.pop()

    def ProcessEvent(self, event: Any) -> bool:
        """Invoke the action only when the native command is handled."""
        self.events.append(event.item_id)
        if not self.consume_event:
            return False
        assert event.item_id == self.menu.items[0].item_id
        self.native_run()
        return True


@pytest.fixture
def harness() -> Iterator[Any]:
    """Load the real action plugin against isolated stateful framework boundaries."""
    editor = Editor()
    windows: list[Any] = []
    state = SimpleNamespace(editor=editor, windows=windows, autofill=None)

    class ActionPlugin:
        def __init__(self) -> None:
            self.defaults()

    def make_window(parent: Any, **kwargs: Any) -> Any:
        window = SimpleNamespace(
            board_action=kwargs.get("board_action", lambda action: action()),
            Center=lambda: None,
            Show=lambda: None,
        )
        windows.append(window)
        if state.autofill:
            window.board_action(state.autofill)
        return window

    package = "_plugin_board_actions_tests"
    stubs = {
        **package_stubs(package),
        "pcbnew": module(
            "pcbnew",
            ActionPlugin=ActionPlugin,
            GetBuildVersion=lambda: "10.0.6",
            GetBoard=lambda: state.editor.board,
            IsActionRunning=lambda: state.editor.running,
        ),
        "wx": module(
            "wx",
            GetTopLevelWindows=lambda: [state.editor],
            EVT_MENU=SimpleNamespace(typeId=9),
            CommandEvent=lambda event_type, item_id: SimpleNamespace(
                event_type=event_type, item_id=item_id
            ),
        ),
        f"{package}.mainwindow": module(
            f"{package}.mainwindow", JLCPCBTools=make_window
        ),
    }
    with load_siblings(package, ("plugin",), stubs) as modules:
        editor.plugin = modules["plugin"].JLCPCBPlugin()
        yield state


def open_window(harness: Any) -> Callable[[Callable[[], None]], None]:
    """Launch through the simulated editor and return the actual callback runner."""
    harness.editor.native_run()
    return harness.windows[-1].board_action


def test_edit_after_save_uses_native_action_without_opening_another_window(
    harness: Any,
) -> None:
    """Edits after saving must start a new native action, keeping the window modeless."""
    action = open_window(harness)
    editor = harness.editor
    editor.dirty = False  # User saves while the modeless plugin remains open.
    action(lambda: editor.board.fields.update(R1="C300"))
    assert editor.board.fields["R1"] == "C300"
    assert editor.dirty
    assert editor.undo[-1]["R1"] == "C100"
    assert editor.events == [71]
    assert len(harness.windows) == 1


def test_constructor_autofill_stays_inside_original_native_action(harness: Any) -> None:
    """Autofill must not nest native action wrappers during initial construction."""
    harness.autofill = lambda: harness.editor.board.fields.update(R1="C400")
    open_window(harness)
    assert harness.editor.board.fields["R1"] == "C400"
    assert harness.editor.undo == [{"R1": "C100", "R2": "C200"}]
    assert harness.editor.events == []


def test_partial_error_is_raised_after_native_finalization(harness: Any) -> None:
    """Propagate the original mutation failure after native dirty/undo cleanup."""
    action = open_window(harness)
    editor = harness.editor

    def fail_later() -> None:
        editor.board.fields["R1"] = "C500"
        raise ValueError("R2 setter failed")

    with pytest.raises(ValueError, match="R2 setter failed"):
        action(fail_later)
    assert editor.finalized == 2
    assert editor.python_errors == []
    assert editor.dirty
    assert editor.undo[-1]["R1"] == "C100"
    action(lambda: editor.board.fields.update(R2="C600"))
    assert editor.board.fields == {"R1": "C500", "R2": "C600"}


@pytest.mark.parametrize(
    "failure", ["board", "editor", "disabled", "menu", "ambiguous", "unhandled", "busy"]
)
def test_unavailable_context_rejects_before_mutation(
    harness: Any, failure: str
) -> None:
    """Never mutate after losing the originating board, editor, or native command."""
    action = open_window(harness)
    editor = harness.editor
    if failure == "board":
        editor.board = SimpleNamespace(
            m_Uuid=SimpleNamespace(AsString=lambda: "original-board"),
            this=2048,
            GetFileName=lambda: "original.kicad_pcb",
            fields={"R1": "C999"},
        )
    elif failure == "editor":
        editor.deleted = True
    elif failure == "disabled":
        editor.enabled = False
    elif failure == "menu":
        editor.menu.items.clear()
    elif failure == "ambiguous":
        editor.menu.items.append(MenuItem("JLCPCB Tools", 72))
    elif failure == "busy":
        editor.running = True
    else:
        editor.consume_event = False
    called: list[bool] = []
    with pytest.raises(RuntimeError):
        action(lambda: called.append(True))
    assert called == []


def test_recreated_menu_uses_current_action_id(harness: Any) -> None:
    """Resolve a fresh native ID after a menu rebuild."""
    action = open_window(harness)
    harness.editor.menu.items[0].item_id = 83
    action(lambda: harness.editor.board.fields.update(R1="C700"))
    assert harness.editor.events == [83]


def test_old_window_cannot_retarget_after_new_board_launch(harness: Any) -> None:
    """Each window's callback keeps its original board identity across later launches."""
    old_action = open_window(harness)
    harness.editor.board = SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: "new-board"),
        this=2048,
        GetFileName=lambda: "new.kicad_pcb",
        fields={"R1": "C999"},
    )
    new_action = open_window(harness)
    with pytest.raises(RuntimeError):
        old_action(lambda: harness.editor.board.fields.update(R1="C123"))
    new_action(lambda: harness.editor.board.fields.update(R1="C456"))
    assert harness.editor.board.fields["R1"] == "C456"


def test_nested_dispatch_is_rejected_and_outer_native_action_finalizes(
    harness: Any,
) -> None:
    """Reject nested undo wrappers while still finishing the outer native action."""
    action = open_window(harness)
    with pytest.raises(RuntimeError, match="already"):
        action(lambda: action(lambda: None))
    assert harness.editor.finalized == 2


def test_native_pointer_identifies_new_wrapper_of_same_native_board(
    harness: Any,
) -> None:
    """Accept new SWIG-like Python wrappers for the unchanged native board."""
    action = open_window(harness)
    harness.editor.board = SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: "original-board"),
        this=1024,
        GetFileName=lambda: "original.kicad_pcb",
        fields=harness.editor.board.fields,
    )
    action(lambda: harness.editor.board.fields.update(R1="C800"))
    assert harness.editor.board.fields["R1"] == "C800"


def test_context_is_rechecked_inside_native_callback(harness: Any) -> None:
    """Never apply a queued callback if the board changed during native dispatch."""
    action = open_window(harness)
    dispatch = harness.editor.ProcessEvent

    def switch_then_dispatch(event: Any) -> bool:
        harness.editor.board = SimpleNamespace(
            m_Uuid=SimpleNamespace(AsString=lambda: "replacement"),
            this=2048,
            GetFileName=lambda: "replacement.kicad_pcb",
            fields={},
        )
        return dispatch(event)

    harness.editor.ProcessEvent = switch_then_dispatch
    called: list[bool] = []
    with pytest.raises(RuntimeError, match="original PCB"):
        action(lambda: called.append(True))
    assert called == []
    assert harness.editor.finalized == 2


def test_failed_event_dispatch_does_not_leave_a_pending_edit(harness: Any) -> None:
    """Clear dispatch state when wx raises before the native callback runs."""
    action = open_window(harness)
    dispatch = harness.editor.ProcessEvent

    def fail_dispatch(event: Any) -> bool:
        raise RuntimeError("native event failure")

    harness.editor.ProcessEvent = fail_dispatch
    called: list[bool] = []
    with pytest.raises(RuntimeError, match="native event failure"):
        action(lambda: called.append(True))
    assert called == []
    harness.editor.ProcessEvent = dispatch
    action(lambda: called.append(True))
    assert called == [True]


def test_python_only_menu_handler_cannot_bypass_native_transaction(
    harness: Any,
) -> None:
    """A consumed menu event must enter KiCad's action wrapper before editing."""
    action = open_window(harness)
    editor = harness.editor

    def bypass_native_wrapper(event: Any) -> bool:
        editor.plugin.Run()
        return True

    editor.ProcessEvent = bypass_native_wrapper
    called: list[bool] = []
    with pytest.raises(RuntimeError, match="native action"):
        action(lambda: called.append(True))
    assert called == []


def test_save_as_rejects_previous_project_context(harness: Any) -> None:
    """A renamed board must reopen against the new project's paths and settings."""
    action = open_window(harness)
    harness.editor.board.GetFileName = lambda: "save-as.kicad_pcb"
    called: list[bool] = []
    with pytest.raises(RuntimeError, match="original PCB"):
        action(lambda: called.append(True))
    assert called == []
    assert harness.editor.events == []


def test_native_undo_redo_restores_each_independent_modeless_edit(harness: Any) -> None:
    """Each completed callback supplies its own complete native undo boundary."""
    action = open_window(harness)
    editor = harness.editor
    action(lambda: editor.board.fields.update(R1="C300", R2="C400"))
    action(lambda: editor.board.fields.update(R1=""))
    editor.undo_last()
    assert editor.board.fields == {"R1": "C300", "R2": "C400"}
    editor.undo_last()
    assert editor.board.fields == {"R1": "C100", "R2": "C200"}
    editor.redo_last()
    assert editor.board.fields == {"R1": "C300", "R2": "C400"}
    editor.redo_last()
    assert editor.board.fields == {"R1": "", "R2": "C400"}
    assert harness.editor.events == [71, 71]


def test_missing_board_rejects_without_mutation(harness: Any) -> None:
    """Closing a PCB invalidates callbacks even before the native editor closes."""
    action = open_window(harness)
    harness.editor.board = None
    called: list[bool] = []
    with pytest.raises(RuntimeError, match="open board"):
        action(lambda: called.append(True))
    assert called == []
