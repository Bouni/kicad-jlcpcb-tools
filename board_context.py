"""Identify the live PCB targeted by a modeless plugin window."""

from contextlib import suppress
import hashlib
from inspect import getattr_static
from typing import Any


class BoardContextChanged(RuntimeError):
    """The editor no longer presents the board this window was opened for."""


def board_identity(board: Any) -> str:
    """Include native address, UUID, and filename without keeping a native handle.

    KiCad can return several SWIG proxies for one board. Its saved UUID alone
    cannot distinguish a board reopened in the editor, and Save As changes the
    project whose settings and output paths the plugin must use. Non-SWIG
    adapters fall back to Python identity without requiring optional variant APIs.
    """
    if board is None:
        raise BoardContextChanged("The PCB editor no longer has an open board")
    pointer = board
    seen: set[int] = set()
    for _ in range(8):
        if getattr_static(pointer, "this", None) is None or id(pointer) in seen:
            break
        seen.add(id(pointer))
        pointer = pointer.this
    address = id(board)
    if seen:
        with suppress(TypeError, ValueError):
            address = int(pointer)
    uuid = getattr(board, "m_Uuid", None)
    uuid_text = uuid.AsString() if callable(getattr(uuid, "AsString", None)) else ""
    parts = (address, str(uuid_text), str(board.GetFileName()))
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()
