"""Nested capture regions share one workbook row through review and export.

These cases use the real dialog constructor and handlers with the existing
stateful wx harness. They do not establish native rendering or control behavior.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest

from .test_impedance_automatic_rows import _accepted_specification, _show
from .test_impedance_draft_dialog import (
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import BoardSnapshot, Config, Section

# Genuine review digest for _scenario before contained-region suppression. Both
# the all-rows and outer-only approvals were accepted by that implementation.
_LEGACY_REVIEW_DIGEST = (
    "b3e95f9c7f7dd60beb3fe7ab1ea3e771b3d0c424be40606a01811e648c96bc0c"
)
# Genuine persisted fingerprints from algorithm 7 and capture policy 6, before
# board bounds entered the source context. These nonnested routes keep their IDs.
_LEGACY_LAYER_REVIEWS = {
    "single_ended": (
        "7ef8ae96639defbf2b01b883a132c6082ce9f55c8a584e1e2c09247fc308723b",
        "a9876158ea382884063ffec7d4bf47efb9b93a39fe6badc920ba6ce9d7f2a7ba",
        (
            (
                "8672c436982e66a189374c6c54b426edda8b218e1c2cc52f30a7b46285ac5ef4",
                "0280978e0e40fb6c4ee15e57d9d33e356cb94b8849277d34d9fb18e15710283a",
            ),
            (
                "ca0f7b2a972ae05903c8924b703281aa8f5a3811e8628b1123a9d73b761065e2",
                "247ada910de1573db8cdf08d11428afa778e36c74ceb12662deecf12a1a41c48",
            ),
        ),
    ),
    "differential": (
        "6d9c93ed8253f1f44ec94a461b6fc32af7bd45b7942727523cbe4bc7872bfb4d",
        "473b093c12204d941ff449ed6bae42256cfcda4831cdd15f819c460e8e25f678",
        (
            (
                "1c0ee8b444315dfdb808cb9d82c5f1e24cbfec8619dceecfc3cfbec2c7373a6d",
                "dfbc047cee0e6e7245bf13d7b69966fd583502b252e9fe36e5a62490b239fc4f",
            ),
        ),
    ),
}


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep region review independent of unrelated network/catalog workers."""

    def skip_catalog(_dialog: object, _count: object = None) -> None:
        """Leave catalog startup to the dedicated catalog workflow tests."""

    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog, "_ensure_catalog_started", skip_catalog
    )


