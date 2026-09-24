"""Content-sized workbook columns through real constructors and refresh paths.

The shared control harness uses deterministic glyph widths and a limited native
autosize sample. These tests verify sizing decisions, not native font rendering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from .test_impedance_automatic_rows import _show
from .test_impedance_dialog import _DataViewColumn
from .test_impedance_draft_dialog import (
    _board,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_width_mismatch import _config

draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.model import BoardSnapshot, Config

    from .test_impedance_dialog import _ConstructedControl


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave unrelated catalog transport outside column-sizing regressions."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _required_width(table: _ConstructedControl, column: int) -> int:
    """Measure the header and all displayed text with a compact renderer inset."""
    values = [table.GetColumn(column).GetTitle()]
    if column:
        values.extend(str(row[column]) for row in table.table_rows)
    if column == 8:
        values.append("!=")
    text_width = max(table.GetTextExtent(value)[0] for value in values)
    return max(text_width, 18 if column == 0 else 0) + table.FromDIP(12)


def _assert_compact(table: _ConstructedControl) -> None:
    """Assert every header/value fits without retaining a previous wider size."""
    assert table.GetColumnCount() in (11, 12)
    if table.GetColumnCount() == 12:
        assert table.GetColumn(11).GetTitle() == ""
        assert table.GetColumn(11).GetMinWidth() == 0
        assert all(len(row) == 12 and row[11] == "" for row in table.table_rows)
    for column in range(11):
        needed = _required_width(table, column)
        actual = table.GetColumn(column).GetWidth()
        assert actual >= needed, (column, actual, needed, "clipped content")
        assert actual <= needed + table.FromDIP(4), (
            column,
            actual,
            needed,
            "unnecessary width",
        )
    assert "True" not in table.measured_text
    assert "False" not in table.measured_text


def _many_rows(api: SimpleNamespace, *, long_last_net: bool) -> BoardSnapshot:
    """Put the longest displayed name beyond the harness autosize sample."""
    names = [f"N{index:02d}" for index in range(40)]
    if long_last_net:
        names[-1] = "Connector_to_radio_front_end_impedance_controlled_clock_signal"
    traces = tuple(
        api.model.Trace(
            f"track-{index}",
            "F.Cu",
            name,
            150_000,
            ((0, index * 1_000_000), (2_000_000, index * 1_000_000)),
        )
        for index, name in enumerate(names)
    )
    return replace(
        _board(api),
        traces=traces,
        net_class_memberships=tuple((name, ("RF",)) for name in names),
        differential_pairs=(),
    )


def test_constructor_sizes_workbook_headers_and_values_compactly(
    draft_api: SimpleNamespace,
) -> None:
    """Short row numbers, nets and marker should not use old fixed widths."""
    api = draft_api
    dialog = _open(api, _config(api), _board(api))

    assert dialog.sections.GetColumnCount() == 12
    _assert_compact(dialog.sections)
    assert dialog.sections.GetColumn(1).GetWidth() < 50
    assert dialog.sections.GetColumn(6).GetWidth() < 120
    assert dialog.sections.GetColumn(8).GetWidth() < 35


def test_refresh_grows_for_offscreen_net_then_shrinks_for_short_values(
    draft_api: SimpleNamespace,
) -> None:
    """Fit all current rows and discard stale minimums after a shorter refresh."""
    api = draft_api
    short = _many_rows(api, long_last_net=False)
    long = _many_rows(api, long_last_net=True)
    dialog = _open(api, _config(api), short)
    _assert_compact(dialog.sections)
    short_width = dialog.sections.GetColumn(6).GetWidth()

    assert dialog._refresh_rows(long)
    assert len(dialog.sections.table_rows[-1][6]) > 50
    _assert_compact(dialog.sections)
    assert dialog.sections.GetColumn(6).GetWidth() > short_width

    assert dialog._refresh_rows(short)
    _assert_compact(dialog.sections)
    assert dialog.sections.GetColumn(6).GetWidth() == short_width


def test_gtk_explicit_width_shrinks_after_long_rows_are_removed(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GTK's ignored autosize request cannot retain an obsolete wide column."""
    api = draft_api
    monkeypatch.setattr(api.wx, "Platform", "__WXGTK__")
    original_set_width = _DataViewColumn.SetWidth

    def gtk_set_width(column: _DataViewColumn, width: int) -> None:
        """Model wxGTK keeping resolved width on AUTO, with minimum growth."""
        if width == api.wx.COL_WIDTH_AUTOSIZE and column.width >= 0:
            width = column.width
        original_set_width(column, width)

    monkeypatch.setattr(_DataViewColumn, "SetWidth", gtk_set_width)
    long = _many_rows(api, long_last_net=True)
    short = _many_rows(api, long_last_net=False)
    dialog = _open(api, _config(api), long)
    assert dialog.sections.GetColumnCount() == 12
    assert dialog.sections.GetColumn(11).GetTitle() == ""
    assert all(row[11] == "" for row in dialog.sections.table_rows)
    _assert_compact(dialog.sections)
    long_width = dialog.sections.GetColumn(6).GetWidth()

    assert dialog._refresh_rows(short)

    _assert_compact(dialog.sections)
    assert dialog.sections.GetColumn(6).GetWidth() < long_width


