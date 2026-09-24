"""Keep checked workbook rows together without changing section identities."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from .test_impedance_automatic_rows import _show
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import BoardSnapshot, Config

draft_api = _draft_api


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep row-order workflows independent of catalog requests."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _mixed_board(api: SimpleNamespace) -> BoardSnapshot:
    """Interleave two covered stubs among four independent routed sections."""
    return replace(
        _board(api),
        traces=tuple(
            api.model.Trace(
                f"trace-{index}",
                "F.Cu",
                f"SIGNAL_{index}",
                150_000,
                ((0, index * 5_000_000), (100_000, index * 5_000_000)),
            )
            for index in range(6)
        ),
        net_class_memberships=tuple((f"SIGNAL_{index}", ("RF",)) for index in range(6)),
        differential_pairs=(),
        copper_features=tuple(
            api.model.CopperFeature(
                f"pad-{index}",
                f"SIGNAL_{index}",
                "F.Cu",
                "pad",
                "rect",
                (0, index * 5_000_000),
                1_000_000,
                1_000_000,
            )
            for index in (0, 3)
        ),
    )


def _assert_order(dialog: ImpedanceDialog, source_indexes: tuple[int, ...]) -> None:
    """Check section identity, stable row numbers, and each visible checkbox."""
    analysis = dialog.session.analysis
    assert analysis is not None
    assert dialog._row_ids == tuple(
        analysis.sections[index].section_id for index in source_indexes
    )
    assert [
        dialog.sections.GetTextValue(row, 1) for row in range(len(source_indexes))
    ] == [str(index + 1) for index in source_indexes]
    checked = [
        dialog.sections.GetToggleValue(row, 0) for row in range(len(source_indexes))
    ]
    assert checked == sorted(checked, reverse=True)
    assert all(
        checked[row] == (section_id in dialog.session.included)
        for row, section_id in enumerate(dialog._row_ids)
    )


def _click(dialog: ImpedanceDialog, row: int, column: int = 0) -> None:
    """Dispatch through the production grid click handler without draining queues."""
    dialog.sections._on_cell_click(
        SimpleNamespace(GetRow=lambda: row, GetCol=lambda: column, Skip=lambda: None)
    )


def test_open_groups_checked_rows_before_interleaved_stubs(
    draft_api: SimpleNamespace,
) -> None:
    """The initially displayed rows put both unchecked covered stubs last."""
    api = draft_api
    dialog = _open(api, _intent(api), _mixed_board(api))
    analysis = dialog.session.analysis
    assert analysis is not None
    assert [section.exclusion_reason for section in analysis.sections] == [
        "covered_stub",
        "",
        "",
        "covered_stub",
        "",
        "",
    ]
    _assert_order(dialog, (1, 2, 4, 5, 0, 3))
    assert dialog.preview_pane.sections == analysis.sections
    assert (
        dialog.preview_pane.sections[dialog.preview_pane._index].section_id
        == (dialog._row_ids[dialog.sections.GetSelectedRow()])
    )


@pytest.mark.parametrize("include_all", [False, True])
def test_uniform_inclusion_uses_canonical_order(
    draft_api: SimpleNamespace, include_all: bool
) -> None:
    """All-checked and all-unchecked tables retain every candidate in source order."""
    api = draft_api
    dialog = _open(api, _intent(api), _mixed_board(api))
    analysis = dialog.session.analysis
    assert analysis is not None
    original_digest = analysis.digest
    dialog.session.set_included(
        (section.section_id for section in analysis.sections) if include_all else ()
    )
    dialog._populate_sections()
    _assert_order(dialog, tuple(range(6)))
    assert dialog.session.analysis is analysis
    assert dialog.session.analysis.digest == original_digest


def test_checkbox_reordering_retains_logical_selection_and_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Unchecking and rechecking the moved row operate on its own section."""
    api = draft_api
    dialog = _open(api, _intent(api), _mixed_board(api))
    _show(api, dialog)
    analysis = dialog.session.analysis
    assert analysis is not None
    moved = analysis.sections[1]
    _click(dialog, 0)
    assert moved.section_id not in dialog.session.included
    api.drain()
    api.paint(dialog.preview_pane)
    _assert_order(dialog, (2, 4, 5, 0, 1, 3))
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == moved.section_id
    assert dialog.sections.GetGridCursorCol() == 0
    assert api.preview_calls[-1] == moved

    _click(dialog, dialog.sections.GetSelectedRow())
    assert moved.section_id in dialog.session.included
    api.drain()
    api.paint(dialog.preview_pane)
    _assert_order(dialog, (1, 2, 4, 5, 0, 3))
    assert dialog.sections.GetSelectedRow() == 0
    assert api.preview_calls[-1] == moved


def test_rapid_toggles_commit_visible_ids_before_deferred_sort(
    draft_api: SimpleNamespace,
) -> None:
    """Several clicks before CallAfter runs commit exactly those visible sections."""
    api = draft_api
    dialog = _open(api, _intent(api), _mixed_board(api))
    analysis = dialog.session.analysis
    assert analysis is not None
    _click(dialog, 0)
    _click(dialog, 1)
    _click(dialog, 4)
    assert dialog.session.included == {
        analysis.sections[index].section_id for index in (0, 4, 5)
    }
    api.drain()
    _assert_order(dialog, (0, 4, 5, 1, 2, 3))
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == (
        analysis.sections[0].section_id
    )


def test_preview_and_forced_retry_resolve_sorted_row_identity(
    draft_api: SimpleNamespace,
) -> None:
    """Selecting a displayed row and retrying it render the same canonical section."""
    api = draft_api
    dialog = _open(api, _intent(api), _mixed_board(api))
    _show(api, dialog)
    selected = dialog.session.analysis.sections[4]
    _click(dialog, 2, 6)
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls[-1] == selected
    assert dialog.sections.GetGridCursorCol() == 6
    dialog._on_retry_rows(SimpleNamespace())
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls[-1] == selected
    assert dialog.preview_pane.sections[dialog.preview_pane._index] == selected
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected.section_id


def test_approved_subset_reopens_in_checked_first_order(
    draft_api: SimpleNamespace,
) -> None:
    """Approved persisted IDs restore grouping without changing source numbering."""
    api = draft_api
    board = _mixed_board(api)
    saved: list[Config] = []
    dialog = _open(api, _intent(api), board, saved.append)
    _show(api, dialog)
    _click(dialog, 0)
    api.drain()
    api.paint(dialog.preview_pane)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    reopened = _open(api, api.model.Config.from_dict(saved[-1].to_dict()), board)
    _assert_order(reopened, (2, 4, 5, 0, 1, 3))
    assert reopened.session.approved
    assert reopened.session.included == dialog.session.included


def test_analysis_failure_clears_rows_and_retry_restores_grouping(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed scan leaves no stale sorted IDs for callbacks to mutate."""
    api = draft_api
    board = _mixed_board(api)
    dialog = _open(api, _intent(api), board)

    def fail_snapshot() -> BoardSnapshot:
        raise ValueError("Board is unavailable")

    monkeypatch.setattr(dialog, "refresh_snapshot", fail_snapshot)
    assert not dialog._refresh_rows()
    assert dialog._row_ids == ()
    assert dialog.sections.GetItemCount() == 0
    dialog._on_include(0)
    dialog._on_preview(0)
    assert dialog.session.included == set()
    assert dialog.preview_pane.sections == ()
    assert dialog._refresh_rows(board)
    _assert_order(dialog, (1, 2, 4, 5, 0, 3))
