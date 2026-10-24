"""Spacing diagnostics through actual constructors and stateful wx events.

The controls retain the state read by production handlers. Native wx rendering
and event-loop behavior are exercised separately in the native test module.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- required by draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep unrelated catalog workers outside offline diagnostic assertions."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _config(api: SimpleNamespace, *, kind: str = "differential") -> Any:
    """Declare the known 0.200 mm gap independently of any width calculator."""
    config = _intent(api)
    specification = replace(
        config.specifications[0],
        kind=kind,
        target_ohms="90",
        layer_settings=(
            api.model.LayerSettings(
                "F.Cu",
                ("B.Cu",),
                200_000,
                250_000 if kind.endswith("coplanar") else None,
            ),
        ),
    )
    return replace(config, specifications=(specification,))


def _gap_board(api: SimpleNamespace, gap_nm: int) -> Any:
    """Move the real mate centerline while keeping widths and track IDs fixed."""
    board = _board(api)
    mate = board.traces[1]
    return replace(
        board,
        traces=(
            board.traces[0],
            replace(
                mate, points=((0, gap_nm + 150_000), (2_000_000, gap_nm + 150_000))
            ),
            *board.traces[2:],
        ),
    )


@pytest.mark.parametrize("kind", ["differential", "differential_coplanar"])
@pytest.mark.parametrize(
    ("gap_nm", "expected_gap"),
    [
        (198_999, "0.199 mm"),
        (199_000, "0.199 mm"),
        (200_000, "0.2 mm"),
        (201_000, "0.201 mm"),
        (201_001, "0.201 mm"),
        (203_201, "0.2032 mm"),
        (203_205, "0.20321 mm"),
    ],
)
def test_workbook_spacing_values_and_independent_mismatch_colour(
    draft_api: SimpleNamespace, kind: str, gap_nm: int, expected_gap: str
) -> None:
    """Real rows compare the routed gap with the declared layer spacing."""
    api = draft_api
    dialog = _open(api, _config(api, kind=kind), _gap_board(api, gap_nm))
    table = dialog.sections
    assert [table.GetColLabelValue(col) for col in range(11, 15)] == [
        "Pair spacing",
        "",
        "Target spacing",
        "Spacing check",
    ]
    mismatch = abs(gap_nm - 200_000) > 1_000
    for row in range(table.GetItemCount()):
        assert table.GetTextValue(row, 11) == expected_gap
        assert table.GetTextValue(row, 12) == ("!=" if mismatch else "")
        assert table.GetTextValue(row, 13) == "0.2 mm"
        assert table.GetTextValue(row, 14) == ("Deviation" if mismatch else "Match")
        for column in range(table.GetColumnCount()):
            expected = (
                (180, 140, 0)
                if mismatch and column in (11, 12, 13, 14)
                else table.GetDefaultCellTextColour()[:3]
            )
            assert table.GetCellTextColour(row, column)[:3] == expected


@pytest.mark.parametrize("kind", ["differential", "differential_coplanar"])
def test_workbook_spacing_refresh_repair_and_save_reopen(
    draft_api: SimpleNamespace, kind: str
) -> None:
    """Widths stay equal while the spacing status changes and survives reopening."""
    api = draft_api
    config = _config(api, kind=kind)
    board = _gap_board(api, 200_000)
    saved: list[Any] = []
    dialog = _open(api, config, board, saved.append)
    table = dialog.sections
    assert table.GetColLabelValue(11) == "Pair spacing"
    assert table.GetNumberCols() == 15
    assert table.GetColLabelValue(14) == "Spacing check"
    assert [table.GetTextValue(0, col) for col in range(11, 15)] == [
        "0.2 mm",
        "",
        "0.2 mm",
        "Match",
    ]
    assert table.GetTextValue(0, 7) == "0.15 mm"
    assert "Routed spacing matches" in dialog.spacing_summary.GetValue()
    included = tuple(
        table.GetToggleValue(row, 0) for row in range(table.GetItemCount())
    )
    table.SelectRow(0, column=11)
    table.Scroll(3, 0)

    changed = _gap_board(api, 250_000)
    assert dialog._refresh_rows(changed)
    assert [table.GetTextValue(0, col) for col in range(11, 15)] == [
        "0.25 mm",
        "!=",
        "0.2 mm",
        "Deviation",
    ]
    assert "routed spacing issue" in dialog.spacing_summary.GetValue()
    assert table.GetCellTextColour(0, 11)[:3] == (180, 140, 0)
    assert table.GetGridCursorCol() == 11
    assert table.GetViewStart() == (3, 0)
    assert (
        tuple(table.GetToggleValue(row, 0) for row in range(table.GetItemCount()))
        == included
    )

    assert dialog._refresh_rows(board)
    assert [table.GetTextValue(0, col) for col in range(11, 15)] == [
        "0.2 mm",
        "",
        "0.2 mm",
        "Match",
    ]
    assert "routed spacing issue" not in dialog.spacing_summary.GetValue()
    assert table.GetCellTextColour(0, 11) == table.GetDefaultCellTextColour()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert saved
    reopened = _open(api, saved[-1], changed)
    assert [reopened.sections.GetTextValue(0, col) for col in range(11, 15)] == [
        "0.25 mm",
        "!=",
        "0.2 mm",
        "Deviation",
    ]


def test_editor_spacing_is_independent_of_incomplete_calculator_fields(
    draft_api: SimpleNamespace,
) -> None:
    """Missing references, target impedance, ground gap and stackup do not hide a gap."""
    api = draft_api
    specification = replace(
        _config(api, kind="differential_coplanar").specifications[0],
        target_ohms="",
        layer_settings=(api.model.LayerSettings("F.Cu", (), 200_000, None),),
    )
    dialog = api.dialog.SpecificationDialog(None, _board(api), specification)
    assert "Routed spacing matches" in dialog.spacing_comparison.GetLabel()
    assert "0.2" in dialog.spacing_comparison.GetLabel()

    dialog.spacing.SetValue("0.15")
    dialog.spacing.emit(api.wx.EVT_TEXT)
    assert "routed spacing issue" in dialog.spacing_comparison.GetLabel()
    warning = dialog.spacing_comparison.GetForegroundColour()
    assert warning != dialog._spacing_text_colour

    dialog.spacing.SetValue("")
    dialog.spacing.emit(api.wx.EVT_TEXT)
    assert "Needs target" in dialog.spacing_comparison.GetLabel()
    assert "routed spacing issue" not in dialog.spacing_comparison.GetLabel()

    dialog.spacing.SetValue("invalid")
    dialog.spacing.emit(api.wx.EVT_TEXT)
    assert "valid" in dialog.spacing_comparison.GetLabel().lower()
    assert "Routed spacing matches" not in dialog.spacing_comparison.GetLabel()

    dialog.spacing.SetValue("0.2")
    dialog.spacing.emit(api.wx.EVT_TEXT)
    assert "Routed spacing matches" in dialog.spacing_comparison.GetLabel()
    assert (
        dialog.spacing_comparison.GetForegroundColour() == dialog._spacing_text_colour
    )
    dialog.kind.SetSelection(0)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert "Routed spacing matches" not in dialog.spacing_comparison.GetLabel()
    assert (
        dialog.spacing_comparison.GetForegroundColour() == dialog._spacing_text_colour
    )


@pytest.mark.parametrize("kind", ["single_ended", "single_ended_coplanar"])
def test_single_ended_workbook_has_no_spacing_verdict(
    draft_api: SimpleNamespace,
    kind: str,
) -> None:
    """The added column never invents differential semantics for single-ended rows."""
    api = draft_api
    dialog = _open(api, _config(api, kind=kind), _board(api))
    assert dialog.sections.GetColLabelValue(11) == "Pair spacing"
    assert all(row[11:15] == ["", "", "", ""] for row in dialog.sections.table_rows)
    assert dialog.spacing_summary.GetValue() == ""
    assert not dialog.spacing_summary.IsShown()


def test_unsupported_spacing_geometry_is_visible_and_never_matches(
    draft_api: SimpleNamespace,
) -> None:
    """A readable legacy arc without native midpoint cannot get a green result."""
    api = draft_api
    board = _board(api)
    unsupported = replace(
        board,
        traces=(replace(board.traces[0], is_native_arc=True), *board.traces[1:]),
    )
    dialog = _open(api, _config(api), unsupported)
    assert dialog.sections.GetItemCount() == 1
    assert dialog.sections.GetTextValue(0, 14) != "Match"
    assert dialog.sections.GetTextValue(0, 12) == ""
    assert dialog.sections.GetCellTextColour(0, 14)[:3] == (180, 140, 0)
    assert dialog.spacing_summary.IsShown()
    check = next(iter(dialog._spacing_by_section.values()))
    assert check.verdict != "match"
    assert check.coverage != "complete"


def test_spacing_edits_reuse_measurement_and_snapshot_edits_replace_it(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target typing only compares cached geometry; refreshing the board measures again."""
    api = draft_api
    spacing = importlib.import_module(api.dialog.__package__ + ".spacing_checks")
    measurements: list[Any] = []
    measure = spacing.measure_pair_spacing

    def observe(snapshot: Any, pair: tuple[str, str], layer: str, **kwargs: Any) -> Any:
        """Count actual geometry entry points while retaining the production result."""
        measurements.append(snapshot)
        return measure(snapshot, pair, layer, **kwargs)

    monkeypatch.setattr(spacing, "measure_pair_spacing", observe)
    board = _board(api)
    specification = _config(api).specifications[0]
    editor = api.dialog.SpecificationDialog(None, board, specification)
    assert measurements == [board]
    for value in ("0.15", "0.2", "", "0.3", "0.2"):
        editor.spacing.SetValue(value)
        editor.spacing.emit(api.wx.EVT_TEXT)
    assert measurements == [board]
    dialog = _open(api, _config(api), board)
    assert measurements == [board, board]
    dialog._populate_sections()
    assert measurements == [board, board]
    changed = _gap_board(api, 250_000)
    assert dialog._refresh_rows(changed)
    assert measurements == [board, board, changed]


