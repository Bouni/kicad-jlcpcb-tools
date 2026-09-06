"""Persist data-view column widths in the existing window settings."""

from __future__ import annotations

from typing import TypeVar

import wx
import wx.dataview as dv

from .helpers import HighResWxSize

Size = TypeVar("Size", int, "wx.Size")


def to_dip(window: wx.Window, size: Size) -> Size:
    """Convert a size to logical pixels when supported by this wx version."""
    return window.ToDIP(size) if hasattr(window, "ToDIP") else size


def get_column_widths(
    control: dv.DataViewCtrl, column_keys: dict[int, str]
) -> dict[str, int]:
    """Return logical widths by stable key for explicitly persisted columns.

    The last column must be an unpersisted spacer to absorb automatic stretching.
    """
    return {
        column_keys[column.GetModelColumn()]: to_dip(control, column.GetWidth())
        for column in control.GetColumns()
        if column.GetModelColumn() in column_keys
    }


def restore_column_widths(
    control: dv.DataViewCtrl, widths: object, column_keys: dict[int, str]
) -> None:
    """Restore widths after layout, bounded by the control's usable width."""
    if not isinstance(widths, dict):
        return
    client_width = control.GetClientSize().GetWidth()
    if client_width <= 0:
        return
    maximum = max(1, to_dip(control, client_width))
    for column in control.GetColumns():
        if column.GetModelColumn() not in column_keys:
            continue
        width = widths.get(column_keys[column.GetModelColumn()])
        if type(width) is int and width > 0:
            # Bound Python integers before converting them through native wx APIs.
            width = HighResWxSize(control, min(width, maximum))
            column.SetWidth(min(width, client_width))
