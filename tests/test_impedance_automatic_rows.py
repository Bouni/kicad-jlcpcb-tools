"""Automatically discover workbook rows without implicitly approving them.

These regressions exercise the production session, constructor, and bound handlers
with explicit stateful wx visibility and CallAfter dispatch; they do not assert
native layout or rendering.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from .test_impedance_dialog import _LayoutDouble
from .test_impedance_draft_dialog import (
    _approved,
    _board,
    _intent,
    _open,
    _stackup,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

# Explicitly register the shared fixture without an unused import shadowed by
# pytest's same-named injection parameters.
draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import Config, Specification
    from impedance.service import CapturedImage


def _review_cell_attr(
    api: SimpleNamespace,
    dialog: ImpedanceDialog,
    row: int,
    *,
    column: int = 0,
) -> object:
    """Apply GetAttrByRow for one specifications cell and return the attr double."""
    attr = api.dv.DataViewItemAttr()
    applied = dialog.specifications.GetModel().GetAttrByRow(row, column, attr)
    assert applied is (column == 0)
    return attr


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep workbook workflow cases independent of the catalog's network worker."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _show(api: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Deliver first native show before dispatching deferred preview callbacks."""
    dialog.on_screen = True
    skipped: list[bool] = []
    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog,
            IsShown=lambda: True,
            Skip=lambda: skipped.append(True),
        )
    )
    assert skipped == [True]
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    if dialog._capture_restore_pending:
        api.finish_preparation(dialog)


def _accepted_specification(
    api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    specification: Specification,
    *,
    width_results: tuple = (),
    modal_result: Optional[int] = None,
    review_tracking: object = None,
) -> None:
    """Accept an immutable child result while retaining its real parent handlers."""
    if modal_result is None:
        modal_result = api.wx.ID_OK

    class AcceptedSpecification:
        def __init__(
            self, parent: ImpedanceDialog, *_args: object, **_kwargs: object
        ) -> None:
            self.specification = specification if modal_result == api.wx.ID_OK else None
            tracking = review_tracking
            if tracking is None:
                tracking = parent.session.config.review_tracking
            self.review_tracking = tracking
            self.width_results = width_results

        def ShowModal(self) -> int:
            return modal_result

        def Destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(api.dialog, "SpecificationDialog", AcceptedSpecification)


def _current_layer_tracking(
    api: SimpleNamespace, config: Config, snapshot: object
) -> object:
    """Build ImageView/LayerApproval records that match the current board and captures."""
    package = api.dialog.__name__.rsplit(".", 1)[0]
    tracking = sys.modules[f"{package}.review_tracking"]
    matching = sys.modules[f"{package}.matching"]
    analysis = matching.analyze(config, snapshot)
    source = tracking.source_fingerprint(snapshot)
    image_sha256 = api.capture.sha256
    timestamp = tracking.utc_now()
    images = []
    layers = []
    for spec in config.specifications:
        captures_by_layer: dict[str, list[tuple[str, str]]] = {}
        for section in analysis.sections:
            if section.spec_id != spec.spec_id or section.exclusion_reason:
                continue
            digest = tracking.capture_fingerprint(source, spec, section, image_sha256)
            images.append(
                tracking.ImageView(
                    spec.spec_id,
                    section.layer,
                    section.section_id,
                    digest,
                    timestamp,
                )
            )
            captures_by_layer.setdefault(section.layer, []).append(
                (section.section_id, digest)
            )
        for layer, captures in captures_by_layer.items():
            layer_sections = tuple(
                section
                for section in analysis.sections
                if section.spec_id == spec.spec_id and section.layer == layer
            )
            layers.append(
                tracking.LayerApproval(
                    spec.spec_id,
                    layer,
                    tracking.layer_fingerprint(
                        source,
                        spec,
                        layer,
                        layer_sections,
                        stackup=config.stackup,
                        width_results=config.width_results,
                    ),
                    timestamp,
                    tuple(sorted(captures)),
                )
            )
    return tracking.ReviewTracking(tuple(images), tuple(layers))


def test_edit_review_button_label(draft_api: SimpleNamespace) -> None:
    """The specs toolbar names the dual edit-and-review action."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    labels = [button.GetLabel() for button in dialog._selection_buttons]
    assert "Edit / Review…" in labels
    assert all(label != "Edit…" for label in labels)


def test_spec_ok_with_layer_reviews_marks_approved_without_approve_click(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completing the specification dialog is enough to approve a single net class."""
    api = draft_api
    board = _board(api)
    intent = _intent(api)
    tracking = _current_layer_tracking(api, intent, board)
    dialog = _open(api, api.model.Config(enabled=True), board)
    _accepted_specification(
        api,
        monkeypatch,
        intent.specifications[0],
        review_tracking=tracking,
    )
    dialog._edit_specification(None)
    assert dialog._review_row_labels == ["Approved"]
    assert dialog.specifications.table_rows[0][0] == "Approved"
    assert dialog.session.approved is True
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save and close"