def test_native_geometry_assertion_waits_for_deferred_gtk_allocation(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid GTK width request need not change its last native allocation yet."""
    from . import test_impedance_workbook_sizing_native as native_sizing

    api = draft_api
    monkeypatch.setattr(api.wx, "Platform", "__WXGTK__")
    long = _many_rows(api, long_last_net=True)
    short = _many_rows(api, long_last_net=False)
    dialog = _open(api, _config(api), long)
    original_set_width = _DataViewColumn.SetWidth
    pending: dict[_DataViewColumn, int] = {}
    waited: list[bool] = []

    def request_width(column: _DataViewColumn, width: int) -> None:
        """Keep GTK's allocated width until a later native layout phase."""
        pending[column] = width

    def allocate_after_wait(
        _wx: object, predicate: Callable[[], bool], **_options: object
    ) -> None:
        """Distinguish immediate event draining from an eventual allocation."""
        assert not predicate(), "The previous wide allocation must not pass."
        # Immediate yields cannot make a future frame-clock phase ready.
        for _ in range(3):
            assert not predicate()
        for column, width in pending.items():
            original_set_width(column, width)
        assert predicate(), "The accepted width request must pass after layout."
        waited.append(True)

    monkeypatch.setattr(_DataViewColumn, "SetWidth", request_width)
    monkeypatch.setattr(native_sizing, "wait_until", allocate_after_wait)
    assert dialog._refresh_rows(short)
    native_sizing.assert_columns_fit(dialog.sections)
    assert waited == [True]


@pytest.mark.parametrize("allocated_width", [1, 1000])
def test_native_geometry_deadline_rejects_clipped_or_wide_columns(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    allocated_width: int,
) -> None:
    """Waiting cannot accept a persistent width violation or lose phase evidence."""
    from . import test_impedance_workbook_sizing_native as native_sizing

    api = draft_api
    dialog = _open(api, _config(api), _board(api))
    dialog.sections.GetColumn(6).width = allocated_width

    def deadline(
        _wx: object, predicate: Callable[[], bool], **_options: object
    ) -> None:
        """Expire the bounded wait with a native allocation that never converges."""
        assert not predicate()
        raise AssertionError("Native layout deadline expired")

    monkeypatch.setattr(native_sizing, "wait_until", deadline)
    with pytest.raises(AssertionError, match="read failure: after") as failure:
        native_sizing.assert_columns_fit(
            dialog.sections, phase="read failure", expected_rows=2
        )
    assert "allowed, requested minimum, allocated" in str(failure.value)
    assert f", {allocated_width})" in str(failure.value)


def test_native_resize_restores_compact_final_column(
    draft_api: SimpleNamespace,
) -> None:
    """Cocoa's final-column stretch must be corrected after native resizing."""
    api = draft_api
    dialog = _open(api, _config(api), _board(api))
    _show(api, dialog)
    selected = dialog._row_ids[dialog.sections.GetSelectedRow()]
    included = set(dialog.session.included)
    final_column = dialog.sections.GetColumn(10)
    compact_width = final_column.GetWidth()
    final_column.SetWidth(compact_width + 300)
    resize_handler = dialog.sections.bindings.get((api.wx.EVT_SIZE, -1))
    if resize_handler is not None:
        resize_handler(
            SimpleNamespace(
                GetEventObject=lambda: dialog.sections,
                GetSize=lambda: (1400, 500),
                Skip=lambda: None,
            )
        )
    api.drain()

    _assert_compact(dialog.sections)
    assert final_column.GetWidth() == compact_width
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected
    assert dialog.session.included == included


def test_cocoa_late_native_layout_expands_only_the_inert_spacer(
    draft_api: SimpleNamespace,
) -> None:
    """AppKit can resize its last column after the queued product fit has run."""
    api = draft_api
    dialog = _open(api, _config(api), _board(api))
    _show(api, dialog)
    selected = dialog.sections.GetSelectedRow()
    included = set(dialog.session.included)
    intrinsic = tuple(
        dialog.sections.GetColumn(index).GetWidth() for index in range(11)
    )
    # AppKit's last-column policy uses the last column with an autoresizing
    # mask. A flags=0 spacer is skipped and still leaves Segments expanding.
    # This allocation need not cause another wx size event or queued fit.
    last_native_column = next(
        column
        for column in reversed(dialog.sections.dataview_columns)
        if column.flags & api.dv.DATAVIEW_COL_RESIZABLE
    )
    last_native_column.SetWidth(last_native_column.GetWidth() + 300)

    _assert_compact(dialog.sections)
    assert (
        tuple(dialog.sections.GetColumn(index).GetWidth() for index in range(11))
        == intrinsic
    )
    assert dialog.sections.GetSelectedRow() == selected
    assert dialog.session.included == included


def test_calculation_refresh_resizes_pending_and_numeric_targets(
    draft_api: SimpleNamespace,
) -> None:
    """Changing calculator text can grow and shrink the existing Target column."""
    api = draft_api
    board = _board(api)
    pending = _config(api, state="pending")
    numeric = _config(api, target_width_nm=123_456_789)
    dialog = _open(api, pending, board)
    _assert_compact(dialog.sections)
    pending_width = dialog.sections.GetColumn(9).GetWidth()
    for config in (numeric, pending):
        dialog.session.replace_calculation_context(config.stackup, config.width_results)
        assert dialog._refresh_rows(board)
        _assert_compact(dialog.sections)
        if config is numeric:
            assert dialog.sections.GetColumn(9).GetWidth() > pending_width
        else:
            assert dialog.sections.GetColumn(9).GetWidth() == pending_width


def test_empty_and_failed_rows_keep_headers_and_reserved_marker_compact(
    draft_api: SimpleNamespace,
) -> None:
    """An empty model fits headers; a failed refresh removes old wide values."""
    api = draft_api
    empty = _open(api, api.model.Config(), _board(api))
    assert empty.sections.table_rows == []
    _assert_compact(empty.sections)
    assert empty.sections.GetColumn(8).GetWidth() >= 14

    dialog = _open(api, _config(api), _many_rows(api, long_last_net=True))
    dialog._fail_rows("The board could not be read.")
    assert dialog.sections.table_rows == []
    _assert_compact(dialog.sections)
    assert dialog.sections.GetColumn(6).GetWidth() < 120


def test_first_show_and_repopulation_preserve_selection_and_inclusion(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sizing after native show and user toggles must preserve workbook state."""
    api = draft_api
    dialog = _open(api, _config(api), _board(api))
    dialog.sections.SelectRow(1)
    selected = dialog._row_ids[1]
    dialog.sections.SetToggleValue(False, 0, 0)
    dialog.sections.emit(api.dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, row=0)
    included = set(dialog.session.included)
    before_show = sum(
        len(column.width_requests) for column in dialog.sections.dataview_columns
    )
    original_extent = dialog.sections.GetTextExtent

    def realized_extent(text: str) -> tuple[int, int]:
        """Model fonts becoming wider when the native view becomes visible."""
        width, height = original_extent(text)
        return width * 2, height

    monkeypatch.setattr(dialog.sections, "GetTextExtent", realized_extent)

    _show(api, dialog)

    after_show = sum(
        len(column.width_requests) for column in dialog.sections.dataview_columns
    )
    assert after_show > before_show
    _assert_compact(dialog.sections)
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected
    assert dialog.session.included == included
    dialog._populate_sections()
    _assert_compact(dialog.sections)
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected
    assert dialog.session.included == included


def test_cached_widths_fit_after_save_and_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """Restored calculator cells and long net names size from their saved text."""
    api = draft_api
    config = _config(api)
    board = _many_rows(api, long_last_net=True)
    saved: list[Config] = []
    dialog = api.dialog.ImpedanceDialog(
        None,
        replace(config, width_results=()),
        board,
        api.preview,
        lambda: board,
        load_width_results=lambda: config.width_results,
        save_config=saved.append,
    )
    _assert_compact(dialog.sections)
    widths = [column.GetWidth() for column in dialog.sections.dataview_columns]
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert saved

    reopened = _open(api, saved[-1], board)
    _assert_compact(reopened.sections)
    assert [
        column.GetWidth() for column in reopened.sections.dataview_columns
    ] == widths