def test_editor_updates_spacing_concerns_and_clears_invalid_selections(
    draft_api: SimpleNamespace,
) -> None:
    """Target and type edits replace diagnostic entries without retaining old concerns."""
    api = draft_api
    editor = api.dialog.SpecificationDialog(
        None, _gap_board(api, 250_000), _config(api).specifications[0]
    )
    assert editor._spacing_by_section
    assert editor.preview_pane.spacing_entries
    assert any(
        check.verdict == "deviation" for check in editor._spacing_by_section.values()
    )

    editor.spacing.SetValue("invalid")
    editor.spacing.emit(api.wx.EVT_TEXT)
    assert editor._spacing_by_section == {}
    assert editor.preview_pane.spacing_entries == ()

    editor.spacing.SetValue("0.25")
    editor.spacing.emit(api.wx.EVT_TEXT)
    assert editor._spacing_by_section
    assert all(
        check.verdict == "match" for check in editor._spacing_by_section.values()
    )
    assert editor.preview_pane.spacing_entries == ()

    editor.spacing.SetValue("0.2")
    editor.spacing.emit(api.wx.EVT_TEXT)
    assert editor.preview_pane.spacing_entries
    editor.kind.SetSelection(0)
    editor.kind.emit(api.wx.EVT_CHOICE)
    assert editor._spacing_by_section == {}
    assert editor.preview_pane.spacing_entries == ()


