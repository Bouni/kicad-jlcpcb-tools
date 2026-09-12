"""Stateful view-model doubles for stock display and concern workflows."""

from collections.abc import Iterator
from contextlib import contextmanager
import types
from typing import Any

from .wx_harness import load_siblings, module, wx_stubs


class DataViewModel:
    """Preserve row identities and native view notifications without a GUI."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, tuple[Any, ...]]] = []

    @staticmethod
    def ObjectToItem(value: Any) -> Any:
        """Represent an item with its underlying row."""
        return value

    @staticmethod
    def ItemToObject(value: Any) -> Any:
        """Recover the exact row represented by an item."""
        return value

    def HasValue(self, _item: Any, _column: int) -> bool:
        """Allow ordinary model cells to contain values."""
        return True

    def ItemAdded(self, *args: Any) -> None:
        """Record the view notification and affected row or cell."""
        self.notifications.append(("added", args))

    def ItemChanged(self, *args: Any) -> None:
        """Record the view notification and affected row or cell."""
        self.notifications.append(("changed", args))

    def ValueChanged(self, *args: Any) -> None:
        """Record the view notification and affected row or cell."""
        self.notifications.append(("value", args))

    def Cleared(self) -> None:
        """Record the view notification."""
        self.notifications.append(("cleared", ()))

    def Resort(self) -> None:
        """Record the view notification."""
        self.notifications.append(("resort", ()))


@contextmanager
def stock_modules() -> Iterator[types.SimpleNamespace]:
    """Import real models and columns with only unrelated GUI behavior replaced."""
    package = "stock_workflow_tests"
    stubs = wx_stubs(
        Colour=lambda *rgb: rgb,
        SYS_COLOUR_WINDOW=0,
        SystemSettings=types.SimpleNamespace(
            GetColour=lambda _key: types.SimpleNamespace(GetLuminance=lambda: 0.1)
        ),
    )
    stubs["wx.dataview"].PyDataViewModel = DataViewModel
    stubs["wx.dataview"].DataViewIconText = lambda *args: args
    stubs["wx.dataview"].NullDataViewItem = None
    stubs[f"{package}.helpers"] = module(
        f"{package}.helpers", loadIconScaled=lambda name, _scale: name
    )
    stubs[f"{package}.dataview_highlight"] = module(
        f"{package}.dataview_highlight",
        decode_highlighted_value=lambda value: (value, []),
        encode_highlighted_value=lambda value, _terms: value,
        expand_footprint=lambda *_args: [],
        expand_value=lambda *_args: [],
    )
    with load_siblings(package, ("datamodel",), stubs) as loaded:
        yield types.SimpleNamespace(**loaded, wx=stubs["wx"], package=package)


def board_row(reference: str, stock: Any, lcsc: str = "C1") -> list[Any]:
    """Build the raw row consumed by the main model's real AddEntry path."""
    return [reference, "10k", "R_0603", lcsc, "Basic", stock] + ["0"] * 8


def selector_row(stock: Any, lcsc: str = "C1") -> list[Any]:
    """Build a selector row matching the real selector column order."""
    return [lcsc, "part", "R_0603", "Basic", "params", stock, "mfr", "desc", "", ""]
