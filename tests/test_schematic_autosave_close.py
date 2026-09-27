"""Save-on-close decisions precede every irreversible window teardown step."""

from collections.abc import Iterator
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from .wx_harness import load_siblings, mainwindow_stubs, wx_stubs


class CloseEvent:
    """Retain the native veto flag instead of merely recording a method call."""

    def __init__(self, forced: bool = False) -> None:
        self.forced = forced
        self.vetoed = False

    def CanVeto(self) -> bool:
        """Report whether this is an ordinary user close."""
        return not self.forced

    def Veto(self) -> None:
        """Reject illegal attempts to prevent forced shutdown."""
        assert not self.forced
        self.vetoed = True


@pytest.fixture
def window() -> Iterator[Any]:
    """Run the production close handler around independently observable resources."""
    package = "_schematic_autosave_close"
    stubs = mainwindow_stubs(
        package,
        wx=wx_stubs(Frame=object, Dialog=type("Dialog", (), {}), NewIdRef=object),
    )
    with load_siblings(package, ("mainwindow",), stubs) as modules:
        steps: list[str] = []
        frame = SimpleNamespace(
            _closing=False,
            _saving_on_close=False,
            _layout_ready=False,
            _project_storage_unavailable=False,
            store=object(),
            logger=Mock(),
            settings={},
            GetChildren=lambda: [],
            Destroy=lambda: steps.append("destroy"),
            assembly_lookup=SimpleNamespace(close=lambda: steps.append("lookup")),
            _type_cell_tooltip=SimpleNamespace(stop=lambda: steps.append("tooltip")),
            _variant_controller=None,
            steps=steps,
        )

        def save(*, interactive: bool = True) -> bool:
            """Assert that resources are still usable at the save boundary."""
            assert not frame._closing
            assert not steps
            steps.append("save")
            return True

        frame.export_to_schematic = Mock(side_effect=save)
        frame.quit_dialog = MethodType(
            modules["mainwindow"].JLCPCBTools.quit_dialog, frame
        )
        yield frame


def test_save_finishes_before_teardown_and_repeated_close_is_inert(window: Any) -> None:
    """A successful close saves exactly once, then releases all owned resources."""
    event = CloseEvent()
    window.quit_dialog(event)
    window.quit_dialog(CloseEvent())
    assert not event.vetoed
    assert window.steps == ["save", "lookup", "tooltip", "destroy"]
    window.export_to_schematic.assert_called_once_with(interactive=True)


def test_canceled_save_preserves_live_window_and_can_retry(window: Any) -> None:
    """A failed/canceled save cannot consume lookup, tooltip, or window lifetime."""
    window.export_to_schematic.side_effect = [False, True]
    first = CloseEvent()
    window.quit_dialog(first)
    assert first.vetoed
    assert not window._closing and not window._saving_on_close
    assert not window.steps
    second = CloseEvent()
    window.quit_dialog(second)
    assert not second.vetoed
    assert window.steps == ["lookup", "tooltip", "destroy"]
    assert window.export_to_schematic.call_count == 2


def test_nested_close_during_save_does_not_save_or_destroy_twice(window: Any) -> None:
    """A modal save prompt can dispatch another close event before returning."""
    nested = CloseEvent()

    def save(*, interactive: bool) -> bool:
        window.quit_dialog(nested)
        assert not window.steps
        return True

    window.export_to_schematic.side_effect = save
    window.quit_dialog(CloseEvent())
    assert nested.vetoed
    window.export_to_schematic.assert_called_once()
    assert window.steps == ["lookup", "tooltip", "destroy"]


def test_forced_close_uses_no_interactive_save_and_cannot_be_vetoed(
    window: Any,
) -> None:
    """A host-forced shutdown cannot be held open by a schematic failure."""
    window.export_to_schematic.side_effect = None
    window.export_to_schematic.return_value = False
    event = CloseEvent(forced=True)
    window.quit_dialog(event)
    assert not event.vetoed
    window.export_to_schematic.assert_called_once_with(interactive=False)
    assert window.steps == ["lookup", "tooltip", "destroy"]


@pytest.mark.parametrize("unavailable", ["store", "storage"])
def test_uninitialized_windows_close_without_schematic_access(
    window: Any, unavailable: str
) -> None:
    """Recovery/settings windows remain closable when assignments never loaded."""
    if unavailable == "store":
        window.store = None
    else:
        window._project_storage_unavailable = True
    window.quit_dialog(CloseEvent())
    window.export_to_schematic.assert_not_called()
    assert window.steps == ["lookup", "tooltip", "destroy"]


@pytest.mark.parametrize("variants", [False, True])
def test_active_generation_defers_save_and_vetoes_close(
    window: Any, variants: bool
) -> None:
    """Generation keeps its source and resources until its own finally block runs."""
    if variants:
        window._variant_controller = SimpleNamespace(
            session=SimpleNamespace(generating=True)
        )
    else:
        window._generating = True
    event = CloseEvent()
    window.quit_dialog(event)
    assert event.vetoed
    window.export_to_schematic.assert_not_called()
    assert not window.steps