def test_spec_ok_approves_only_completed_classes_until_all_are_reviewed(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each class flips to Approved when its layer review lands; workbook waits for all."""
    api = draft_api
    base = _board(api)
    board = replace(
        base,
        net_classes=("Default", "RF", "USB"),
        net_class_memberships=(
            ("CLK_P", ("RF",)),
            ("CLK_N", ("RF",)),
            ("D_P", ("USB",)),
            ("D_N", ("USB",)),
            ("GND", ("Default",)),
        ),
        traces=base.traces
        + tuple(
            replace(trace, trace_id=f"usb-{trace.trace_id}", net=net)
            for trace, net in zip(base.traces[:2], ("D_P", "D_N"))
        ),
    )
    rf = _intent(api).specifications[0]
    usb = replace(rf, spec_id="usb", label="USB", net_class="USB")
    rf_tracking = _current_layer_tracking(
        api, api.model.Config(specifications=(rf,)), board
    )
    dialog = _open(api, api.model.Config(enabled=True, specifications=(rf,)), board)
    dialog.session.config = replace(dialog.session.config, review_tracking=rf_tracking)
    dialog._refresh_rows()
    assert dialog._review_row_labels == ["Approved"]
    assert dialog.session.approved is False

    usb_tracking = _current_layer_tracking(
        api, api.model.Config(specifications=(usb,)), board
    )
    _accepted_specification(api, monkeypatch, usb, review_tracking=usb_tracking)
    dialog._edit_specification(None)
    by_class = {
        row[1]: status
        for row, status in zip(
            dialog.specifications.table_rows, dialog._review_row_labels
        )
    }
    assert by_class == {"RF": "Approved", "USB": "Approved"}
    assert dialog.session.approved is True


def test_refresh_restores_saved_checks_and_approval_without_new_review(
    draft_api: SimpleNamespace,
) -> None:
    """Opening an unchanged approved board must not reset its exclusions."""
    api = draft_api
    board = _board(api)
    session = api.dialog.ReviewSession(_intent(api), board)
    first = session.refresh()
    wanted = first.sections[-1].section_id
    session.set_included((wanted,))
    session.approve()
    saved = session.save_result()
    reopened = api.dialog.ReviewSession(
        api.model.Config.from_dict(saved.to_dict()), board
    )

    restored = reopened.refresh()

    assert restored.digest == first.digest
    assert reopened.included == {wanted}
    assert reopened.approved is True
    assert reopened.save_result() == saved
    reopened.refresh()
    assert reopened.approved is True
    assert reopened.save_result() == saved


def test_refresh_preserves_unapproved_choices_but_changed_board_revokes_approval(
    draft_api: SimpleNamespace,
) -> None:
    """Harmless updates retain draft checks; changed capture inputs require approval."""
    api = draft_api
    board = _board(api)
    session = api.dialog.ReviewSession(_intent(api), board)
    first = session.refresh()
    wanted = first.sections[-1].section_id
    session.set_included((wanted,))
    session.refresh()
    assert session.included == {wanted}
    assert session.approved is False
    assert session.config.reviewed_digest == ""
    session.approve()
    changed = replace(board, context_digest="moved reference text")
    current = session.refresh(changed)
    assert current.digest != first.digest
    assert session.snapshot == changed
    assert session.approved is False
    assert session.config.reviewed_digest == ""


def test_specification_review_cell_style_uses_status_backgrounds(
    draft_api: SimpleNamespace,
) -> None:
    """Pending Review cells are amber; Approved is green; text stays white."""
    api = draft_api
    for label, background in (
        (api.dialog.SPEC_REVIEW_NEEDS_APPROVAL, (160, 90, 16)),
        (api.dialog.SPEC_REVIEW_NO_MATCHING_ROWS, (160, 90, 16)),
        (api.dialog.SPEC_REVIEW_APPROVED, (35, 135, 60)),
    ):
        assert api.dialog.specification_review_cell_style(label) == (
            background,
            (255, 255, 255),
        )
        attr = api.dv.DataViewItemAttr()
        assert api.dialog.apply_specification_review_cell_style(label, attr) is True
        assert attr.background == background
        assert attr.colour == (255, 255, 255)


def test_opening_populates_rows_and_offers_counted_approval_without_scan_button(
    draft_api: SimpleNamespace,
) -> None:
    """A populated class gets actionable workbook rows on first construction."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.session.analysis is not None
    assert any(layout.proportions[:2] == [3, 1] for layout in _LayoutDouble.instances)
    assert [title for title, _width in dialog.sections.columns] == [
        "Incl.",
        "Row",
        "Layer",
        "Net class",
        "Target Ω",
        "Length",
        "Net",
        "Width",
        "",
        "Target width",
        "Segments",
    ]
    assert [title for title, _width in dialog.specifications.columns] == [
        "Review",
        "Net class",
        "Signal layers",
        "Ω",
        "Type",
        "Label",
        "Width (mm)",
        "References",
    ]
    assert dialog.sections.GetNumberCols() == 11
    assert dialog.sections.minimum_size[0] >= 720
    assert len(dialog.sections.items) == 2
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.sections.GetSelectedRow() == 0
    assert not hasattr(dialog, "scan_button")
    assert dialog.retry_button.IsShown() is False
    assert dialog.approve_button.GetLabel() == "Approve 2 workbook rows"
    assert "needs approval" in dialog.review_status.GetLabel().lower()
    assert dialog._review_row_labels == ["Needs approval"]
    assert dialog.specifications.table_rows[0][0] == "Needs approval"
    pending = _review_cell_attr(api, dialog, 0)
    assert pending.background == (160, 90, 16)
    assert pending.colour == (255, 255, 255)
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert api.preview_calls == []
    assert dialog.approve_button.enabled is False

    _show(api, dialog)
    assert dialog.preview_pane.ready is True
    assert dialog.approve_button.enabled is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is True
    assert "2 workbook rows approved" in dialog.review_status.GetLabel().lower()
    assert dialog._review_row_labels == ["Approved"]
    assert dialog.specifications.table_rows[0][0] == "Approved"
    approved = _review_cell_attr(api, dialog, 0)
    assert approved.background == (35, 135, 60)
    assert approved.colour == (255, 255, 255)
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save and close"
    red, green, blue = dialog.review_status.foreground[:3]
    assert green > red and green > blue


def test_review_column_marks_unmatched_net_classes(
    draft_api: SimpleNamespace,
) -> None:
    """Specs without matching copper are visible in the Review column."""
    api = draft_api
    unused = api.model.Specification(
        spec_id="unused",
        label="Unused",
        target_ohms="50",
        kind="single_ended",
        net_class="Unused",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    board = replace(
        _board(api),
        net_classes=("Default", "RF", "Unused"),
    )
    config = replace(
        _intent(api),
        specifications=_intent(api).specifications + (unused,),
    )
    dialog = _open(api, config, board)
    by_class = {
        row[1]: status
        for row, status in zip(
            dialog.specifications.table_rows, dialog._review_row_labels
        )
    }
    assert by_class["RF"] == "Needs approval"
    assert by_class["Unused"] == "No matching rows"
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert dialog.specifications.table_rows[0][0] == "Needs approval"
    assert dialog.specifications.table_rows[1][0] == "No matching rows"
    for row in (0, 1):
        attr = _review_cell_attr(api, dialog, row)
        assert attr.background == (160, 90, 16)
        assert attr.colour == (255, 255, 255)
        other = api.dv.DataViewItemAttr()
        assert dialog.specifications.GetModel().GetAttrByRow(row, 1, other) is False
        assert other.background is None and other.colour is None


def test_save_close_and_reopen_restores_visible_rows_checks_and_approval(
    draft_api: SimpleNamespace,
) -> None:
    """The user's former empty-pane reproduction survives actual Save and reopen."""
    api = draft_api
    board = _board(api)
    saved: list[Config] = []
    dialog = _open(api, _intent(api), board, saved.append)
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    dialog.sections.SetToggleValue(False, 0, 0)
    dialog._on_include(0)
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    included = set(dialog.session.included)
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert len(saved) == 1

    reopened = _open(api, api.model.Config.from_dict(saved[0].to_dict()), board)
    _show(api, reopened)

    assert len(reopened.sections.items) == 2
    assert [row[1] for row in reopened.sections.table_rows] == ["2", "1"]
    assert reopened.session.included == included
    assert reopened.session.approved is True
    assert reopened.sections.GetToggleValue(0, 0) is True
    assert reopened.sections.GetToggleValue(1, 0) is False
    assert "1 workbook row approved" in reopened.review_status.GetLabel().lower()


def test_added_specification_refreshes_and_selects_its_first_row(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting a new class shows its rows rather than the previous class's row."""
    api = draft_api
    board = _board(api)
    added = replace(
        _intent(api).specifications[0],
        spec_id="ground",
        label="Return",
        net_class="Default",
        layer_settings=(api.model.LayerSettings("B.Cu", ("F.Cu",)),),
    )
    dialog = _open(api, _intent(api), board)
    _accepted_specification(api, monkeypatch, added)

    dialog._on_add(SimpleNamespace())

    assert len(dialog.sections.items) == 3
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2", "3"]
    current = dialog.session.analysis.sections[dialog.sections.GetSelectedRow()]
    assert current.spec_id == "ground"
    assert dialog.sections.table_rows[dialog.sections.GetSelectedRow()][1] == "1"
    assert dialog.session.approved is False
    assert dialog.approve_button.GetLabel() == "Approve 3 workbook rows"

    dialog.specifications.selection = 1
    dialog._on_remove(SimpleNamespace())

    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert [spec.spec_id for spec in dialog.session.config.specifications] == ["rf"]


def test_workbook_row_numbers_follow_reordered_and_removed_candidates(
    draft_api: SimpleNamespace,
) -> None:
    """Current visible positions replace earlier numbers when board entries move."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _intent(api), board)
    assert [(row[1], row[6]) for row in dialog.sections.table_rows] == [
        ("1", "CLK_P"),
        ("2", "CLK_N"),
    ]
    moved = replace(
        board,
        traces=tuple(
            replace(trace, points=((-2_000_000, 0), (-1_000_000, 0)))
            if trace.net == "CLK_N"
            else trace
            for trace in board.traces
        ),
    )

    assert dialog._refresh_rows(moved) is True

    assert [(row[1], row[6]) for row in dialog.sections.table_rows] == [
        ("1", "CLK_N"),
        ("2", "CLK_P"),
    ]
    removed = replace(
        moved, traces=tuple(trace for trace in moved.traces if trace.net != "CLK_N")
    )

    assert dialog._refresh_rows(removed) is True

    assert [(row[1], row[6]) for row in dialog.sections.table_rows] == [("1", "CLK_P")]


def test_accepted_no_op_edit_preserves_approval_and_selected_row(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening and accepting unchanged specification fields is not a reset action."""
    api = draft_api
    board = _board(api)
    approved = _approved(api, board)
    dialog = _open(api, approved, board)
    dialog.sections.SelectRow(1)
    selected = dialog.session.analysis.sections[1].section_id
    dialog.specifications.selection = 0
    _accepted_specification(api, monkeypatch, approved.specifications[0])

    dialog.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)

    assert dialog.session.approved is True
    assert dialog.session.config.reviewed_digest == approved.reviewed_digest
    assert (
        dialog.session.analysis.sections[dialog.sections.GetSelectedRow()].section_id
        == selected
    )


def test_changed_specification_rebuilds_rows_and_invalidates_old_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting changed target impedance updates current rows without a second action."""
    api = draft_api
    board = _board(api)
    approved = _approved(api, board)
    dialog = _open(api, approved, board)
    dialog.specifications.selection = 0
    changed = replace(approved.specifications[0], target_ohms="55")
    _accepted_specification(api, monkeypatch, changed)

    dialog.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)

    assert dialog.session.config.specifications == (changed,)
    assert len(dialog.sections.items) == 2
    assert dialog.session.analysis.digest != approved.reviewed_digest
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""


def _calculator_stackup(api: SimpleNamespace):
    """Return a fingerprint-ready two-layer construction for draft width merges."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    return model.Stackup(
        "sample",
        "Sample two-layer construction",
        2,
        "0.324",
        "1",
        "",
        calculator_id="sample-calculator",
        layers=(
            model.StackupLayer("L1", "copper", "0.035"),
            model.StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
            model.StackupLayer("L2", "copper", "0.035"),
        ),
    )


def test_ok_from_draft_merges_width_results_and_skips_refetch(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draft calculator successes must enter the session so main comparisons skip JLCPCB."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    base = replace(_intent(api), stackup=stackup)
    session = api.dialog.ReviewSession(base, board)
    session.refresh()
    session.approve()
    config = session.save_result()
    dialog = _open(api, config, board)
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    dialog.calculate_width_callback = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("Cached draft widths must not call JLCPCB.")
    )
    spec = config.specifications[0]
    result = model.WidthResult(
        spec_id=spec.spec_id,
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(stackup, spec, "F.Cu"),
        status="success",
        target_width_nm=181_400,
        calculated_at_utc="2026-09-23T00:00:00.000000Z",
        model="CoatedMicrostrip1B",
        calculation_digest="b" * 64,
    )
    dialog.specifications.selection = 0
    _accepted_specification(api, monkeypatch, spec, width_results=(result,))

    dialog.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    api.drain()

    assert result in dialog.session.config.width_results
    jobs = dialog._width_refresh_jobs(dialog.session.config)
    assert jobs is not None
    assert dialog._width_results_current(dialog.session.config, jobs)
    assert dialog._calculation_cancel is None
    assert workers == []
    targets = [row[9] for row in dialog.sections.table_rows]
    assert targets
    assert all(target not in {"Pending", "Not calculated", ""} for target in targets)


def _cached_success(model: ModuleType, stackup: object, spec: object, layer: str):
    """Build a digest-current success as the persisted cache would return it."""
    return model.WidthResult(
        spec_id=spec.spec_id,
        layer=layer,
        input_digest=model.calculation_fingerprint(stackup, spec, layer),
        status="success",
        target_width_nm=181_400,
        calculated_at_utc="2026-09-26T00:00:00.000000Z",
        model="CoatedMicrostrip1B",
        calculation_digest="d" * 64,
    )


def test_open_seeds_width_results_from_cache_and_skips_jlcpcb(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved config without results still paints Differs from the persisted cache."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = replace(_intent(api), stackup=stackup)
    assert config.width_results == ()
    spec = config.specifications[0]
    cached = _cached_success(model, stackup, spec, "F.Cu")
    stale = replace(cached, input_digest="9" * 64, target_width_nm=99_000)
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    loads: list[bool] = []

    def load_width_results() -> tuple:
        loads.append(True)
        return (stale, cached)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        load_width_results=load_width_results,
        calculate_width=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Cached widths must not call JLCPCB on open.")
        ),
    )
    _show(api, dialog)

    assert loads == [True]
    assert cached in dialog.session.config.width_results
    assert stale not in dialog.session.config.width_results
    assert workers == []
    assert dialog._calculation_cancel is None
    targets = [row[9] for row in dialog.sections.table_rows]
    assert targets
    assert all(target not in {"Pending", "Not calculated", ""} for target in targets)


