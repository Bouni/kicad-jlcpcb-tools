"""Keep modeless windows bound to the PCB they were opened for."""

from collections.abc import Callable
from functools import wraps
from typing import Any, Union


class BoardContextChanged(RuntimeError):
    """The originating native board was closed, replaced, or saved elsewhere."""


def board_identity(board: Any) -> Union[str, int]:
    """Use KiCad's public UUID, or object identity for owned standalone boards."""
    uuid = getattr(board, "m_Uuid", None)
    return uuid.AsString() if uuid is not None else id(board)


def handle_board_context(method: Callable[..., Any]) -> Callable[..., Any]:
    """Turn a stale-board error at a UI boundary into a disabled, readable window."""

    @wraps(method)
    def guarded(window: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(window, *args, **kwargs)
        except BoardContextChanged as error:
            window._set_project_storage_error(error)
            return None

    return guarded