def _scenario(
    api: SimpleNamespace, *, nested: bool = True
) -> tuple[Config, BoardSnapshot]:
    """Model the reported same-width USB D+/D- regions and one separate row."""
    points = (
        (0, 0),
        (2_000_000, 0),
        (3_000_000, 1_000_000),
        (3_000_000, 2_000_000),
        (2_000_000, 3_000_000),
        (0, 3_000_000),
        (0, 2_000_000),
    )
    outer = tuple(
        api.model.Trace(f"outer-{index}", "F.Cu", "/D+", 203_200, (start, end))
        for index, (start, end) in enumerate(zip(points, points[1:]))
    )
    inner_x = 1_000_000 if nested else -2_000_000
    traces = outer + (
        api.model.Trace(
            "inner",
            "F.Cu",
            "/D-",
            203_200,
            ((inner_x, 1_000_000), (inner_x + 200_000, 1_000_000)),
        ),
        api.model.Trace(
            "separate",
            "F.Cu",
            "/OTHER",
            203_200,
            ((5_000_000, 0), (6_000_000, 0)),
        ),
    )
    board = api.model.BoardSnapshot(
        ("F.Cu", "B.Cu"),
        traces,
        context_digest="nested-region-native-context",
        net_classes=("USB",),
        net_class_memberships=tuple(
            (net, ("USB",)) for net in ("/D+", "/D-", "/OTHER")
        ),
        net_class_context_digest="usb-region-classes",
    )
    config = api.model.Config(
        enabled=True,
        specifications=(
            api.model.Specification(
                "usb",
                "USB",
                "90",
                "single_ended",
                "USB",
                (api.model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )
    return config, board


def _nets(dialog: ImpedanceDialog) -> list[tuple[str, ...]]:
    """Read the logical rows behind the actual populated list control."""
    return [section.net_names for section in dialog.session.analysis.sections]


def test_nested_rows_can_be_approved_saved_and_reopened(
    draft_api: SimpleNamespace,
) -> None:
    """A shared row retains both signals through approval and Save events."""
    api = draft_api
    config, board = _scenario(api)
    saved: list[Config] = []
    dialog = _open(api, config, board, saved.append)

    assert _nets(dialog) == [("/D+", "/D-"), ("/OTHER",)]
    assert dialog.sections.table_rows[0][6] == "/D+, /D-"
    assert sorted(
        trace.trace_id
        for section in dialog.session.analysis.sections
        for trace in section.traces
    ) == sorted(trace.trace_id for trace in board.traces)
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    original_ids = dialog._row_ids
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert len(saved) == 1
    assert saved[0].included_section_ids == original_ids

    reopened = _open(api, api.model.Config.from_dict(saved[0].to_dict()), board)

    assert _nets(reopened) == [("/D+", "/D-"), ("/OTHER",)]
    assert reopened._row_ids == original_ids
    assert reopened.session.included == set(original_ids)
    assert reopened.session.approved is True
    assert "2 workbook rows approved" in reopened.review_status.GetLabel().lower()


def test_refresh_removes_contained_row_and_keeps_surviving_selection(
    draft_api: SimpleNamespace,
) -> None:
    """A row that becomes contained disappears while another selection stays put."""
    api = draft_api
    config, board = _scenario(api, nested=False)
    dialog = _open(api, config, board)
    assert len(dialog._row_ids) == 3
    selected = next(
        index
        for index, section in enumerate(dialog.session.analysis.sections)
        if section.net_names == ("/OTHER",)
    )
    dialog.sections.SelectRow(selected)
    dialog.sections.emit(api.dv.EVT_DATAVIEW_SELECTION_CHANGED)
    selected_id = dialog._row_ids[selected]
    _, nested = _scenario(api)

    assert dialog._refresh_rows(nested) is True

    assert _nets(dialog) == [("/D+", "/D-"), ("/OTHER",)]
    assert [row[1] for row in dialog.sections.table_rows] == ["1", "2"]
    assert dialog.sections.GetSelectedRow() == 1
    assert dialog._row_ids[1] == selected_id
    assert (
        dialog.preview_pane._sections[dialog.preview_pane._index].section_id
        == selected_id
    )
    assert dialog.session.approved is False


@pytest.mark.parametrize("included_inner", [False, True])
def test_old_saved_inner_row_cannot_restore_a_suppressed_candidate(
    draft_api: SimpleNamespace,
    included_inner: bool,
) -> None:
    """A pre-policy saved row ID cannot resurrect a nested row or authorize export."""
    api = draft_api
    config, board = _scenario(api)
    matching = importlib.import_module(api.dialog.__package__ + ".matching")
    service = importlib.import_module(api.dialog.__package__ + ".service")
    inner_only = replace(
        board,
        traces=tuple(trace for trace in board.traces if trace.trace_id == "inner"),
    )
    inner_id = matching.analyze(config, inner_only).sections[0].section_id
    outer_and_separate = replace(
        board,
        traces=tuple(trace for trace in board.traces if trace.trace_id != "inner"),
    )
    surviving_ids = tuple(
        section.section_id
        for section in matching.analyze(config, outer_and_separate).sections
    )
    old = replace(
        config,
        reviewed_digest=_LEGACY_REVIEW_DIGEST,
        included_section_ids=surviving_ids + ((inner_id,) if included_inner else ()),
    )
    payload = old.to_dict()
    dialog = _open(api, api.model.Config.from_dict(payload), board)

    assert inner_id not in dialog._row_ids
    assert dialog._row_ids[0] != surviving_ids[0]
    assert dialog._row_ids[1] == surviving_ids[1]
    assert _nets(dialog) == [("/D+", "/D-"), ("/OTHER",)]
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert old.to_dict() == payload
    with pytest.raises(api.model.ValidationError, match="changed|review"):
        service.prepare(old, board)
    draft = dialog.session.save_result()
    assert draft.included_section_ids == ()
    assert draft.specifications == config.specifications


def test_report_preflight_and_exports_use_only_retained_regions(
    draft_api: SimpleNamespace, tmp_path: Path
) -> None:
    """Actual workbook and HTML outputs contain one capture per surviving row."""
    api = draft_api
    config, board = _scenario(api)
    original_traces = board.traces
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    session.approve()
    service = importlib.import_module(api.dialog.__package__ + ".service")
    plan = service.prepare(session.save_result(), board)
    assert plan is not None
    assert [section.net_names for section in plan.sections] == [
        ("/D+", "/D-"),
        ("/OTHER",),
    ]
    rendered: list[Section] = []

    class Renderer:
        """Supply deterministic pixels at the native renderer boundary."""

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Record actual chosen geometry and provide a valid immutable capture."""
            rendered.append(section)
            destination.write_bytes(api.capture.data)
            return destination

    def renderer_factory(_board: object, _pcbnew: object) -> Renderer:
        """Construct the image provider without importing native KiCad bindings."""
        return Renderer()

    artifacts = service.export_reports(
        plan,
        object(),
        ModuleType("pcbnew-test-double"),
        tmp_path,
        renderer_factory=renderer_factory,
    )

    assert tuple(rendered) == plan.sections
    assert sorted(
        trace.trace_id for section in rendered for trace in section.traces
    ) == sorted(trace.trace_id for trace in board.traces)
    report_rows = service.report_rows(plan, tuple(api.capture for _section in rendered))
    assert [row.nets for row in report_rows] == [("/D+", "/D-"), ("/OTHER",)]
    with ZipFile(artifacts.workbook) as workbook:
        images = [name for name in workbook.namelist() if name.startswith("xl/media/")]
        assert len(images) == 2
        assert all(workbook.read(name) == api.capture.data for name in images)
    html = artifacts.html_report.read_text(encoding="utf-8")
    assert html.count('src="data:image/png;base64,') == 2
    assert "/D+, /D-" in html
    assert board.traces == original_traces


def test_nearby_disconnected_signal_segments_share_one_reviewable_row(
    draft_api: SimpleNamespace,
) -> None:
    """A local second segment remains explicitly represented in its signal box."""
    api = draft_api
    config, board = _scenario(api)
    board = replace(
        board,
        traces=(
            api.model.Trace("first", "F.Cu", "/D-", 203_200, ((0, 0), (200_000, 0))),
            api.model.Trace(
                "second", "F.Cu", "/D-", 203_200, ((500_000, 0), (700_000, 0))
            ),
        ),
    )
    dialog = _open(api, config, board)

    assert _nets(dialog) == [("/D-",)]
    assert {trace.trace_id for trace in dialog.session.analysis.sections[0].traces} == {
        "first",
        "second",
    }
    assert dialog.sections.table_rows[0][6] == "/D-"


def test_changed_region_members_replace_id_and_require_fresh_review(
    draft_api: SimpleNamespace,
) -> None:
    """Adding D- to D+'s region invalidates its old identity and selection."""
    api = draft_api
    config, nested = _scenario(api)
    board = replace(
        nested,
        traces=tuple(trace for trace in nested.traces if trace.trace_id != "inner"),
    )
    saved: list[Config] = []
    current = [board]
    dialog = _open(api, config, board, saved.append)
    dialog.refresh_snapshot = lambda: current[0]
    old_id = dialog._row_ids[0]
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True

    current[0] = nested
    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert _nets(dialog) == [("/D+", "/D-"), ("/OTHER",)]
    assert old_id not in dialog._row_ids
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.sections.GetSelectedRow() == 0
    assert (
        dialog.preview_pane._sections[dialog.preview_pane._index].section_id
        == dialog._row_ids[0]
    )
    assert "changed" in dialog.status.GetValue().lower()
    api.drain()
    api.paint(dialog.preview_pane)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert len(saved) == 1
    reopened = _open(api, api.model.Config.from_dict(saved[0].to_dict()), nested)
    assert _nets(reopened) == [("/D+", "/D-"), ("/OTHER",)]
    assert reopened.session.approved is True


@pytest.mark.parametrize("kind", ["single_ended", "differential"])
def test_old_layer_review_cannot_silently_restore_workbook_approval(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """An unchanged row set needs fresh layer approval after the policy changes."""
    api = draft_api
    config, board = _scenario(api, nested=False)
    board = replace(
        board,
        traces=tuple(trace for trace in board.traces if trace.trace_id != "separate"),
        net_class_memberships=tuple(
            membership
            for membership in board.net_class_memberships
            if membership[0] != "/OTHER"
        ),
        differential_pairs=(("/D+", "/D-"),),
    )
    spec = replace(
        config.specifications[0],
        kind=kind,
        layer_settings=(
            api.model.LayerSettings(
                "F.Cu", ("B.Cu",), 200_000 if kind == "differential" else None
            ),
        ),
    )
    config = replace(config, specifications=(spec,))
    matching = importlib.import_module(api.dialog.__package__ + ".matching")
    tracking = api.dialog.tracking
    old_digest, old_layer_digest, captures = _LEGACY_LAYER_REVIEWS[kind]
    timestamp = "2026-10-15T12:00:00.000000Z"
    history = tracking.ReviewTracking(
        images=tuple(
            tracking.ImageView("usb", "F.Cu", section_id, capture_digest, timestamp)
            for section_id, capture_digest in captures
        ),
        layers=(
            tracking.LayerApproval(
                "usb", "F.Cu", old_layer_digest, timestamp, tuple(sorted(captures))
            ),
        ),
    )
    old = replace(
        config,
        reviewed_digest=old_digest,
        included_section_ids=tuple(section_id for section_id, _digest in captures),
        review_tracking=history,
    )
    payload = old.to_dict()
    dialog = _open(api, api.model.Config.from_dict(payload), board)
    current = matching.analyze(config, board)
    assert (
        tuple(section.section_id for section in current.sections)
        == old.included_section_ids
    )
    assert dialog.session.approved is False
    assert dialog.session.config.review_tracking == history
    source = tracking.source_fingerprint(board)
    for section, (_section_id, old_capture_digest) in zip(current.sections, captures):
        assert (
            tracking.capture_fingerprint(source, spec, section, api.capture.sha256)
            != old_capture_digest
        )

    # Reaccepting an unchanged specification is a production path which calls
    # approve_if_layer_reviews_complete; it must not revive the legacy approval.
    _accepted_specification(api, monkeypatch, spec, review_tracking=history)
    dialog._edit_specification(0)

    assert dialog.session.approved is False
    assert dialog.session.approve_if_layer_reviews_complete() is False
    assert dialog._review_row_labels == [api.dialog.SPEC_REVIEW_NEEDS_APPROVAL]
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.session.config.review_tracking == history
    assert dialog.session.save_result().review_tracking == history
    assert old.to_dict() == payload