def test_open_ignores_cache_rows_that_do_not_match_current_inputs(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached result for other inputs cannot suppress the needed refresh."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = replace(_intent(api), stackup=stackup)
    spec = config.specifications[0]
    stale = replace(
        _cached_success(model, stackup, spec, "F.Cu"), input_digest="9" * 64
    )
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        load_width_results=lambda: (stale,),
    )
    _show(api, dialog)

    assert stale not in dialog.session.config.width_results
    assert len(workers) == 1


def test_worker_persists_each_settled_result_before_the_batch_finishes(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing mid-batch must keep the solves that already completed."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    second = api.model.Specification(
        spec_id="usb",
        label="USB",
        target_ohms="90",
        kind="single_ended",
        net_class="Default",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    third = api.model.Specification(
        spec_id="clk",
        label="CLK",
        target_ohms="60",
        kind="single_ended",
        net_class="RF",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    base = _intent(api)
    config = replace(
        base, stackup=stackup, specifications=(*base.specifications, second, third)
    )
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    saved: list[object] = []
    order: list[str] = []
    statuses = {"rf": "success", "usb": "unavailable", "clk": "unsupported"}

    def calculate(calc_stackup: object, spec: object, layer: str, **_k: object):
        order.append(f"solve:{spec.spec_id}")
        status = statuses[spec.spec_id]
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=model.calculation_fingerprint(calc_stackup, spec, layer),
            status=status,
            target_width_nm=150_000 if status == "success" else None,
            calculated_at_utc="2026-09-26T00:00:00.000000Z"
            if status == "success"
            else "",
            model="CoatedMicrostrip1B" if status == "success" else "",
            calculation_digest="a" * 64 if status == "success" else "",
            message="" if status == "success" else f"{status} reason",
        )

    def save_width_result(result: object) -> None:
        order.append(f"save:{result.spec_id}")
        saved.append(result)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        calculate_width=calculate,
        save_width_result=save_width_result,
    )
    _show(api, dialog)
    assert len(workers) == 1
    finished: list[object] = []
    monkeypatch.setattr(
        dialog, "_finish_calculation", lambda *args: finished.append(args)
    )

    workers.pop()()

    # Each settled job reaches the cache immediately after its own solve, in
    # worker order, before the batch-level completion callback is dispatched.
    assert order == [
        "solve:rf",
        "save:rf",
        "solve:usb",
        "solve:clk",
        "save:clk",
    ]
    assert [result.status for result in saved] == ["success", "unsupported"]
    assert finished == []
    api.drain()
    assert len(finished) == 1


