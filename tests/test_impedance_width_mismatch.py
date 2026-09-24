"""Workbook mismatch cues through the real dialog and stateful control harness.

Cell attributes are model assertions; native selection and drawing are checked
separately by the wx smoke workflow.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from .test_impedance_automatic_rows import _cached_success, _calculator_stackup
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import Config


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the constructor independent of unrelated catalog network workers."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _config(
    api: SimpleNamespace, *, target_width_nm: int = 181_400, state: str = "success"
) -> Config:
    """Supply persisted calculator results for the supported RF specification."""
    stackup = _calculator_stackup(api)
    config = replace(_intent(api), stackup=stackup)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    result = replace(
        _cached_success(model, stackup, config.specifications[0], "F.Cu"),
        target_width_nm=target_width_nm,
    )
    if state == "missing":
        return config
    if state == "stale":
        result = replace(result, input_digest="9" * 64)
    elif state != "success":
        result = replace(result, status=state, target_width_nm=None)
    return replace(config, width_results=(result,))


def _assert_cell_colour(
    api: SimpleNamespace,
    dialog: ImpedanceDialog,
    row: int,
    column: int,
    colour: Optional[tuple[int, int, int]],
) -> None:
    """Check only an intentional override, leaving ordinary cells native."""
    attr = api.dv.DataViewItemAttr()
    applied = dialog.sections.GetModel().GetAttrByRow(row, column, attr)
    assert applied is (colour is not None)
    assert attr.background is None
    if colour is None:
        assert attr.colour is None
    else:
        assert attr.colour[:3] == colour


def _assert_regular_rows(
    api: SimpleNamespace, dialog: ImpedanceDialog, *, mismatch: bool
) -> None:
    """Check marker placement and the exact scope of foreground overrides."""
    rows = dialog.sections.table_rows
    assert rows
    # Keep this first: the old table must fail for its absent mismatch marker,
    # rather than because the future cell-attribute model does not exist yet.
    assert [row[8] for row in rows] == ["!=" if mismatch else ""] * len(rows)
    assert [row[1] for row in rows] == [str(i + 1) for i in range(len(rows))]
    for index, row in enumerate(rows):
        assert row[7] == "0.15 mm"
        assert len(row) == dialog.sections.GetColumnCount()
        if len(row) == 12:
            assert row[11] == ""
        for column in range(11):
            colour = (180, 140, 0) if mismatch and column in (7, 8, 9) else None
            _assert_cell_colour(api, dialog, index, column, colour)


def test_numeric_mismatch_colours_the_two_width_values_and_marker(
    draft_api: SimpleNamespace,
) -> None:
    """Amber != joins amber Width and Target width without tinting other cells."""
    api = draft_api
    dialog = _open(api, _config(api), _board(api))

    _assert_regular_rows(api, dialog, mismatch=True)
    assert all(row[9] == "0.1814 mm" for row in dialog.sections.table_rows)


@pytest.mark.parametrize(
    "delta_nm", [-10_001, -10_000, -9_999, 0, 9_999, 10_000, 10_001]
)
def test_mismatch_preserves_strict_absolute_point_zero_one_mm_threshold(
    draft_api: SimpleNamespace, delta_nm: int
) -> None:
    """Both boundary directions use > 0.01 mm, including exact equality."""
    api = draft_api
    dialog = _open(api, _config(api, target_width_nm=150_000 + delta_nm), _board(api))

    _assert_regular_rows(api, dialog, mismatch=abs(delta_nm) > 10_000)


@pytest.mark.parametrize(
    ("state", "target_text"),
    [
        ("pending", "Pending"),
        ("stale", "Pending"),
        ("error", "Not calculated"),
        ("unsupported", "Not calculated"),
        ("missing", "Pending"),
    ],
)
def test_non_numeric_or_stale_target_has_no_mismatch_cue(
    draft_api: SimpleNamespace, state: str, target_text: str
) -> None:
    """Unavailable targets cannot claim that a routed width mismatches."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _config(api), board)
    _assert_regular_rows(api, dialog, mismatch=True)

    unavailable = _config(api, state=state)
    dialog.session.replace_calculation_context(
        unavailable.stackup, unavailable.width_results
    )
    assert dialog._refresh_rows(board)

    _assert_regular_rows(api, dialog, mismatch=False)
    assert all(row[9] == target_text for row in dialog.sections.table_rows)


def test_refresh_removes_and_restores_marker_and_width_colours(
    draft_api: SimpleNamespace,
) -> None:
    """Repopulating the same dialog clears old attributes as results change."""
    api = draft_api
    config = _config(api)
    board = _board(api)
    dialog = _open(api, config, board)
    _assert_regular_rows(api, dialog, mismatch=True)
    original_ids = dialog._row_ids
    original_included = set(dialog.session.included)

    matched = _config(api, target_width_nm=150_000)
    dialog.session.replace_calculation_context(matched.stackup, matched.width_results)
    assert dialog._refresh_rows(board)
    _assert_regular_rows(api, dialog, mismatch=False)

    dialog.session.replace_calculation_context(config.stackup, config.width_results)
    assert dialog._refresh_rows(board)
    _assert_regular_rows(api, dialog, mismatch=True)
    assert dialog._row_ids == original_ids
    assert dialog.session.included == original_included


def test_cached_mismatch_survives_save_and_reopen(
    draft_api: SimpleNamespace,
) -> None:
    """The cue derives from saved current results without a new calculation."""
    api = draft_api
    config = _config(api)
    board = _board(api)
    saved: list[Config] = []
    # Exercise the production cache hydration path in the constructor.
    cached = api.dialog.ImpedanceDialog(
        None,
        replace(config, width_results=()),
        board,
        api.preview,
        lambda: board,
        save_config=saved.append,
        load_width_results=lambda: config.width_results,
    )
    _assert_regular_rows(api, cached, mismatch=True)
    cached.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)

    assert saved
    assert saved[-1].width_results == config.width_results
    reopened = _open(api, saved[-1], board)
    _assert_regular_rows(api, reopened, mismatch=True)


def test_covered_stub_keeps_muted_cells_and_blank_comparison_when_included(
    draft_api: SimpleNamespace,
) -> None:
    """Covered stubs retain their presentation even when the nominal differs."""
    api = draft_api
    stub = api.model.Trace("stub", "F.Cu", "CLK_P", 150_000, ((0, 0), (100_000, 0)))
    run = api.model.Trace("run", "F.Cu", "CLK_N", 150_000, ((0, 0), (2_500_000, 0)))
    pad = api.model.CopperFeature(
        "pad-1", "CLK_P", "F.Cu", "pad", "circle", (0, 0), 400_000, 400_000
    )
    board = replace(
        _board(api),
        traces=(stub, run),
        copper_features=(pad,),
        differential_pairs=(),
    )
    dialog = _open(api, _config(api), board)
    assert dialog.session.analysis is not None
    index = next(
        i
        for i, section in enumerate(dialog.session.analysis.sections)
        if section.exclusion_reason == "covered_stub"
    )
    for include in (False, True):
        if include:
            dialog.sections.SetToggleValue(True, index, 0)
            dialog.sections.emit(api.dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, row=index)
        assert dialog.sections.GetToggleValue(index, 0) is include
        row = dialog.sections.table_rows[index]
        assert row[8:10] == ["", ""]
        for column in range(11):
            _assert_cell_colour(api, dialog, index, column, (120, 120, 120))
