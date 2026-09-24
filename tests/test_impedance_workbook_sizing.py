"""Grid sizing workflows measured with explicit header and body fonts.

The stateful wx harness checks sizing decisions and recovery. Native paint/input
coverage lives separately in test_impedance_workbook_sizing_native.py.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from .test_impedance_automatic_rows import _show
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_width_mismatch import _config

draft_api = _draft_api
COMPACT_COLUMNS = (0, 1, 4)

if TYPE_CHECKING:
    from impedance.model import BoardSnapshot, Config


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


def _content_width(api: SimpleNamespace, table: Any, column: int) -> int:
    """Measure actual label/cell fonts independently of the sizing implementation."""
    dc = api.wx.ClientDC(table)
    try:
        dc.SetFont(table.GetLabelFont())
        width = dc.GetTextExtent(table.GetColLabelValue(column))[0]
        if column == 0:
            glyph = api.wx.RendererNative.Get().GetCheckBoxSize(
                table, api.wx.CONTROL_CELL
            )
            return max(width, glyph.GetWidth())
        for row in range(table.GetNumberRows()):
            dc.SetFont(table.GetCellFont(row, column))
            width = max(width, dc.GetTextExtent(table.GetCellValue(row, column))[0])
        if column == 8:
            dc.SetFont(table.GetDefaultCellFont())
            width = max(width, dc.GetTextExtent("!=")[0])
        return width
    finally:
        del dc


def _assert_compact(api: SimpleNamespace, table: Any) -> None:
    """Fit complete current content with a single six-DIP clearance allowance."""
    assert table.GetNumberCols() == 11
    for column in range(table.GetNumberCols()):
        content = _content_width(api, table, column)
        allocated = table.GetColSize(column)
        assert content <= allocated <= content + table.FromDIP(6), (
            column,
            table.GetColLabelValue(column),
            content,
            allocated,
            "clipped content or excess clearance",
        )


def _widths(table: Any) -> tuple[int, ...]:
    """Read allocated grid widths without requesting any new sizing."""
    return tuple(table.GetColSize(column) for column in range(table.GetNumberCols()))


def _state(dialog: Any) -> tuple[object, ...]:
    """Observe report state and native selection independently of sizing state."""
    table = dialog.sections
    return (
        dialog.session.config,
        tuple(dialog._row_ids),
        tuple(table.GetSelectedRows()),
        table.GetGridCursorRow(),
        frozenset(dialog.session.included),
        tuple(table.GetToggleValue(row, 0) for row in range(table.GetNumberRows())),
    )


def _many_rows(
    api: SimpleNamespace, *, row_count: int = 40, long_last_net: bool = False
) -> BoardSnapshot:
    """Provide independent routes, with an optional long name beyond visible samples."""
    names = [f"N{index:04d}" for index in range(row_count)]
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


def test_constructor_uses_grid_renderers_for_compact_workbook_columns(
    draft_api: SimpleNamespace,
) -> None:
    """Workbook content must use Grid rendering rather than outline-column sizing."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    assert hasattr(dialog.sections, "GetNumberRows"), (
        "Workbook rows must use wx.Grid with renderer-aware column sizing"
    )
    import wx.grid as gridlib

    assert isinstance(dialog.sections, gridlib.Grid)
    assert not getattr(dialog.sections, "can_focus", True), (
        "Only the grid's cell window may own keyboard focus"
    )
    assert dialog.sections.GetNumberRows() == len(dialog._row_ids) == 2
    assert dialog.sections.GetRowLabelSize() == 0
    assert [dialog.sections.GetColLabelValue(column) for column in COMPACT_COLUMNS] == [
        "Incl.",
        "Row",
        "Target Ω",
    ]
    _assert_compact(api, dialog.sections)