def test_cancelled_batch_keeps_the_jobs_that_already_settled(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel after job one: job one is cached, job two is never solved."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    second = api.model.Specification(
        spec_id="clk",
        label="CLK",
        target_ohms="60",
        kind="single_ended",
        net_class="RF",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    base = _intent(api)
    config = replace(
        base, stackup=stackup, specifications=(*base.specifications, second)
    )
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    order: list[str] = []

    def calculate(calc_stackup: object, spec: object, layer: str, **kwargs: object):
        order.append(f"solve:{spec.spec_id}")
        # The user closes the dialog while this solve is in flight.
        kwargs["cancel"].set()
        return _cached_success(model, calc_stackup, spec, layer)

    def save_width_result(result: object) -> None:
        order.append(f"save:{result.spec_id}")

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        calculate_width=calculate,
        save_width_result=save_width_result,
    )
    _show(api, dialog)
    finished: list[object] = []
    monkeypatch.setattr(
        dialog, "_finish_calculation", lambda *args: finished.append(args)
    )

    workers.pop()()
    api.drain()

    assert order == ["solve:rf", "save:rf"]
    assert finished == []


def test_open_keeps_saved_approval_when_cache_matches_saved_results(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved, approved config is authoritative; the cache cannot revoke it."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    spec = _intent(api).specifications[0]
    saved_result = _cached_success(model, stackup, spec, "F.Cu")
    session = api.dialog.ReviewSession(
        replace(_intent(api), stackup=stackup, width_results=(saved_result,)), board
    )
    session.refresh()
    session.approve()
    approved = session.save_result()
    assert approved.reviewed_digest
    # Same digest, but a later re-solve stored a different nominal.
    cache_row = replace(
        saved_result,
        target_width_nm=181_500,
        calculated_at_utc="2026-09-27T00:00:00.000000Z",
    )
    monkeypatch.setattr(
        api.dialog,
        "Thread",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("A settled saved result must not be re-solved.")
        ),
    )

    dialog = api.dialog.ImpedanceDialog(
        None,
        approved,
        board,
        api.preview,
        lambda: board,
        load_width_results=lambda: (cache_row,),
    )
    _show(api, dialog)

    assert dialog.session.config.width_results == (saved_result,)
    assert dialog.session.config.reviewed_digest == approved.reviewed_digest
    assert dialog.session.approved is True


