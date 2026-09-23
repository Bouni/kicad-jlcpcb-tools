"""Identity checks must follow native board lifetime and its project filename."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .wx_harness import load_siblings


def board(*, pointer: int = 100, filename: str = "original.kicad_pcb") -> Any:
    """Build a SWIG-shaped proxy without needing a native editor process."""
    return SimpleNamespace(
        this=SimpleNamespace(this=pointer),
        m_Uuid=SimpleNamespace(AsString=lambda: "unchanged-uuid"),
        GetFileName=lambda: filename,
    )


def test_identity_follows_native_lifetime_across_wrapper_objects() -> None:
    """New proxies may denote the same board; reloading a UUID is a new target."""
    with load_siblings("_board_context_tests", ("board_context",), {}) as modules:
        identify = modules["board_context"].board_identity
        assert identify(board()) == identify(board())
        assert identify(board()) != identify(board(pointer=200))


def test_save_as_invalidates_captured_project_context() -> None:
    """Identical native pointers and UUIDs cannot retain the old project path."""
    with load_siblings("_board_context_tests", ("board_context",), {}) as modules:
        identify = modules["board_context"].board_identity
        assert identify(board()) != identify(board(filename="saved-as.kicad_pcb"))


def test_missing_uuid_and_pointer_use_object_identity() -> None:
    """Stateful adapters without SWIG details remain distinct board lifetimes."""
    with load_siblings("_board_context_tests", ("board_context",), {}) as modules:
        identify = modules["board_context"].board_identity
        first = SimpleNamespace(GetFileName=lambda: "original.kicad_pcb")
        second = SimpleNamespace(GetFileName=lambda: "original.kicad_pcb")
        assert identify(first) == identify(first)
        assert identify(first) != identify(second)


def test_missing_board_is_reported_without_dereferencing() -> None:
    """A closed editor cannot produce a target for a later mutation."""
    with load_siblings("_board_context_tests", ("board_context",), {}) as modules:
        context = modules["board_context"]
        with pytest.raises(context.BoardContextChanged, match="open board"):
            context.board_identity(None)


def test_dynamic_adapter_attributes_cannot_form_an_unbounded_proxy_chain() -> None:
    """Non-SWIG adapters may dynamically synthesize arbitrary attributes."""
    with load_siblings("_board_context_tests", ("board_context",), {}) as modules:
        identify = modules["board_context"].board_identity
        first = MagicMock()
        second = MagicMock()
        first.GetFileName.return_value = second.GetFileName.return_value = "board"
        assert identify(first) == identify(first)
        assert identify(first) != identify(second)
