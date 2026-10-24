"""Native wx.Grid measurement without application-specific sizing policy."""

from typing import Any


def measure_grid_content_widths(grid: Any) -> tuple[int, ...]:
    """Measure cell content while leaving header layout to the caller.

    The grid table must expose ``measuring_cells`` and return empty column
    labels while that flag is true. Previous minima must be cleared because
    ``AutoSizeColumns(setAsMin=False)`` still respects them. The caller owns
    batching, final minimums, padding, and any custom header geometry.
    """
    column_count = grid.GetNumberCols()
    for column in range(column_count):
        grid.SetColMinimalWidth(column, 1)
    if not grid.GetNumberRows():
        return (0,) * column_count

    table = grid.GetTable()
    previous = table.measuring_cells
    table.measuring_cells = True
    try:
        grid.AutoSizeColumns(setAsMin=False)
    finally:
        table.measuring_cells = previous
    return tuple(grid.GetColSize(column) for column in range(column_count))


def grid_sizing_signature(grid: Any) -> tuple[str, str, int]:
    """Identify body/header fonts and display scale for cached column widths."""
    return (
        grid.GetDefaultCellFont().GetNativeFontInfoDesc(),
        grid.GetLabelFont().GetNativeFontInfoDesc(),
        grid.FromDIP(100),
    )