def test_retry_timer_resubmits_only_transient_failures(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An 'unsupported' rejection is settled; only 'unavailable' jobs retry."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    second = api.model.Specification(
        spec_id="usb",
        label="USB",
        target_ohms="90",
        kind="single_ended",
        net_class="Default",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    base = _intent(api)
    config = replace(
        base, stackup=stackup, specifications=(*base.specifications, second)
    )
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    solved: list[str] = []

    def calculate(calc_stackup: object, spec: object, layer: str, **_k: object):
        solved.append(spec.spec_id)
        digest = model.calculation_fingerprint(calc_stackup, spec, layer)
        if spec.spec_id == "usb":
            return model.WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="unsupported",
                message="JLCPCB's calculator rejected these inputs: the target "
                "impedance is not achievable within its trace-width range.",
            )
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=digest,
            status="unavailable",
            message="Could not connect to JLCPCB's calculator.",
        )

    dialog = api.dialog.ImpedanceDialog(
        None, config, board, api.preview, lambda: board, calculate_width=calculate
    )
    _show(api, dialog)
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert sorted(solved) == ["rf", "usb"]
    jobs = dialog._width_refresh_jobs(dialog.session.config)
    assert jobs is not None
    needing = dialog._width_jobs_needing_refresh(dialog.session.config, jobs)
    assert [spec.spec_id for spec, _layer in needing] == ["rf"]

    # The 3-minute retry fires: only the transient failure is resubmitted.
    dialog._width_refresh_timer.Stop()
    dialog._on_width_refresh_timer(SimpleNamespace())
    assert len(workers) == 1
    solved.clear()
    workers.pop()()
    api.drain()
    assert solved == ["rf"]
    # The rejection is retained with its reason (this fixture routes no Default
    # traces, so exercise the comparison contract directly for that spec).
    checks = importlib.import_module(api.dialog.__package__ + ".width_checks")
    check = checks.width_check(dialog.session.config, second, "F.Cu", 150_000)
    assert check.status == "not_calculated"
    assert check.result_current is True
    assert "not achievable" in check.message
    assert "pending" not in check.message.lower()


def test_worker_cache_failure_does_not_fail_the_batch(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache write error is logged; the in-memory result is still applied."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = replace(_intent(api), stackup=stackup)
    workers: list[object] = []

    class QueuedThread:
        def __init__(self, *, target: object, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)

    def calculate(calc_stackup: object, spec: object, layer: str, **_k: object):
        return _cached_success(model, calc_stackup, spec, layer)

    def failing_save(_result: object) -> None:
        raise OSError("disk full")

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        calculate_width=calculate,
        save_width_result=failing_save,
    )
    _show(api, dialog)
    workers.pop()()
    api.drain()

    assert dialog._calculation_cancel is None
    assert any(
        result.status == "success" for result in dialog.session.config.width_results
    )


