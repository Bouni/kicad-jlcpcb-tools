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


def test_spacing_modal_verifies_once_at_each_native_acceptance_boundary(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One synchronous native event needs one fresh source read, without nested repeats."""
    from .test_impedance_netclass_dialog import (
        _drive_spacing_viewer,
        _install_spacing_viewer_harness,
    )

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _gap_board(api, 100_000)
    checks: list[Any] = []
    editor = api.dialog.SpecificationDialog(
        None,
        board,
        _config(api).specifications[0],
        preview=api.preview,
        verify_snapshot=checks.append,
    )
    editor.on_screen = True
    api.drain()
    pane = editor.preview_pane
    api.paint(pane)
    assert pane.displayed and len(pane.spacing_entries) == 1
    checks.clear()

    def modal(viewer: Any) -> int:
        """Deliver real post-paint and button handlers with separate source reads."""
        assert checks == [board]  # Before presenting the large image.
        displayed = viewer.canvas._displayed
        painted_checks: list[int] = []

        def after_paint() -> None:
            """Observe the completed display handler rather than bypassing it."""
            displayed()
            painted_checks.append(len(checks))

        viewer.canvas._displayed = after_paint
        result = _drive_spacing_viewer(api, viewer)
        assert painted_checks == [2]  # Fresh post-paint check, once.
        assert viewer.approved and checks == [board, board, board]
        return result

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", modal)
    assert pane.approve_spacing_image(editor._approve_spacing_capture)
    assert editor._spacing_record_current(pane.selected_entry)


@pytest.mark.parametrize("boundary", ["paint", "approve"])
@pytest.mark.parametrize("change", ["source", "appearance"])
def test_spacing_modal_requires_fresh_source_and_appearance_between_event_turns(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    change: str,
) -> None:
    """Presentation, painted evidence, and explicit consent cannot share freshness."""
    from .test_impedance_netclass_dialog import (
        _drive_spacing_viewer,
        _install_spacing_viewer_harness,
    )

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _gap_board(api, 100_000)
    state = {"source": board, "appearance": "original-palette"}
    checks: list[Any] = []

    def verify(expected: Any) -> None:
        """Read the current source anew for each native acceptance event."""
        checks.append(state["source"])
        if state["source"] != expected:
            raise ValueError("The board changed between spacing review events")

    editor = api.dialog.SpecificationDialog(
        None,
        board,
        _config(api).specifications[0],
        preview=api.preview,
        verify_snapshot=verify,
        appearance_context=lambda: state["appearance"],
    )
    editor.on_screen = True
    api.drain()
    pane = editor.preview_pane
    api.paint(pane)
    entry = pane.selected_entry
    assert pane.displayed and len(pane.spacing_entries) == 1
    checks.clear()

    def mutate() -> None:
        """Model a board edit or palette change after the preceding boundary."""
        state[change] = (
            _gap_board(api, 90_000) if change == "source" else "changed-palette"
        )

    def modal(viewer: Any) -> int:
        """Change state between construction, post-paint, and the button event."""
        assert checks == [board]
        if boundary == "paint":
            mutate()
        else:
            displayed = viewer.canvas._displayed

            def after_paint() -> None:
                """Keep the real readable paint before changing the live inputs."""
                displayed()
                assert viewer._display_verified
                mutate()

            viewer.canvas._displayed = after_paint
        _drive_spacing_viewer(api, viewer, approve=boundary == "approve")
        assert not viewer.approved and not viewer.readable
        record = api.dialog.tracking.find_spacing_image(
            editor.review_tracking, entry.spec_id, entry.layer, entry.view_id
        )
        if boundary == "paint":
            assert record is None
        else:
            assert record is not None and record.viewed_digest
            assert not record.approved_digest and not record.approved_at_utc
        viewer.EndModal(api.wx.ID_CANCEL)
        return api.wx.ID_CANCEL

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", modal)
    assert not pane.approve_spacing_image(editor._approve_spacing_capture)
    assert len(checks) == (2 if boundary == "paint" else 3)
    assert not editor._layer_drafts["F.Cu"].confirmed


@pytest.mark.parametrize("entrypoint", ["button", "thumbnail"])
def test_workbook_spacing_open_event_verifies_source_once_before_presentation(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """The owner and shared pane consume one fresh check in the same open event."""
    from .test_impedance_netclass_dialog import (
        _drive_spacing_viewer,
        _install_spacing_viewer_harness,
    )

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _gap_board(api, 100_000)
    checks: list[Any] = []

    def current() -> Any:
        """Read a live board for the owner's real verification handler."""
        checks.append(board)
        return board

    dialog = api.dialog.ImpedanceDialog(None, _config(api), board, api.preview, current)
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    pane = dialog.preview_pane
    api.paint(pane)
    entry = pane.selected_entry
    assert pane.displayed and len(pane.spacing_entries) == 1
    checks.clear()

    def modal(viewer: Any) -> int:
        """Keep one presentation check plus separate post-paint and consent checks."""
        assert checks == [board]
        result = _drive_spacing_viewer(api, viewer)
        assert viewer.approved and checks == [board, board, board]
        return result

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", modal)
    if entrypoint == "button":
        dialog.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    else:
        pane.canvas._on_click(SimpleNamespace(Skip=lambda: None))
    assert dialog.session.spacing_review_current(entry)


@pytest.mark.parametrize("displayed", [False, True])
@pytest.mark.parametrize("change", ["source", "unavailable", "appearance"])
def test_workbook_spacing_open_verifies_even_without_displayed_capture(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    displayed: bool,
    change: str,
) -> None:
    """A missing capture must not bypass row refresh or stale-evidence invalidation."""
    from .test_impedance_netclass_dialog import _install_spacing_viewer_harness

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _gap_board(api, 100_000)
    state = {"source": board, "appearance": "original-palette"}
    checks: list[Any] = []

    def current() -> Any:
        """Refresh rows on actual edits and fail safely when the board is unavailable."""
        checks.append(state["source"])
        if state["source"] is None:
            raise OSError("The live board is unavailable")
        return state["source"]

    dialog = api.dialog.ImpedanceDialog(
        None,
        _config(api),
        board,
        api.preview,
        current,
        appearance_context=lambda: state["appearance"],
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    pane = dialog.preview_pane
    api.paint(pane)
    assert pane.displayed
    if not displayed:
        pane.clear("The capture has not displayed")
    if change == "source":
        state["source"] = _gap_board(api, 90_000)
    elif change == "unavailable":
        state["source"] = None
    else:
        state["appearance"] = "changed-palette"
    checks.clear()
    monkeypatch.setattr(
        api.wx.Dialog,
        "ShowModal",
        lambda _viewer: pytest.fail("Stale spacing pixels cannot open for approval"),
    )
    dialog.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    assert checks == [state["source"]]
    assert not pane.capture_loaded and not pane.displayed
    assert not dialog.session.config.review_tracking.spacing_images
    if change == "source":
        assert dialog.session.snapshot == state["source"]
        assert "board changed" in dialog.status.GetValue().lower()
    elif change == "unavailable":
        assert dialog.session.analysis is None
        assert "unavailable" in dialog.status.GetValue().lower()
    else:
        assert "appearance changed" in dialog.status.GetValue().lower()


def test_workbook_status_reuses_immutable_source_digest_without_skipping_live_checks(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated UI derivation hashes one owned snapshot while every live check still runs."""
    api = draft_api
    board = _gap_board(api, 100_000)
    fresh_reads: list[Any] = []

    def current() -> Any:
        """Keep independent source-boundary reads observable."""
        fresh_reads.append(board)
        return board

    tracking = api.dialog.tracking
    fingerprint = tracking.source_fingerprint
    derivations: list[Any] = []

    def derive(snapshot: Any) -> str:
        """Count actual canonical derivations, preserving their real validation."""
        derivations.append(snapshot)
        return fingerprint(snapshot)

    monkeypatch.setattr(tracking, "source_fingerprint", derive)
    dialog = api.dialog.ImpedanceDialog(None, _config(api), board, api.preview, current)
    spacing_review = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    # Diagnostic input hashing is a separate context; count the session/UI memo
    # without substituting the real fingerprint used by diagnostic derivation.
    monkeypatch.setattr(spacing_review, "source_fingerprint", fingerprint)
    dialog.session.config = replace(
        dialog.session.config,
        review_tracking=tracking.put_layer(
            dialog.session.config.review_tracking,
            tracking.LayerApproval("rf", "F.Cu", "a" * 64, tracking.utc_now()),
        ),
    )
    derivations.clear()  # Constructor diagnostics have their own independent fingerprint.
    fresh_reads.clear()
    for _turn in range(3):
        dialog._verify_review_snapshot()
        dialog._populate_review_status()
    assert fresh_reads == [board, board, board]
    assert derivations == [board]
    assert dialog.session.source_digest == fingerprint(board)

    equal_snapshot = replace(board)
    dialog.session.snapshot = equal_snapshot
    dialog._populate_review_status()
    assert len(derivations) == 2 and derivations[-1] is equal_snapshot
    assert dialog.session.source_digest == fingerprint(board)

    changed = replace(board, context_digest="new-render-source")
    dialog.session.snapshot = changed
    dialog._populate_review_status()
    assert len(derivations) == 3 and derivations[-1] is changed
    assert dialog.session.source_digest == fingerprint(changed) != fingerprint(board)

    invalid = replace(changed, traces=("not a trace",))
    dialog.session.snapshot = invalid
    for _attempt in range(2):
        with pytest.raises(ValueError, match="Every board trace"):
            _ = dialog.session.source_digest
    assert len(derivations) == 5
    dialog.session.snapshot = changed
    dialog._populate_review_status()
    assert (
        len(derivations) == 5
    )  # A failed derivation did not replace the last valid memo.


def _manufacturing_capture_dialog(api: SimpleNamespace, board: Any = None) -> Any:
    """Prepare real trace image history independently of diagnostic approval state."""
    if board is None:
        board = _gap_board(api, 100_000)
    dialog = _open(api, _config(api), board)
    tracking = api.dialog.tracking
    source = tracking.source_fingerprint(board)
    config = dialog.session.config
    specifications = {spec.spec_id: spec for spec in config.specifications}
    history = config.review_tracking
    for section in dialog.session.analysis.sections:
        history = tracking.put_image(
            history,
            tracking.ImageView(
                section.spec_id,
                section.layer,
                section.section_id,
                tracking.capture_fingerprint(
                    source, specifications[section.spec_id], section, api.capture.sha256
                ),
                tracking.utc_now(),
            ),
        )
    dialog.session.config = replace(config, review_tracking=history)
    dialog._capture_expected_key = None
    return dialog


@pytest.mark.parametrize("memo", ["spacing", "capture", "retry"])
def test_review_memos_adopt_equal_snapshot_references(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    memo: str,
) -> None:
    """Equal reconstructed boards are compared once, retaining cached review state."""
    api = draft_api
    dialog = _manufacturing_capture_dialog(api)
    session = dialog.session
    session.spacing_entries
    dialog._review_capture_sections()
    dialog._reset_main_capture_preparation()
    entries = session.spacing_entries
    expected = dialog._capture_expected
    retry = dialog._main_capture_retry_attempts
    retry[session.analysis.sections[0]] = 2
    equal = replace(session.snapshot)
    session.snapshot = equal
    compared: list[object] = []
    original_equal = api.model.BoardSnapshot.__eq__

    def compare(left: Any, right: object) -> bool:
        """Observe full-board equality while preserving its real semantics."""
        compared.append(right)
        return original_equal(left, right)

    monkeypatch.setattr(api.model.BoardSnapshot, "__eq__", compare)

    def read() -> None:
        """Exercise the production memo through its normal public derivation."""
        if memo == "spacing":
            assert session.spacing_entries is entries
        elif memo == "capture":
            dialog._review_capture_sections()
            assert dialog._capture_expected is expected
        else:
            dialog._reset_main_capture_preparation()
            assert dialog._main_capture_retry_attempts is retry
            assert retry[session.analysis.sections[0]] == 2

    read()
    assert compared
    compared.clear()
    for _attempt in range(5):
        read()
    assert compared == []


def test_capture_expectation_failure_keeps_the_previous_coherent_memo(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partially derived replacement cannot overwrite the old key or result."""
    api = draft_api
    board = _separate_gap_concerns_board(api, 100_000)
    board = replace(
        board,
        traces=tuple(
            replace(trace, width_nm=trace.width_nm + 50_000)
            if index in (2, 3)
            else trace
            for index, trace in enumerate(board.traces)
        ),
    )
    dialog = _manufacturing_capture_dialog(api, board)
    sections = dialog._review_capture_sections()
    assert len(sections) >= 2
    old_key = dialog._capture_expected_key
    old_expected = dialog._capture_expected
    old_digest = dialog._capture_source_digest
    old_snapshot = dialog.session.snapshot
    dialog.session.snapshot = replace(old_snapshot, context_digest="new source")
    derive = api.dialog.tracking.expected_capture_digest
    attempts = 0

    def fail_second(*args: Any, **kwargs: Any) -> Any:
        """Fail after one successful result to expose partial-state publication."""
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ValueError("capture derivation failed")
        return derive(*args, **kwargs)

    monkeypatch.setattr(api.dialog.tracking, "expected_capture_digest", fail_second)
    with pytest.raises(ValueError, match="capture derivation failed"):
        dialog._review_capture_sections()
    assert attempts == 2
    assert dialog._capture_expected_key is old_key
    assert dialog._capture_expected is old_expected
    assert dialog._capture_source_digest == old_digest
    dialog.session.snapshot = old_snapshot
    assert dialog._review_capture_sections() == sections
    assert attempts == 2


def test_spacing_history_does_not_rederive_manufacturing_capture_expectations(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostic view/consent changes leave the independent manufacturing memo current."""
    api = draft_api
    dialog = _manufacturing_capture_dialog(api)
    tracking = api.dialog.tracking
    expected_capture = tracking.expected_capture_digest
    derivations: list[Any] = []

    def derive(*args: Any, **kwargs: Any) -> Any:
        """Count real expected-digest derivations without replacing their behavior."""
        derivations.append(args[3])
        return expected_capture(*args, **kwargs)

    monkeypatch.setattr(tracking, "expected_capture_digest", derive)
    sections = dialog._review_capture_sections()
    expected = dict(dialog._capture_expected)
    assert sections and len(derivations) == len(sections)
    derivations.clear()
    entry = dialog.session.spacing_entries[0]
    dialog.session.record_spacing_view(entry, api.capture.sha256, tracking.utc_now())
    assert dialog._review_capture_sections() == sections
    assert dialog._capture_expected == expected and derivations == []
    dialog.session.approve_spacing_image(entry, api.capture.sha256)
    assert dialog._review_capture_sections() == sections
    assert dialog._capture_expected == expected and derivations == []
    assert dialog.session.spacing_review_current(entry)


@pytest.mark.parametrize(
    "change",
    [
        "snapshot",
        "analysis",
        "specification",
        "stackup",
        "width",
        "images",
        "layers",
        "included",
    ],
)
def test_manufacturing_capture_memo_rederives_every_relevant_input(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Physical inputs, manufacturing history, and row inclusion remain cache boundaries."""
    api = draft_api
    dialog = _manufacturing_capture_dialog(api)
    session = dialog.session
    tracking = api.dialog.tracking
    sections = dialog._review_capture_sections()
    assert sections
    config = session.config
    if change == "snapshot":
        session.snapshot = replace(
            session.snapshot, context_digest="changed-render-source"
        )
    elif change == "analysis":
        session.analysis = replace(session.analysis, digest="b" * 64)
    elif change == "specification":
        session.config = replace(
            config,
            specifications=(replace(config.specifications[0], target_ohms="100"),),
        )
    elif change == "stackup":
        session.config = replace(
            config,
            stackup=api.dialog.Stackup("two-layer", "Two layer", 2, "1.6", "1", ""),
        )
    elif change == "width":
        session.config = replace(
            config,
            width_results=(
                api.dialog.WidthResult("rf", "F.Cu", "a" * 64, "success", 250_000),
            ),
        )
    elif change == "images":
        session.config = replace(
            config,
            review_tracking=replace(
                config.review_tracking,
                images=(
                    replace(config.review_tracking.images[0], capture_digest="b" * 64),
                )
                + config.review_tracking.images[1:],
            ),
        )
    elif change == "layers":
        session.config = replace(
            config,
            review_tracking=tracking.put_layer(
                config.review_tracking,
                tracking.LayerApproval("rf", "F.Cu", "a" * 64, tracking.utc_now()),
            ),
        )
    else:
        session.included.remove(sections[0].section_id)
    expected_capture = tracking.expected_capture_digest
    derivations: list[Any] = []

    def derive(*args: Any, **kwargs: Any) -> Any:
        """Keep real expected-digest behavior while observing memo invalidation."""
        derivations.append(args[3])
        return expected_capture(*args, **kwargs)

    monkeypatch.setattr(tracking, "expected_capture_digest", derive)
    dialog._review_capture_sections()
    included = tuple(
        section
        for section in session.analysis.sections
        if section.section_id in session.included
    )
    assert derivations == list(included)
    assert set(dialog._capture_expected) == set(included)
    assert dialog._capture_source_digest == tracking.source_fingerprint(
        session.snapshot
    )
    if change == "images":
        assert "b" * 64 in dialog._capture_expected.values()


def _separate_gap_concerns_board(api: SimpleNamespace, gap_nm: int) -> Any:
    """Keep three physical concerns so an unrelated cold capture survives a retry."""
    board = _gap_board(api, gap_nm)
    return replace(
        board,
        traces=tuple(
            replace(
                trace,
                trace_id=f"{trace.trace_id}-concern-{index}",
                points=tuple((x + offset, y) for x, y in trace.points),
            )
            for index, offset in enumerate((0, 10_000_000, 20_000_000))
            for trace in board.traces[:2]
        )
        + board.traces[2:],
    )


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


@pytest.mark.parametrize("owner", ["specification", "workbook"])
def test_compact_spacing_action_uses_existing_owner_review_handler(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """The compact preview opens its owner's approval workflow without consent."""
    api = draft_api
    preview_module = importlib.import_module(api.dialog.__package__ + ".dialog_preview")
    construct = preview_module.WorkbookPreview.__init__
    callbacks: list[Any] = []

    def observe_constructor(preview: Any, *args: Any, **kwargs: Any) -> None:
        """Retain real controls while observing only the owner's action boundary."""
        callbacks.append(kwargs.get("open_spacing_review"))
        construct(preview, *args, **kwargs)

    monkeypatch.setattr(preview_module.WorkbookPreview, "__init__", observe_constructor)
    config, board = _config(api), _gap_board(api, 100_000)
    if owner == "specification":
        dialog = api.dialog.SpecificationDialog(
            None, board, config.specifications[0], preview=api.preview
        )
        handler = "_on_approve_layer"
    else:
        dialog = _open(api, config, board)
        handler = "_on_approve_spacing_image"
    assert len(callbacks) == 1 and callable(callbacks[0])
    events: list[Any] = []
    monkeypatch.setattr(dialog, handler, events.append)
    history = (
        dialog.review_tracking
        if owner == "specification"
        else dialog.session.config.review_tracking
    )
    callbacks[0]()
    assert events == [None]
    assert (
        dialog.review_tracking
        if owner == "specification"
        else dialog.session.config.review_tracking
    ) == history


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


@pytest.mark.parametrize("owner", ["specification", "workbook"])
def test_spacing_images_open_first_and_keep_manual_trace_selection(
    draft_api: SimpleNamespace, owner: str
) -> None:
    """Fresh review starts at concerns while explicit trace navigation stays available."""
    api = draft_api
    config, board = _config(api), _gap_board(api, 100_000)
    if owner == "specification":
        dialog = api.dialog.SpecificationDialog(
            None, board, config.specifications[0], preview=api.preview
        )
        refresh = dialog._update_controls
    else:
        dialog = _open(api, config, board)
        refresh = dialog._populate_sections
    pane = dialog.preview_pane
    assert pane.spacing_entries
    assert pane.entries == pane.spacing_entries + pane.sections
    assert pane.selected_entry == pane.spacing_entries[0]

    refresh()
    assert pane.selected_entry == pane.spacing_entries[0]
    if owner == "workbook":
        dialog._on_preview(0)
    else:
        pane.checklist.Select(len(pane.spacing_entries))
        pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    assert pane.selected_entry == pane.sections[0]
    refresh()
    assert pane.selected_entry == pane.sections[0]
    pane.select(0)
    refresh()
    assert pane.selected_entry == pane.spacing_entries[0]


def test_initial_spacing_then_manual_trace_survives_same_context_retry(
    draft_api: SimpleNamespace,
) -> None:
    """Continuing a review retries the chosen row without inventing spacing consent."""
    api = draft_api
    board = _gap_board(api, 100_000)
    dialog = _open(api, _config(api), board)
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    pane = dialog.preview_pane
    api.paint(pane)
    entries = pane.spacing_entries
    assert entries and pane.selected_entry == entries[0]
    assert pane.displayed
    row = len(dialog._row_ids) - 1
    section_id = dialog._row_ids[row]
    dialog.sections._on_cell_click(
        SimpleNamespace(GetRow=lambda: row, GetCol=lambda: 2, Skip=lambda: None)
    )
    api.drain()
    api.paint(pane)
    assert pane.selected_entry.section_id == section_id
    assert pane.displayed

    dialog._populate_sections()
    assert pane.selected_entry.section_id == section_id
    dialog.retry_button.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(pane)

    assert pane.selected_entry.section_id == section_id
    assert pane.displayed
    assert pane.spacing_entries == entries
    assert dialog.session.pending_spacing_reviews == entries
    assert dialog.session.config.review_tracking.spacing_images == ()
    assert not dialog.session.approved


def test_main_prepares_spacing_before_workbook_images(
    draft_api: SimpleNamespace,
) -> None:
    """Cold speculative captures follow the same concern-first presentation order."""
    dialog = _open(draft_api, _config(draft_api), _gap_board(draft_api, 100_000))
    assert dialog.session.spacing_entries
    assert dialog._capture_prepare_sections == (
        dialog.session.spacing_entries + dialog.session.analysis.sections
    )


def test_new_snapshot_concern_precedes_surviving_manual_trace_selection(
    draft_api: SimpleNamespace,
) -> None:
    """An actual selected trace survives in the grid while a new concern opens first."""
    _assert_new_snapshot_concern_priority(draft_api, pending=False)


def test_new_snapshot_concern_precedes_pending_grid_selection(
    draft_api: SimpleNamespace,
) -> None:
    """An older queued grid gesture cannot bypass a newly discovered spacing concern."""
    _assert_new_snapshot_concern_priority(draft_api, pending=True)


def _assert_new_snapshot_concern_priority(
    api: SimpleNamespace, *, pending: bool
) -> None:
    """Refresh real rows with a surviving settled or pending trace selection."""
    board = _gap_board(api, 200_000)
    board = replace(
        board,
        traces=board.traces
        + tuple(
            replace(
                trace,
                trace_id=trace.trace_id + "-other",
                net=trace.net + "-other",
                points=tuple((x, y + 5_000_000) for x, y in trace.points),
            )
            for trace in board.traces[:2]
        ),
        net_class_memberships=board.net_class_memberships
        + (("CLK_P-other", ("RF",)), ("CLK_N-other", ("RF",))),
        differential_pairs=board.differential_pairs + (("CLK_P-other", "CLK_N-other"),),
    )
    dialog = _open(api, _config(api), board)
    assert not dialog.session.spacing_entries
    row = next(
        index
        for index, section in enumerate(dialog.session.analysis.sections)
        if "CLK_P-other" in section.net_names
    )
    selected_id = dialog._row_ids[row]
    if pending:
        dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=row, column=4)
    else:
        dialog.sections.SelectRow(row)
        dialog._on_preview(row)
    changed = replace(
        board,
        traces=tuple(
            replace(trace, points=((0, 250_000), (2_000_000, 250_000)))
            if trace.trace_id == "track-b"
            else trace
            for trace in board.traces
        ),
    )

    assert dialog._refresh_rows(changed)
    api.drain()

    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
    assert dialog.session.spacing_entries
    assert dialog.preview_pane.selected_entry == dialog.session.spacing_entries[0]


def test_saved_manufacturing_verification_precedes_speculative_spacing(
    draft_api: SimpleNamespace,
) -> None:
    """Concern-first display retains cheap restoration of already approved rows."""
    from .test_impedance_netclass_dialog import apply_layer

    api = draft_api
    config, board = _config(api), _gap_board(api, 100_000)
    editor = api.dialog.SpecificationDialog(
        None, board, config.specifications[0], preview=api.preview
    )
    editor.on_screen = True
    apply_layer(api, editor)
    assert editor._layer_drafts["F.Cu"].confirmed
    session = api.dialog.ReviewSession(
        replace(config, review_tracking=editor.review_tracking), board
    )
    session.refresh()
    session.capture_digests.update(editor.capture_digests)
    session.spacing_capture_digests.update(editor.spacing_capture_digests)
    session.approve()
    saved = session.save_result()
    saved = replace(
        saved, review_tracking=replace(saved.review_tracking, spacing_images=())
    )
    dialog = _open(api, saved, board)
    assert dialog._capture_restore_pending
    assert dialog.preview_pane.selected_entry == dialog.session.spacing_entries[0]
    assert dialog._capture_prepare_sections == (
        dialog._review_capture_sections() + dialog.session.spacing_entries
    )
    assert dialog.session.config.review_tracking == saved.review_tracking


def test_editor_preparation_restores_layer_with_trace_sections_only(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate diagnostic preparation never enters a manufacturing layer fingerprint."""
    api = draft_api
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 100_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    api.drain()
    editor._capture_first_paint = True
    traces = editor._sections_for_layer("F.Cu")
    entries = editor._spacing_entries_for_layer("F.Cu")
    assert entries
    editor._capture_hashes.update((section, api.capture.sha256) for section in traces)
    editor._spacing_capture_hashes.update(
        (entry, api.capture.sha256) for entry in entries
    )
    restored: list[Any] = []
    monkeypatch.setattr(
        editor,
        "_restore_layer_approval",
        lambda layer, sections: restored.append((layer, sections)),
    )
    editor._on_capture_prepare_timer(SimpleNamespace())
    assert restored == [("F.Cu", traces)]


def test_editor_layer_switch_binds_only_current_layer_spacing(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No intermediate review binding may install the previous layer's concerns."""
    api = draft_api
    board = _gap_board(api, 100_000)
    board = replace(
        board,
        traces=board.traces
        + tuple(
            replace(trace, trace_id=trace.trace_id + "-back", layer="B.Cu")
            for trace in board.traces[:2]
        ),
    )
    spec = _config(api).specifications[0]
    spec = replace(
        spec,
        layer_settings=spec.layer_settings
        + (api.model.LayerSettings("B.Cu", ("F.Cu",), 200_000),),
    )
    editor = api.dialog.SpecificationDialog(None, board, spec, preview=api.preview)
    bind = editor.preview_pane.set_sections
    bound: list[Any] = []

    def observe(sections: Any, context: Any, *args: Any, **kwargs: Any) -> None:
        """Assert the full model as it crosses the real preview binding boundary."""
        spacing = kwargs.get("spacing_entries", ())
        assert spacing and all(entry.layer == "B.Cu" for entry in spacing)
        assert all(section.layer == "B.Cu" for section in sections)
        bound.append(spacing)
        bind(sections, context, *args, **kwargs)

    monkeypatch.setattr(editor.preview_pane, "set_sections", observe)
    editor.layer.SetSelection(editor._layer_names.index("B.Cu"))
    editor.layer.emit(api.wx.EVT_CHOICE)
    assert bound == [editor.preview_pane.spacing_entries]
    assert editor.preview_pane.selected_entry == editor.preview_pane.spacing_entries[0]


def test_editor_prepares_pending_spacing_before_trace_captures(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor warms concerns first even when no image has a verified hash yet."""
    api = draft_api
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 100_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    api.drain()
    editor._capture_first_paint = True
    editor._capture_hashes.clear()
    editor._spacing_capture_hashes.clear()
    requests: list[Any] = []
    monkeypatch.setattr(
        editor, "_prepared_preview", lambda entry, **_kwargs: requests.append(entry)
    )
    editor._on_capture_prepare_timer(SimpleNamespace())
    assert requests == [editor._spacing_entries_for_layer("F.Cu")[0]]


def test_compact_spacing_paint_releases_preparation_without_review_events(
    draft_api: SimpleNamespace,
) -> None:
    """Concern-first display must not leave manufacturing evidence permanently cold."""
    api = draft_api
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 100_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    api.drain()
    assert editor.preview_pane.capture_loaded
    assert not editor.preview_pane.ready
    assert not editor._capture_first_paint
    api.paint(editor.preview_pane)
    assert editor.preview_pane.ready
    api.finish_preparation(editor)
    assert all(
        section in editor._capture_hashes for section in editor.preview_pane.sections
    )
    assert editor.review_tracking.images == ()
    assert editor.review_tracking.spacing_images == ()
    assert editor.review_tracking.layers == ()


def test_spacing_approvals_advance_to_spacing_then_required_traces(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit concern consent advances in order, without a manual-trace gate."""
    from .test_impedance_netclass_dialog import _install_spacing_viewer_harness

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 100_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    pane = editor.preview_pane
    entries = pane.spacing_entries
    assert entries
    # Manual trace choice is allowed before concerns have been approved.
    pane.select(len(entries))
    api.drain()
    api.paint(pane)
    assert pane.selected_section == pane.sections[0]
    assert pane.displayed
    assert not editor.review_tracking.spacing_images
    pane.select(0)
    for index, entry in enumerate(entries):
        api.drain()
        api.paint(pane)
        assert pane.selected_entry == entry
        editor.approve_layer.emit(api.wx.EVT_BUTTON)
        api.drain()
        record = next(
            record
            for record in editor.review_tracking.spacing_images
            if record.view_id == entry.view_id
        )
        assert record.approved_digest and record.approved_at_utc
        assert not editor._layer_drafts["F.Cu"].confirmed
        assert pane.selected_entry == (
            entries[index + 1] if index + 1 < len(entries) else pane.sections[0]
        )
    api.paint(pane)
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._layer_drafts["F.Cu"].confirmed


def test_spacing_failure_exports_separate_pending_evidence_without_losing_traces(
    draft_api: SimpleNamespace,
) -> None:
    """Applying after a failed concern preserves verified manufacturing evidence."""
    api = draft_api
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 100_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    api.drain()
    api.paint(editor.preview_pane)
    api.finish_preparation(editor)
    entry = editor.preview_pane.selected_entry
    trace_digests = editor.capture_digests
    assert trace_digests and all(trace_digests.values())
    history = editor.review_tracking
    editor.preview_pane._report_failure("The current spacing image could not paint")
    assert editor.capture_digests == trace_digests
    assert editor.spacing_capture_digests[entry.view_id] is None
    assert editor.review_tracking == history
    assert entry in editor._pending_spacing_entries("F.Cu")


def test_remaining_spacing_precedes_failed_trace_and_retains_final_failure_gate(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concern approvals stay first; an earlier trace failure still blocks the layer."""
    from .test_impedance_netclass_dialog import _install_spacing_viewer_harness

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _separate_gap_concerns_board(api, 100_000)
    editor = api.dialog.SpecificationDialog(
        None, board, _config(api).specifications[0], preview=api.preview
    )
    editor.on_screen = True
    pane = editor.preview_pane
    entries = pane.spacing_entries
    assert len(entries) == 3
    assert len({entry.issue_id for entry in entries}) == 3
    trace = pane.sections[0]
    pane.select(pane.entries.index(trace))
    api.drain()
    api.paint(pane)
    pane._report_failure("Known trace paint failure")
    assert trace in editor._capture_display_failed
    history = editor.review_tracking
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert pane.selected_entry == entries[0]
    assert trace in editor._capture_display_failed
    assert editor.review_tracking == history
    assert not editor.review_tracking.layers
    for index, entry in enumerate(entries):
        api.drain()
        api.paint(pane)
        assert pane.selected_entry == entry
        editor.approve_layer.emit(api.wx.EVT_BUTTON)
        assert not editor._layer_drafts["F.Cu"].confirmed
        assert trace in editor._capture_display_failed
        assert pane.selected_entry == (
            entries[index + 1] if index + 1 < len(entries) else trace
        )
    assert "Image 1 must display successfully" in editor.validation_message.GetLabel()
    assert not editor._pending_spacing_entries("F.Cu")
    api.drain()
    api.paint(pane)
    assert trace not in editor._capture_display_failed
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._layer_drafts["F.Cu"].confirmed


@pytest.mark.parametrize("owner", ["specification", "workbook"])
@pytest.mark.parametrize(
    "review", ["spacing_cancel", "spacing_failure", "spacing_nested", "trace"]
)
def test_enlarged_review_pauses_and_resumes_unrelated_capture_preparation(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    review: str,
) -> None:
    """Nested native event turns cannot rasterize cold rows behind a review window."""
    from .test_impedance_netclass_dialog import (
        _drive_spacing_viewer,
        _install_spacing_viewer_harness,
    )

    api = draft_api
    _install_spacing_viewer_harness(api, monkeypatch)
    board = _separate_gap_concerns_board(api, 100_000)
    captures: list[Any] = []
    verifications: list[Any] = []

    def preview(entry: Any, **kwargs: Any) -> Any:
        """Observe actual cold-provider work while returning immutable fixture pixels."""
        captures.append(entry)
        return api.preview(entry, **kwargs)

    def current_snapshot() -> Any:
        """Observe the main owner's real source verification boundary."""
        verifications.append(board)
        return board

    if owner == "specification":
        dialog = api.dialog.SpecificationDialog(
            None,
            board,
            _config(api).specifications[0],
            preview=preview,
            verify_snapshot=verifications.append,
        )
    else:
        dialog = api.dialog.ImpedanceDialog(
            None,
            _config(api),
            board,
            preview,
            current_snapshot,
            verify_snapshot=verifications.append,
        )
    dialog.on_screen = True
    if owner == "workbook":
        dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    pane = dialog.preview_pane
    api.paint(pane)
    assert len(pane.spacing_entries) == 3
    assert len({entry.issue_id for entry in pane.spacing_entries}) == 3
    if review == "trace":
        pane.select(pane.entries.index(pane.sections[0]))
        api.drain()
        api.paint(pane)
    assert pane.displayed
    retry_entry = pane.spacing_entries[-1]
    retry_at = api.dialog.time.monotonic() + 120
    if owner == "specification":
        dialog._capture_retry_at[retry_entry] = retry_at
        dialog._capture_retry_attempts[retry_entry] = 2
    else:
        dialog._main_capture_retry_at[retry_entry] = retry_at
        dialog._main_capture_retry_attempts[retry_entry] = 2
    idle_wakes: list[bool] = []
    monkeypatch.setattr(
        api.wx, "WakeUpIdle", lambda: idle_wakes.append(pane.review_window_open)
    )

    def prepare_turns() -> None:
        """Deliver the owner's real idle/timer handlers during a nested modal loop."""
        for _turn in range(3):
            if owner == "specification":
                dialog._capture_prepare_timer.running = False
                dialog._on_capture_prepare_timer(SimpleNamespace())
            else:
                dialog._on_main_capture_idle(
                    SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
                )
            api.drain()

    def capture_state() -> Any:
        """Retain queues, evidence, and retry deadlines across each paused turn."""
        if owner == "specification":
            return (
                dict(dialog._capture_hashes),
                dict(dialog._spacing_capture_hashes),
                dict(dialog._capture_retry_at),
                dict(dialog._capture_retry_attempts),
            )
        return (
            dialog._capture_prepare_sections,
            dict(dialog.session.capture_digests),
            dict(dialog.session.spacing_capture_digests),
            dict(dialog._main_capture_retry_at),
            dict(dialog._main_capture_retry_attempts),
        )

    depth = 0
    modal_calls: list[int] = []

    def modal(viewer: Any) -> int:
        """Exercise actual modal, Cancel, failure, and nested windows without consent."""
        nonlocal depth
        depth += 1
        modal_calls.append(depth)
        try:
            before, state = len(captures), capture_state()
            if owner == "specification":
                assert not dialog._capture_prepare_timer.IsRunning()
                dialog._schedule_capture_preparation()
                assert not dialog._capture_prepare_timer.IsRunning()
            prepare_turns()
            assert len(captures) == before, (
                "Cold captures ran behind an enlarged review"
            )
            assert capture_state() == state
            assert pane.review_window_open
            assert pane._active()
            checks_before = len(verifications)
            pane._verify()
            assert len(verifications) > checks_before
            if depth == 1 and review == "spacing_nested":
                pane._open_enlarged_capture()
                assert pane.review_window_open
                prepare_turns()
                assert len(captures) == before
            if depth == 1 and review == "spacing_failure":
                raise ValueError("The enlarged review could not display")
            if depth == 1 and review != "trace":
                return _drive_spacing_viewer(api, viewer, approve=False)
            viewer.EndModal(api.wx.ID_CANCEL)
            return api.wx.ID_CANCEL
        finally:
            depth -= 1

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", modal)
    before = len(captures)
    if review == "trace":
        pane.canvas._on_click(SimpleNamespace(Skip=lambda: None))
    elif owner == "specification":
        dialog.approve_layer.emit(api.wx.EVT_BUTTON)
    else:
        dialog.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    assert modal_calls == ([1, 2] if review == "spacing_nested" else [1])
    assert not pane.review_window_open
    assert len(captures) == before
    assert verifications
    if owner == "specification":
        assert dialog._capture_prepare_timer.IsRunning()
        history = dialog.review_tracking
    else:
        history = dialog.session.config.review_tracking
        assert idle_wakes and not any(idle_wakes)
    assert not history.layers
    assert not any(record.approved_digest for record in history.spacing_images)
    prepare_turns()
    assert len(captures) > before
    assert retry_entry not in captures[before:]
    retry_times = (
        dialog._capture_retry_at
        if owner == "specification"
        else dialog._main_capture_retry_at
    )
    assert retry_times[retry_entry] == retry_at