def test_constructor_measures_populated_rows_once(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure actual initial content without first sizing the temporary empty grid."""
    api = draft_api
    grid_api = importlib.import_module(f"{api.dialog.__package__}.workbook_grid")
    fit_columns = grid_api.WorkbookGrid.fit_columns
    measured_rows: list[int] = []

    def observe_fit(table: Any) -> None:
        """Count complete sizing work, including header-only measurements."""
        measured_rows.append(table.GetNumberRows())
        fit_columns(table)

    monkeypatch.setattr(grid_api.WorkbookGrid, "fit_columns", observe_fit)
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.sections.GetNumberRows() == 2
    assert measured_rows == [2]
    _assert_compact(api, dialog.sections)


def test_window_font_does_not_replace_distinct_header_and_body_font_metrics(
    draft_api: SimpleNamespace,
) -> None:
    """An inflated parent/window font cannot justify blank native column space."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    table = dialog.sections
    window_font = api.wx.Font(table.GetFont())
    window_font.SetPointSize(36)
    body = api.wx.Font(table.GetDefaultCellFont())
    body.SetPointSize(11)
    header = api.wx.Font(table.GetLabelFont())
    header.SetPointSize(17)
    table.SetFont(window_font)
    table.SetDefaultCellFont(body)
    table.SetLabelFont(header)
    table.fit_columns()

    assert table.GetDefaultCellFont().GetPointSize() == 11
    assert table.GetLabelFont().GetPointSize() == 17
    _assert_compact(api, table)
    dc = api.wx.ClientDC(table)
    try:
        dc.SetFont(window_font)
        for column in COMPACT_COLUMNS:
            assert (
                table.GetColSize(column)
                < dc.GetTextExtent(table.GetColLabelValue(column))[0]
            )
    finally:
        del dc


def test_native_checkbox_metric_can_exceed_both_label_and_body_text(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large native checkbox must fit without measuring boolean strings."""
    api = draft_api
    requests: list[tuple[object, int]] = []

    def checkbox_size(window: object, flags: int) -> SimpleNamespace:
        """Keep native glyph width independent of text metrics."""
        requests.append((window, flags))
        return SimpleNamespace(
            width=72, height=18, GetWidth=lambda: 72, GetHeight=lambda: 18
        )

    renderer = SimpleNamespace(GetCheckBoxSize=checkbox_size)
    monkeypatch.setattr(api.wx, "RendererNative", SimpleNamespace(Get=lambda: renderer))
    dialog = _open(api, _intent(api), _board(api))
    table = dialog.sections
    _assert_compact(api, table)
    assert 72 <= table.GetColSize(0) <= 72 + table.FromDIP(6)
    assert (table, api.wx.CONTROL_CELL) in requests


def test_row_number_column_grows_and_shrinks_across_digit_boundaries(
    draft_api: SimpleNamespace,
) -> None:
    """Row one thousand must fit while returning to nine rows releases its width."""
    api = draft_api
    small = _many_rows(api, row_count=9)
    dialog = _open(api, _intent(api), small)
    table = dialog.sections
    _assert_compact(api, table)
    short_width = table.GetColSize(1)

    assert dialog._refresh_rows(_many_rows(api, row_count=1000))
    assert table.GetNumberRows() == 1000
    assert table.GetCellValue(999, 1) == "1000"
    _assert_compact(api, table)
    assert table.GetColSize(1) > short_width

    assert dialog._refresh_rows(small)
    _assert_compact(api, table)
    assert table.GetColSize(1) == short_width


def test_resistance_column_shrinks_after_shorter_values_empty_rows_and_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """Resistance content, distinct from nominal width, controls column four."""
    api = draft_api
    original = _intent(api)
    long_target = "90.123456789012345"
    config = replace(
        original,
        specifications=(replace(original.specifications[0], target_ohms=long_target),),
    )
    current = [_board(api)]
    saved: list[Config] = []
    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        current[0],
        api.preview,
        lambda: current[0],
        save_config=saved.append,
    )
    table = dialog.sections
    assert table.GetCellValue(0, 4) == long_target
    _assert_compact(api, table)
    long_width = table.GetColSize(4)

    dialog.session.replace_specifications(original.specifications)
    assert dialog._refresh_rows()
    assert table.GetCellValue(0, 4) == "50"
    _assert_compact(api, table)
    short_width = table.GetColSize(4)
    assert short_width < long_width

    current[0] = replace(current[0], traces=())
    assert dialog._refresh_rows()
    assert table.GetNumberRows() == 0
    _assert_compact(api, table)
    assert table.GetColSize(4) == short_width
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    restored = api.model.Config.from_dict(saved[-1].to_dict())
    reopened = _open(api, restored, current[0])
    _assert_compact(api, reopened.sections)
    assert reopened.sections.GetColSize(4) == short_width
    assert restored.specifications[0].target_ohms == "50"


def test_refresh_fits_offscreen_long_net_and_then_shrinks_with_short_content(
    draft_api: SimpleNamespace,
) -> None:
    """The longest current label beyond the first rows participates in sizing."""
    api = draft_api
    short = _many_rows(api)
    long = _many_rows(api, long_last_net=True)
    dialog = _open(api, _intent(api), short)
    table = dialog.sections
    short_width = table.GetColSize(6)
    assert dialog._refresh_rows(long)
    assert len(table.GetCellValue(39, 6)) > 50
    _assert_compact(api, table)
    assert table.GetColSize(6) > short_width
    assert dialog._refresh_rows(short)
    _assert_compact(api, table)
    assert table.GetColSize(6) == short_width


def test_font_roundtrip_resize_and_dpi_events_preserve_workbook_state(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sizing follows current fonts without changing inclusion, selection or approval."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)
    table = dialog.sections
    table.SelectRow(1)
    table.SetGridCursor(1, 0)
    dialog._on_preview(1)
    api.drain()
    api.paint(dialog.preview_pane)
    table.SetToggleValue(False, 0, 0)
    dialog._on_include(0)
    api.drain()
    api.paint(dialog.preview_pane)
    original_state = _state(dialog)
    body = api.wx.Font(table.GetDefaultCellFont())
    header = api.wx.Font(table.GetLabelFont())
    original_widths = _widths(table)

    for scale in (2, 1):
        table.SetDefaultCellFont(body.Scaled(scale))
        table.SetLabelFont(header.Scaled(scale))
        table.fit_columns()
        _assert_compact(api, table)
        assert _state(dialog) == original_state
        if scale == 2:
            assert all(
                table.GetColSize(column) > original_widths[column]
                for column in COMPACT_COLUMNS
            )
        else:
            assert _widths(table) == original_widths

    measurements = len(table.auto_size_requests)
    table.SetSize((780, 320))
    size_handler = table.bindings.get((api.wx.EVT_SIZE, -1))
    if size_handler is not None:
        size_handler(SimpleNamespace(GetEventObject=lambda: table, Skip=lambda: None))
    api.drain()
    _assert_compact(api, table)
    assert _widths(table) == original_widths
    assert len(table.auto_size_requests) == measurements, (
        "Viewport size changes must not remeasure unchanged workbook content"
    )
    original_from_dip = table.FromDIP
    with monkeypatch.context() as patch:
        patch.setattr(table, "FromDIP", lambda value: original_from_dip(value) * 2)
        table._on_metrics_changed(SimpleNamespace(Skip=lambda: None))
        api.drain()
        _assert_compact(api, table)
        assert all(
            table.GetColSize(column) > original_widths[column]
            for column in COMPACT_COLUMNS
        )
        assert _state(dialog) == original_state
    table._on_metrics_changed(SimpleNamespace(Skip=lambda: None))
    api.drain()
    _assert_compact(api, table)
    assert _widths(table) == original_widths
    assert _state(dialog) == original_state


def test_autosize_failure_restores_labels_and_batch_before_retry(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed native measurement cannot leave hidden headers or an open batch."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    table = dialog.sections
    labels = tuple(table.GetColLabelValue(column) for column in range(11))
    state = _state(dialog)

    def fail(*_args: Any, **_options: Any) -> None:
        """Fail at the native autosizer after measurement mode has been entered."""
        raise RuntimeError("Native grid measurement failed")

    with monkeypatch.context() as patch:
        patch.setattr(table, "AutoSizeColumns", fail)
        with pytest.raises(RuntimeError, match="Native grid measurement failed"):
            table.fit_columns()
    assert not table.GetTable().measuring_cells
    assert table.GetBatchCount() == 0
    assert tuple(table.GetColLabelValue(column) for column in range(11)) == labels
    assert _state(dialog) == state
    table.fit_columns()
    _assert_compact(api, table)


@pytest.mark.parametrize(
    "refresh_before_save", [False, True], ids=["save", "refresh-save"]
)
def test_checkbox_click_commits_before_immediate_approval_and_save(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    refresh_before_save: bool,
) -> None:
    """Queued row painting cannot lose a click to an immediate approval/save action."""
    api = draft_api
    saved: list[Config] = []
    dialog = _open(api, _intent(api), _board(api), saved.append)
    _show(api, dialog)
    table = dialog.sections
    removed, retained = tuple(dialog._row_ids)
    table._on_cell_click(
        SimpleNamespace(GetRow=lambda: 0, GetCol=lambda: 0, Skip=lambda: None)
    )
    assert not table.GetToggleValue(0, 0)
    assert dialog.session.included == {retained}
    assert removed not in dialog.session.included
    assert dialog._workbook_inclusion_refresh_queued
    if refresh_before_save:
        assert dialog._refresh_rows()
        assert not table.GetToggleValue(dialog._row_ids.index(removed), 0)
        assert dialog.session.included == {retained}
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert saved[-1].included_section_ids == (retained,)

    def no_closed_reprojection(*_args: Any, **_options: Any) -> None:
        """Reject queued inclusion work rebuilding a closed native grid."""
        pytest.fail("Closed workbook rows were rebuilt by queued inclusion work")

    monkeypatch.setattr(dialog, "_populate_sections", no_closed_reprojection)
    api.drain()
    assert not dialog._workbook_inclusion_refresh_queued
    assert saved[-1].included_section_ids == (retained,)


def test_calculation_refresh_resizes_pending_and_numeric_width_targets(
    draft_api: SimpleNamespace,
) -> None:
    """Nominal-width text updates independently of desired resistance."""
    api = draft_api
    board = _board(api)
    pending = _config(api, state="pending")
    numeric = _config(api, target_width_nm=123_456_789)
    dialog = _open(api, pending, board)
    table = dialog.sections
    pending_width = table.GetColSize(9)
    resistance_width = table.GetColSize(4)
    for config in (numeric, pending):
        dialog.session.replace_calculation_context(config.stackup, config.width_results)
        assert dialog._refresh_rows(board)
        _assert_compact(api, table)
        assert table.GetColSize(4) == resistance_width
        if config is numeric:
            assert table.GetColSize(9) > pending_width
        else:
            assert table.GetColSize(9) == pending_width


def test_empty_and_failed_rows_keep_headers_and_reserved_marker_compact(
    draft_api: SimpleNamespace,
) -> None:
    """Empty/failure states release old wide values while keeping usable headers."""
    api = draft_api
    empty = _open(api, api.model.Config(), _board(api))
    assert empty.sections.GetNumberRows() == 0
    _assert_compact(api, empty.sections)
    dialog = _open(api, _intent(api), _many_rows(api, long_last_net=True))
    long_width = dialog.sections.GetColSize(6)
    dialog._fail_rows("The board could not be read.")
    assert dialog.sections.GetNumberRows() == 0
    _assert_compact(api, dialog.sections)
    assert dialog.sections.GetColSize(6) < long_width


def test_cached_widths_fit_after_save_and_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """Restored nominal widths and long names size from their current rendered text."""
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
    _assert_compact(api, dialog.sections)
    widths = _widths(dialog.sections)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    reopened = _open(api, saved[-1], board)
    _assert_compact(api, reopened.sections)
    assert _widths(reopened.sections) == widths