def test_cancel_from_draft_does_not_merge_width_results(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled draft successes must remain local to the destroyed child dialog."""
    api = draft_api
    board = _board(api)
    stackup = _calculator_stackup(api)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = replace(_approved(api, board), stackup=stackup)
    dialog = _open(api, config, board)
    before = dialog.session.config.width_results
    spec = config.specifications[0]
    result = model.WidthResult(
        spec_id=spec.spec_id,
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(stackup, spec, "F.Cu"),
        status="success",
        target_width_nm=181_400,
        calculated_at_utc="2026-09-23T00:00:00.000000Z",
        model="CoatedMicrostrip1B",
        calculation_digest="c" * 64,
    )
    dialog.specifications.selection = 0
    _accepted_specification(
        api,
        monkeypatch,
        spec,
        width_results=(result,),
        modal_result=api.wx.ID_CANCEL,
    )

    dialog.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)

    assert dialog.session.config.width_results == before
    assert result not in dialog.session.config.width_results


def test_harmless_row_population_preserves_selection_and_unchecked_rows(
    draft_api: SimpleNamespace,
) -> None:
    """Status refreshes must not jump the preview back to the first candidate."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.sections.SelectRow(1)
    selected = dialog.session.analysis.sections[1].section_id
    dialog.sections.SetToggleValue(False, 0, 0)
    dialog._on_include(0)
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    original_ids = dialog._row_ids

    dialog._populate_sections()
    assert dialog._refresh_rows() is True

    assert dialog._row_ids == original_ids[::-1]
    assert [row[1] for row in dialog.sections.table_rows] == ["2", "1"]
    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected
    assert dialog.sections.GetToggleValue(0, 0) is True
    assert dialog.sections.GetToggleValue(1, 0) is False
    assert dialog.approve_button.GetLabel() == "Approve 1 workbook row"


def test_stackup_acceptance_repopulates_rows_and_requires_new_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted construction change cannot leave the formerly populated pane empty."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _approved(api, board), board)
    _show(api, dialog)
    selected = _stackup(api)

    class AcceptedPicker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stackup = selected

        def ShowModal(self) -> int:
            return api.wx.ID_OK

        def Destroy(self) -> None:
            self.destroyed = True

    module_name = api.dialog.__package__ + ".stackup_dialog"
    picker_module = ModuleType(module_name)
    picker_module.StackupDialog = AcceptedPicker
    monkeypatch.setitem(sys.modules, module_name, picker_module)
    monkeypatch.setattr(dialog, "_ensure_catalog_started", lambda _count: object())

    dialog.stackup_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.config.stackup == selected
    assert len(dialog.sections.items) == 2
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.approve_button.enabled is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True


def test_complete_calculation_refreshes_rows_without_approving_new_results(
    draft_api: SimpleNamespace,
) -> None:
    """Accept a complete immutable worker result through its actual completion handler."""
    api = draft_api
    board = _board(api)
    original = replace(_intent(api), stackup=_stackup(api))
    dialog = _open(api, original, board)
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = dialog.session.config
    result = model.WidthResult(
        spec_id="rf",
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(
            config.stackup, config.specifications[0], "F.Cu"
        ),
        status="unsupported",
        message="Construction is unavailable to the nominal-width solver.",
    )
    key = dialog._calculation_key(config, board)
    dialog._calculation_cancel = Event()
    generation = dialog._calculation_generation

    dialog._finish_calculation(generation, key, (result,), "")

    assert dialog._calculation_cancel is None
    assert dialog.session.config.width_results == (result,)
    assert len(dialog.sections.items) == 2
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.approve_button.enabled is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True


def test_remove_repopulates_rows_without_leaving_stale_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Removing the final specification empties the rows intentionally and safely."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.specifications.selection = 0

    dialog._on_remove(SimpleNamespace())

    assert dialog.session.config.specifications == ()
    assert dialog.sections.items == []
    assert dialog.sections.table_rows == []
    assert dialog.preview_pane.sections == ()
    assert dialog.preview_pane.canvas._image is None
    assert dialog.preview_pane.canvas._bitmap is None
    assert dialog.approve_button.enabled is False
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "specification" in dialog.status.GetValue().lower()


def test_approval_after_board_change_refreshes_but_requires_second_explicit_click(
    draft_api: SimpleNamespace,
) -> None:
    """The same click must never authorize rows the user has not yet seen."""
    api = draft_api
    board = _board(api)
    changed = replace(board, context_digest="new footprint location")
    dialog = _open(api, _intent(api), board)
    _show(api, dialog)
    old_digest = dialog.session.analysis.digest
    dialog.refresh_snapshot = lambda: changed

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.analysis.digest != old_digest
    assert dialog.session.snapshot == changed
    assert len(dialog.sections.items) == 2
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert "review" in dialog.status.GetValue().lower()
    assert dialog.approve_button.enabled is False
    api.drain()
    api.paint(dialog.preview_pane)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True


def test_approval_rejects_unreadable_live_snapshot(
    draft_api: SimpleNamespace,
) -> None:
    """Previously displayed pixels cannot approve an unreadable current board."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)

    def fail_verification() -> None:
        raise ValueError("The native board capture context is unavailable.")

    dialog.refresh_snapshot = fail_verification
    assert dialog.approve_button.enabled is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.sections.items == []
    assert dialog.sections.table_rows == []
    assert dialog.preview_pane.canvas._image is None
    assert dialog.retry_button.IsShown() is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "native board capture context" in dialog.status.GetValue()


def test_population_failure_clears_stale_rows_and_retry_recovers_without_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed current scan never leaves old candidates looking ready to approve."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _approved(api, board), board)
    original_analyze = api.dialog.analyze

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("Could not read current net classes.")

    monkeypatch.setattr(api.dialog, "analyze", fail)
    dialog.session.analysis = None
    assert dialog._refresh_rows() is False
    assert dialog.session.analysis is None
    assert dialog.sections.items == []
    assert dialog.sections.table_rows == []
    assert dialog.session.approved is False
    assert dialog.preview_pane.canvas._image is None
    assert dialog.preview_pane.canvas._bitmap is None
    assert dialog.retry_button.IsShown() is True
    assert dialog.approve_button.enabled is False
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "Could not read current net classes" in dialog.status.GetValue()

    monkeypatch.setattr(api.dialog, "analyze", original_analyze)
    dialog.retry_button.emit(api.wx.EVT_BUTTON)

    assert len(dialog.sections.items) == 2
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""


def test_initial_population_failure_explains_empty_pane_and_can_retry(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor-time analysis exception leaves an editable dialog, not a crash."""
    api = draft_api
    original_analyze = api.dialog.analyze

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("The board net-class context could not be loaded.")

    monkeypatch.setattr(api.dialog, "analyze", fail)
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.sections.items == []
    assert dialog.sections.table_rows == []
    assert dialog.retry_button.IsShown() is True
    assert "net-class context" in dialog.status.GetValue()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True

    monkeypatch.setattr(api.dialog, "analyze", original_analyze)
    dialog.retry_button.emit(api.wx.EVT_BUTTON)

    assert len(dialog.sections.items) == 2
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False


def test_preview_waits_for_show_and_ignores_closed_dialog_callbacks(
    draft_api: SimpleNamespace,
) -> None:
    """Auto-selection may render only after native visibility, never after Cancel."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    assert api.preview_calls == []
    assert dialog.session.config.review_tracking.images == ()

    _show(api, dialog)

    assert api.preview_calls == [dialog.session.analysis.sections[0]]
    assert dialog.preview_pane.ready is True
    dialog.sections.SelectRow(1)
    dialog._on_preview(dialog.sections.GetSelectedRow())
    dialog.bindings[api.wx.EVT_BUTTON, api.wx.ID_CANCEL](
        SimpleNamespace(Skip=lambda: None)
    )
    api.drain()
    api.paint(dialog.preview_pane)
    assert len(api.preview_calls) == 1
    assert len(dialog.session.config.review_tracking.images) == 1


def test_queued_preview_for_obsolete_selection_is_ignored(
    draft_api: SimpleNamespace,
) -> None:
    """Only the newest native selection may render when CallAfter work is delivered."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    dialog._queue_selected_preview()
    dialog.sections.SelectRow(1)
    dialog._on_preview(dialog.sections.GetSelectedRow())

    api.drain()
    api.paint(dialog.preview_pane)

    assert api.preview_calls == [dialog.session.analysis.sections[1]]
    assert dialog.preview_pane.ready is True
    viewed = dialog.session.config.review_tracking.images
    assert len(viewed) == 1
    assert viewed[0].section_id == dialog.session.analysis.sections[1].section_id


def test_show_event_before_native_visibility_still_schedules_first_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Some native platforms deliver EVT_SHOW before IsShownOnScreen becomes true."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.IsShownOnScreen() is False

    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )

    assert api.preview_calls == []
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    api.drain()
    assert api.preview_calls == [dialog.session.analysis.sections[0]]
    assert dialog.preview_pane.ready is False
    assert dialog.session.config.review_tracking.images == ()
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True