def test_workbook_refresh_replaces_spacing_concerns_and_row_verdicts(
    draft_api: SimpleNamespace,
) -> None:
    """The diagnostic entries and row verdicts follow the same measured snapshot."""
    api = draft_api
    dialog = _open(api, _config(api), _gap_board(api, 250_000))
    assert dialog.preview_pane.spacing_entries
    assert dialog._refresh_rows(_gap_board(api, 200_000))
    assert dialog.preview_pane.spacing_entries == ()
    assert all(
        check.verdict == "match" for check in dialog._spacing_by_section.values()
    )


def test_partial_spacing_summary_keeps_known_issue_and_identifies_coverage_gap(
    draft_api: SimpleNamespace,
) -> None:
    """Known routing defects remain locatable when a separate arc is unsupported."""
    api = draft_api
    board = _gap_board(api, 250_000)
    unsupported = replace(
        board.traces[0],
        trace_id="unsupported",
        points=((10_000_000, 0), (12_000_000, 0)),
        is_native_arc=True,
    )
    board = replace(board, traces=(*board.traces, unsupported))
    capture = replace(
        api.capture, viewport_nm=(-300_000, -300_000, 13_000_000, 6_825_000)
    )
    editor = api.dialog.SpecificationDialog(
        None,
        board,
        _config(api).specifications[0],
        preview=lambda *_args, **_kwargs: capture,
    )
    assert "routed spacing issue" in editor.spacing_comparison.GetLabel()
    assert "partial" in editor.spacing_comparison.GetLabel().lower()
    editor.on_screen = True
    api.drain()
    assert editor.preview_pane.spacing_entries
    entries = editor.preview_pane.spacing_entries
    assert editor.preview_pane._capture.data == capture.data
    assert all(item.caption for item in editor.preview_pane.spacing_entries)
    editor.spacing.SetValue("0.25")
    editor.spacing.emit(api.wx.EVT_TEXT)
    assert "Unverified" in editor.spacing_comparison.GetLabel()
    assert editor.preview_pane.spacing_entries == ()
    editor.spacing.SetValue("0.2")
    editor.spacing.emit(api.wx.EVT_TEXT)
    assert "partial" in editor.spacing_comparison.GetLabel().lower()
    assert editor.preview_pane.spacing_entries
    assert editor.preview_pane.spacing_entries == entries


def test_spacing_display_precision_survives_refresh_and_save_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """Formatting leaves the exact saved target and refreshed geometry intact."""
    api = draft_api
    config = _config(api)
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=203_201),),
            ),
        ),
    )
    saved: list[Any] = []
    dialog = _open(api, config, _gap_board(api, 203_201), saved.append)
    assert dialog.sections.GetTextValue(0, 11) == "0.2032 mm"
    assert dialog.sections.GetTextValue(0, 13) == "0.2032 mm"
    changed = _gap_board(api, 203_205)
    assert dialog._refresh_rows(changed)
    assert dialog.sections.GetTextValue(0, 11) == "0.20321 mm"
    assert dialog.sections.GetTextValue(0, 13) == "0.2032 mm"
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert saved
    persisted = api.model.Config.from_dict(saved[-1].to_dict())
    assert persisted.specifications[0].layer_settings[0].spacing_nm == 203_201
    reopened = _open(api, persisted, changed)
    assert reopened.sections.GetTextValue(0, 11) == "0.20321 mm"
    assert reopened.sections.GetTextValue(0, 13) == "0.2032 mm"
    assert reopened.sections.GetTextValue(0, 14) == "Match"
