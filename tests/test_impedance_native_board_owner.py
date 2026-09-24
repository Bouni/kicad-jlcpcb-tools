"""Model native allocation and SWIG registry lifetime, not setter call counts."""

import gc
from types import SimpleNamespace
from typing import Any
import weakref

import pytest

from impedance.board_copy import NativeBoardOwner


class NativeBoard:
    """Emulate SWIG 4.4.1's unbalanced borrowed-to-owned promotion behavior."""

    def __init__(self, runtime: Any, *, owned: bool = False) -> None:
        self.runtime = runtime
        self._owned = owned
        self.deleted = False
        if owned:
            runtime.capsule_references += 1

    @property
    def thisown(self) -> bool:
        """Read native wrapper ownership."""
        return self._owned

    @thisown.setter
    def thisown(self, owned: bool) -> None:
        """Match SWIG acquire/disown: no balancing capsule refcount change."""
        self._owned = owned

    def __del__(self) -> None:
        """Model wrapper destruction's capsule decref for any owned wrapper."""
        if self._owned:
            self.runtime.capsule_references -= 1
            self.delete_native(self)

    @staticmethod
    def delete_native(board: "NativeBoard") -> None:
        """Match exported delete_BOARD: delete allocation without capsule decref."""
        if board.deleted:
            raise AssertionError("Native board deleted twice")
        board.deleted = True
        board.runtime.native_deletes += 1
        board._owned = False


def runtime() -> Any:
    """Represent the registry's module-owned reference and native allocations."""
    return SimpleNamespace(capsule_references=1, native_deletes=0)


def test_many_scopes_preserve_registry_and_release_every_borrowed_allocation() -> None:
    """Repeated preview snapshots cannot tear down global GetBoard type metadata."""
    state = runtime()
    for _ in range(100):
        board = NativeBoard(state)
        with NativeBoardOwner(board, NativeBoard.delete_native) as current:
            assert current is board
            assert board.thisown is False
            assert board.deleted is False
        assert board.deleted is True
        del current, board
    gc.collect()
    assert state.capsule_references == 1
    assert state.native_deletes == 100


def test_explicit_close_is_idempotent_and_rejects_later_access() -> None:
    """Do not expose dangling borrowed aliases through a closed owner."""
    state = runtime()
    owner = NativeBoardOwner(NativeBoard(state), NativeBoard.delete_native)
    assert owner.closed is False
    owner.close()
    owner.close()
    assert owner.closed is True
    assert state.native_deletes == 1
    assert state.capsule_references == 1
    with pytest.raises(RuntimeError, match="snapshot is closed"):
        _ = owner.board
    with pytest.raises(RuntimeError, match="snapshot is closed"), owner:
        pytest.fail("Closed snapshot reopened")


def test_scope_exception_releases_board_without_suppressing_original_error() -> None:
    """Rendering failures clean up their allocation and remain actionable."""
    state = runtime()
    owner = NativeBoardOwner(NativeBoard(state), NativeBoard.delete_native)
    with pytest.raises(ValueError, match="plot failed"), owner:
        raise ValueError("plot failed")
    assert owner.closed is True
    assert state.native_deletes == 1


def test_initially_owned_wrapper_keeps_python_ownership_and_releases_reference() -> (
    None
):
    """Bindings with %newobject balance their own capsule references via GC."""
    state = runtime()
    board = NativeBoard(state, owned=True)
    reference = weakref.ref(board)

    def forbidden_delete(_board: Any) -> None:
        """Manual delete of an initially owned wrapper would leak its capsule ref."""
        pytest.fail("Originally owned wrapper was manually destroyed")

    owner = NativeBoardOwner(board, forbidden_delete)
    assert owner.board.thisown is True
    del board
    assert reference() is not None
    assert state.capsule_references == 2
    owner.close()
    gc.collect()
    assert reference() is None
    assert state.capsule_references == 1
    assert state.native_deletes == 1


def test_owned_wrapper_retains_external_alias_until_alias_is_released() -> None:
    """Closing drops the owner's reference but cannot invalidate a Python owner."""
    state = runtime()
    board = NativeBoard(state, owned=True)
    owner = NativeBoardOwner(board, NativeBoard.delete_native)
    owner.close()
    assert board.thisown is True
    assert board.deleted is False
    del board
    gc.collect()
    assert state.native_deletes == 1
    assert state.capsule_references == 1


def test_failed_native_delete_is_not_retried() -> None:
    """An ambiguous native exception must never trigger a second deletion."""
    state = runtime()

    def failing_delete(board: NativeBoard) -> None:
        """Simulate a binding reporting failure after native memory was freed."""
        NativeBoard.delete_native(board)
        raise RuntimeError("native cleanup failed")

    owner = NativeBoardOwner(NativeBoard(state), failing_delete)
    with pytest.raises(RuntimeError, match="native cleanup failed"):
        owner.close()
    owner.close()
    assert owner.closed is True
    assert state.native_deletes == 1


def test_owner_never_releases_a_distinct_editor_board() -> None:
    """The native deleter receives only the allocation given to its owner."""
    state = runtime()
    editor_board = NativeBoard(state)
    detached = NativeBoard(state)
    with NativeBoardOwner(detached, NativeBoard.delete_native):
        pass
    assert editor_board.deleted is False
    assert editor_board.thisown is False
    assert detached.deleted is True


def test_missing_destructor_rejected() -> None:
    """Never create an owner unable to release a borrowed allocation."""
    with pytest.raises(TypeError, match="destructor is required"):
        NativeBoardOwner(NativeBoard(runtime()), None)