def test_deferred_parent_preview_waits_for_a_child_dialog_to_close(
    draft_api: SimpleNamespace,
) -> None:
    """A queued parent image is not a viewed capture behind an active child modal."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    dialog._queue_selected_preview()
    dialog._child_dialog_open = True
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls == []
    assert dialog.session.config.review_tracking.images == ()
    dialog._child_dialog_open = False
    dialog._queue_selected_preview()
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls == [
        dialog.session.analysis.sections[dialog.sections.GetSelectedRow()]
    ]
    assert dialog.preview_pane.ready is True


def test_failed_capture_and_pending_retry_cannot_be_bypassed_by_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh row analysis is not evidence that its failed image now renders."""
    api = draft_api
    failing = True
    attempts: list[bool] = []
    original_preview = api.preview

    def capture(section: object, *, refresh: bool = False) -> CapturedImage:
        attempts.append(refresh)
        if failing:
            raise ValueError("Native capture failed.")
        return original_preview(section, refresh=refresh)

    monkeypatch.setattr(api, "preview", capture)
    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)
    assert dialog.preview_pane.canvas._image is None
    assert dialog.approve_button.enabled is False
    assert dialog.retry_button.IsShown() is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.retry_button.IsShown() is True
    failing = False
    dialog.retry_button.emit(api.wx.EVT_BUTTON)
    assert dialog.preview_pane.ready is False
    assert dialog.approve_button.enabled is False

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert api.preview_calls == []
    api.drain()
    assert len(api.preview_calls) == 1
    assert attempts[-1] is True
    assert not any(attempts[:-1])
    assert dialog.preview_pane.canvas._image is not None
    assert dialog.preview_pane.ready is False
    assert dialog.approve_button.enabled is False
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True
    assert dialog.approve_button.enabled is True
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True


def test_parent_view_timestamp_is_not_recorded_behind_a_child_modal(
    draft_api: SimpleNamespace,
) -> None:
    """A late paint notification is not evidence of viewing an obscured parent."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    dialog._queue_selected_preview()
    api.drain()
    assert dialog.preview_pane.canvas._image is not None
    before = dialog.session.config.review_tracking
    dialog._child_dialog_open = True
    api.paint(dialog.preview_pane)
    assert dialog.session.config.review_tracking == before
    assert dialog.preview_pane.ready is False
    dialog._child_dialog_open = False
    api.paint(dialog.preview_pane)
    assert len(dialog.session.config.review_tracking.images) == 1
    assert dialog.preview_pane.ready is True


def test_harmless_refresh_and_approval_reuse_successfully_rendered_preview(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real capture, canvas paint, and source-checked view notification."""
    api = draft_api

    def unexpected_timestamp() -> str:
        pytest.fail(
            "Unchanged row refresh must not manufacture another image-view event"
        )

    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)
    assert len(api.preview_calls) == 1
    pane = dialog.preview_pane
    selected = pane.selected_section
    included = tuple(
        section
        for section in dialog.session.analysis.sections
        if section.section_id in dialog.session.included
    )
    unvisited = tuple(section for section in included if section != selected)
    assert selected is not None and unvisited
    assert api.preview_calls == [selected]
    image = pane.canvas._image
    bitmap = pane.canvas._bitmap
    assert image is not None
    assert bitmap is not None
    generation = pane.canvas._generation
    tracking = dialog.session.config.review_tracking
    assert len(tracking.images) == 1
    preview_module = importlib.import_module(api.dialog.__package__ + ".dialog_preview")
    monkeypatch.setattr(preview_module, "utc_now", unexpected_timestamp)

    assert dialog._refresh_rows() is True
    assert api.preview_calls == [selected]
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(pane)

    assert dialog.session.approved is True
    # Bulk approval binds every included row to current pixels. The displayed
    # row is reused, while each previously unvisited row is captured once.
    assert len(api.preview_calls) == len(included)
    assert api.preview_calls.count(selected) == 1
    assert all(api.preview_calls.count(section) == 1 for section in unvisited)
    captures = dict(dialog.session.config.reviewed_captures)
    assert set(captures) == dialog.session.included
    assert captures[selected.section_id] == tracking.images[0].capture_digest
    assert pane.canvas._generation == generation
    assert pane.canvas._image is image
    assert pane.canvas._bitmap is bitmap
    assert dialog.session.config.review_tracking == tracking


@pytest.mark.parametrize("action", ["approve", "render", "save"])
def test_cancelled_child_cannot_rebind_stale_parent_approval_to_changed_board(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Cancel keeps intent, but the next parent action must recheck the live board."""
    api = draft_api
    original_board = _board(api)
    changed_board = replace(original_board, context_digest="moved component")
    original = _approved(api, original_board)
    saved: list[Config] = []
    dialog = _open(api, original, original_board, saved.append)
    _show(api, dialog)
    dialog.refresh_snapshot = lambda: changed_board
    dialog.specifications.selection = 0

    class CancelledSpecification:
        def __init__(
            self, _parent: object, snapshot: object, *_args: object, **_kwargs: object
        ) -> None:
            assert snapshot == changed_board
            self.specification = None

        def ShowModal(self) -> int:
            return api.wx.ID_CANCEL

        def Destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(api.dialog, "SpecificationDialog", CancelledSpecification)
    dialog.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)

    assert dialog.session.snapshot == original_board
    assert dialog.session.config.specifications == original.specifications
    if action == "approve":
        dialog.approve_button.emit(api.wx.EVT_BUTTON)
    elif action == "save":
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    else:
        api.drain()
        api.paint(dialog.preview_pane)
    assert dialog.session.snapshot == changed_board
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.session.config.specifications == original.specifications
    if action == "save":
        assert len(saved) == 1
        assert saved[0].reviewed_digest == ""
        assert dialog.modal_result == api.wx.ID_OK


def test_board_change_during_capture_never_displays_or_approves_obsolete_pixels(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate source after the provider returns, before installing its pixels."""
    api = draft_api
    original_board = _board(api)
    changed_board = replace(original_board, context_digest="changed during rendering")
    live = {"snapshot": original_board}
    original_preview = api.preview

    def capture(section: object, *, refresh: bool = False) -> CapturedImage:
        result = original_preview(section, refresh=refresh)
        live["snapshot"] = changed_board
        return result

    monkeypatch.setattr(api, "preview", capture)
    dialog = _open(api, _approved(api, original_board), original_board)
    dialog.refresh_snapshot = lambda: live["snapshot"]
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    callback, arguments = api.queued.pop(0)
    callback(*arguments)

    assert dialog.session.snapshot == changed_board
    assert dialog.session.approved is False
    assert dialog.preview_pane.canvas._image is None
    assert dialog.session.config.review_tracking.images == ()
    api.drain()
    assert len(api.preview_calls) == 2
    assert dialog.session.config.review_tracking.images == ()
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True
    assert len(dialog.session.config.review_tracking.images) == 1
    assert dialog.session.approved is False


def test_covered_stub_defaults_unchecked_grey_and_can_be_included(
    draft_api: SimpleNamespace,
) -> None:
    """Covered stubs stay muted and unchecked until the user explicitly includes them."""
    api = draft_api
    model = api.model
    stub = model.Trace("stub", "F.Cu", "CLK_P", 150_000, ((0, 0), (100_000, 0)))
    run = model.Trace("run", "F.Cu", "CLK_N", 150_000, ((0, 0), (2_500_000, 0)))
    pad = model.CopperFeature(
        "pad-1",
        "CLK_P",
        "F.Cu",
        "pad",
        "circle",
        (0, 0),
        400_000,
        400_000,
    )
    board = replace(
        _board(api),
        traces=(stub, run),
        copper_features=(pad,),
        differential_pairs=(),
    )
    dialog = _open(api, _intent(api), board)
    assert dialog.session.analysis is not None
    assert [row[1] for row in dialog.sections.table_rows] == ["2", "1"]
    by_reason = {
        section.exclusion_reason: dialog._row_ids.index(section.section_id)
        for section in dialog.session.analysis.sections
    }
    assert "" in by_reason and "covered_stub" in by_reason
    stub_index = by_reason["covered_stub"]
    run_index = by_reason[""]
    assert dialog.sections.GetToggleValue(stub_index, 0) is False
    assert dialog.sections.GetToggleValue(run_index, 0) is True
    for column in range(dialog.sections.GetNumberCols()):
        assert dialog.sections.GetCellTextColour(stub_index, column)[:3] == (
            120,
            120,
            120,
        )
    stub_id = dialog._row_ids[stub_index]
    assert stub_id not in dialog.session.included

    dialog.sections.SetToggleValue(True, stub_index, 0)
    dialog._on_include(stub_index)
    assert stub_id in dialog.session.included
    assert dialog.sections.GetToggleValue(stub_index, 0) is True
    api.drain()
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
