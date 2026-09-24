"""Saving board intent is transactional and distinct from approving its report.

These regressions exercise the real session, constructor, and bound Save handlers.
Native wx layout and modal default handling are outside this stateful control
harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from .test_impedance_dialog import (
    _ConstructedControl,
    _snapshot,
    constructor_api,  # noqa: F401 -- shared real-constructor fixture
)

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import BoardSnapshot, Config
    from impedance.stackup_model import Stackup


class _NotebookControl(_ConstructedControl):
    """Retain notebook pages and selection independently of choice text."""

    def AddPage(self, page: object, text: str) -> None:
        """Add the real child page and select the first page automatically."""
        self.items.append(text)
        self.selection = max(self.selection, 0)


@pytest.fixture
def draft_api(
    constructor_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Extend only this suite's constructor surface for the summary notebook."""
    monkeypatch.setattr(constructor_api.wx, "Notebook", _NotebookControl, raising=False)

    def show(control: _ConstructedControl, shown: bool = True) -> bool:
        """Retain actual visibility independently of enabled state or layout."""
        previous = getattr(control, "shown", True)
        control.shown = shown
        return previous != shown

    def is_shown(control: _ConstructedControl) -> bool:
        """Read control visibility; ordinary children start shown as in wx."""
        return getattr(control, "shown", True)

    def is_on_screen(control: _ConstructedControl) -> bool:
        """Require all ancestors and the modal root to have become visible."""
        if not is_shown(control):
            return False
        if isinstance(control.parent, _ConstructedControl):
            return is_on_screen(control.parent)
        return getattr(control, "on_screen", False)

    monkeypatch.setattr(_ConstructedControl, "Show", show, raising=False)
    monkeypatch.setattr(
        _ConstructedControl, "Hide", lambda control: show(control, False), raising=False
    )
    monkeypatch.setattr(_ConstructedControl, "IsShown", is_shown, raising=False)
    monkeypatch.setattr(
        _ConstructedControl, "IsShownOnScreen", is_on_screen, raising=False
    )
    monkeypatch.setattr(
        _ConstructedControl,
        "GetCount",
        lambda control: len(control.items),
        raising=False,
    )
    monkeypatch.setattr(
        _ConstructedControl,
        "GetFirstSelected",
        lambda control: control.selection,
        raising=False,
    )

    def skip_catalog(_dialog: ImpedanceDialog, _snapshot: BoardSnapshot) -> None:
        """Keep unrelated catalog/network workers outside persistence regressions."""

    monkeypatch.setattr(
        constructor_api.dialog.ImpedanceDialog, "_sync_catalog_count", skip_catalog
    )
    return constructor_api


def _board(api: SimpleNamespace) -> BoardSnapshot:
    """Provide current net-class membership without removed width selectors."""
    return replace(
        _snapshot(api),
        net_classes=("Default", "RF"),
        net_class_memberships=(
            ("CLK_P", ("RF",)),
            ("CLK_N", ("RF",)),
            ("GND", ("Default",)),
        ),
        net_class_context_digest="draft-save-class-context",
    )


def _intent(api: SimpleNamespace, *, enabled: bool = True) -> Config:
    """Use the supported class-based single-ended specification contract."""
    specification = api.model.Specification(
        spec_id="rf",
        label="RF",
        target_ohms="50",
        kind="single_ended",
        net_class="RF",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    return api.model.Config(enabled=enabled, specifications=(specification,))


def _stackup(api: SimpleNamespace) -> Stackup:
    """Select compatible vendor intent without invoking a catalog service."""
    return api.dialog.Stackup("two-layer", "Two layer", 2, "1.6", "1", "")


def _approved(api: SimpleNamespace, snapshot: BoardSnapshot) -> Config:
    """Create genuine prior explicit approval using the production session API."""
    session = api.dialog.ReviewSession(_intent(api), snapshot)
    session.refresh()
    session.approve()
    return session.save_result()


def _open(
    api: SimpleNamespace,
    config: Config,
    snapshot: BoardSnapshot,
    save_config: Optional[Callable[[Config], None]] = None,
) -> ImpedanceDialog:
    """Construct the actual dialog with optional in-modal persistence."""
    return api.dialog.ImpedanceDialog(
        None,
        config,
        snapshot,
        api.preview,
        lambda: snapshot,
        save_config=save_config,
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_stackup_only_can_be_saved_and_reopened_without_report_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """A board can persist its selected construction before it has specifications."""
    api = draft_api
    snapshot = _board(api)
    original = api.model.Config(enabled=enabled)
    persisted = {"config": original}
    dialog = _open(
        api, original, snapshot, lambda config: persisted.update(config=config)
    )
    selected = _stackup(api)
    child_state = {"destroyed": False}

    class AcceptedPicker:
        """Model the child's accepted immutable result, not its native rendering."""

        def __init__(
            self, parent: object, count: int, current: object, **_kwargs: object
        ) -> None:
            assert parent is dialog
            assert count == 2
            assert current is None
            self.stackup = selected

        def ShowModal(self) -> int:
            return api.wx.ID_OK

        def Destroy(self) -> None:
            child_state["destroyed"] = True

    module_name = api.dialog.__package__ + ".stackup_dialog"
    picker_module = ModuleType(module_name)
    picker_module.StackupDialog = AcceptedPicker
    monkeypatch.setitem(sys.modules, module_name, picker_module)
    monkeypatch.setattr(dialog, "_ensure_catalog_started", lambda _count: object())
    dialog.stackup_button.emit(api.wx.EVT_BUTTON)
    assert child_state["destroyed"] is True
    assert persisted["config"] is original

    button = dialog.FindWindow(api.wx.ID_OK)
    assert button.enabled is True
    assert button.GetLabel() == "Save and close"
    assert dialog.review_status.GetLabel() == "No workbook rows to approve"
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)

    assert dialog.modal_result == api.wx.ID_OK
    assert persisted["config"].stackup == selected
    assert persisted["config"].enabled is enabled
    assert persisted["config"].reviewed_digest == ""
    assert persisted["config"].included_section_ids == ()
    restored = api.model.Config.from_dict(persisted["config"].to_dict())
    reopened = _open(api, restored, snapshot)
    assert reopened.session.config.stackup == selected
    assert selected.name in reopened.stackup_summary.GetLabel()
    assert reopened.FindWindow(api.wx.ID_OK).enabled is True


def test_database_failure_preserves_open_dialog_and_allows_retry(
    draft_api: SimpleNamespace,
) -> None:
    """Persistence must succeed before closing or publishing an accepted result."""
    api = draft_api
    snapshot = _board(api)
    original = _approved(api, snapshot)
    attempted: list[Config] = []
    dialog: ImpedanceDialog

    def persist(config: Config) -> None:
        """Fail once while checking that no native close preceded the write."""
        assert dialog.modal_result is None
        assert dialog._closing is False
        attempted.append(config)
        if len(attempted) == 1:
            raise ValueError("Another window changed this board's settings.")

    dialog = _open(api, original, snapshot, persist)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)

    assert attempted == [original]
    assert dialog.config is original
    assert dialog.session.config == original
    assert dialog.session.config.reviewed_digest == original.reviewed_digest
    assert dialog._closing is False
    assert dialog.modal_result is None
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "Another window" in dialog.status.GetValue()

    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert attempted == [original, original]
    assert dialog.modal_result == api.wx.ID_OK


def test_failed_draft_write_retains_unsaved_selection(
    draft_api: SimpleNamespace,
) -> None:
    """A failed write leaves the edited stackup available, not the old database row."""
    api = draft_api
    original = api.model.Config()

    def fail(_config: Config) -> None:
        """Simulate a full disk or unavailable project database."""
        raise OSError("Could not save project database.")

    dialog = _open(api, original, _board(api), fail)
    selected = _stackup(api)
    dialog.session.replace_calculation_context(selected, ())
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.config is original
    assert dialog.session.config.stackup == selected
    assert dialog.modal_result is None
    assert dialog._closing is False
    assert "Could not save" in dialog.status.GetValue()


def test_cancel_discards_session_without_calling_persistence(
    draft_api: SimpleNamespace,
) -> None:
    """The child selection stays transactional until the parent Save succeeds."""
    api = draft_api
    original = api.model.Config()
    writes: list[Config] = []
    skipped: list[bool] = []
    dialog = _open(api, original, _board(api), writes.append)
    dialog.session.replace_calculation_context(_stackup(api), ())
    handler = dialog.bindings[api.wx.EVT_BUTTON, api.wx.ID_CANCEL]
    handler(SimpleNamespace(Skip=lambda: skipped.append(True)))
    assert writes == []
    assert dialog.config is original
    assert skipped == [True]
    assert dialog._closing is True


def test_busy_calculation_does_not_block_save(
    draft_api: SimpleNamespace,
) -> None:
    """Saving draft settings cancels in-flight width work instead of blocking."""
    api = draft_api
    writes: list[Config] = []
    dialog = _open(api, _intent(api), _board(api), writes.append)
    cancellation = Event()
    dialog._calculation_cancel = cancellation
    dialog._populate_sections()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert cancellation.is_set()
    assert len(writes) == 1
    assert dialog.modal_result == api.wx.ID_OK


@pytest.mark.parametrize("enabled", [False, True])
def test_reopening_and_saving_unchanged_approval_mints_no_audit_events(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Saving settings is not an image-view event or a new explicit approval."""
    api = draft_api
    snapshot = _board(api)
    config = _approved(api, snapshot)
    tracking = api.dialog.tracking
    history = tracking.ReviewTracking(
        images=(
            tracking.ImageView(
                "rf", "F.Cu", "historical-row", "a" * 64, "2026-09-01T12:00:00.000000Z"
            ),
        ),
        layers=(
            tracking.LayerApproval(
                "rf", "F.Cu", "b" * 64, "2026-09-01T12:01:00.000000Z"
            ),
        ),
    )
    config = replace(config, enabled=enabled, review_tracking=history)

    def no_new_timestamp() -> str:
        """Prevent saving alone from manufacturing a human-review event."""
        pytest.fail("Saving settings must not create a review timestamp")

    monkeypatch.setattr(tracking, "utc_now", no_new_timestamp)
    preview_module = importlib.import_module(api.dialog.__package__ + ".dialog_preview")
    monkeypatch.setattr(preview_module, "utc_now", no_new_timestamp)
    dialog = _open(api, config, snapshot)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    api.drain()
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.config == config
    assert dialog.config.review_tracking == history
    assert dialog.session.config.review_tracking == history


@pytest.mark.parametrize("change", ["stackup", "specification", "reset", "include"])
def test_changed_intent_or_reset_cannot_save_old_export_authorization(
    draft_api: SimpleNamespace, change: str
) -> None:
    """Persisting partial work must not restore the last approved report digest."""
    api = draft_api
    snapshot = _board(api)
    approved = _approved(api, snapshot)
    session = api.dialog.ReviewSession(approved, snapshot)
    if change == "stackup":
        session.replace_calculation_context(_stackup(api), ())
    elif change == "specification":
        session.replace_specifications(
            (replace(approved.specifications[0], target_ohms="55"),)
        )
    else:
        session.refresh()
        if change == "reset":
            session.invalidate_review()
        if change == "include":
            session.approve()
            assert session.config.reviewed_digest
            session.set_included(())
    candidate = session.save_result(snapshot)
    assert candidate.enabled is True
    assert candidate.reviewed_digest == ""
    assert candidate.included_section_ids == ()
    assert candidate.review_tracking == approved.review_tracking


def test_no_op_inclusion_update_preserves_existing_explicit_approval(
    draft_api: SimpleNamespace,
) -> None:
    """A redundant native checkbox event does not change the approved row choice."""
    api = draft_api
    session = api.dialog.ReviewSession(_intent(api), _board(api))
    session.refresh()
    session.approve()
    approved = session.save_result()
    session.set_included(tuple(session.included))
    assert session.approved is True
    assert session.save_result() == approved


@pytest.mark.parametrize("enabled", [False, True])
def test_board_change_saves_draft_without_reauthorizing_stale_captures(
    draft_api: SimpleNamespace,
    enabled: bool,
) -> None:
    """A changed board invalidates the report, not the ability to keep settings."""
    api = draft_api
    before = _board(api)
    approved = replace(_approved(api, before), enabled=enabled)
    after = replace(before, context_digest="changed board text")
    writes: list[Config] = []
    dialog = _open(api, approved, before, writes.append)
    dialog.refresh_snapshot = lambda: after
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert len(writes) == 1
    assert writes[0].enabled is enabled
    assert writes[0].specifications == approved.specifications
    assert writes[0].reviewed_digest == ""
    assert writes[0].included_section_ids == ()
    assert writes[0].review_tracking == approved.review_tracking


def test_visual_failure_revokes_report_without_disabling_draft_save(
    draft_api: SimpleNamespace,
) -> None:
    """A capture failure cannot preserve approval or strand otherwise valid edits."""
    api = draft_api
    snapshot = _board(api)
    dialog = _open(api, _approved(api, snapshot), snapshot)
    dialog._invalidate_visual_review()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "needs approval" in dialog.review_status.GetLabel().lower()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.config.reviewed_digest == ""
    assert dialog.config.included_section_ids == ()


def test_snapshot_refresh_failure_keeps_dialog_open_but_revokes_review(
    draft_api: SimpleNamespace,
) -> None:
    """An unreadable or replaced native board cannot be saved as current approval."""
    api = draft_api
    snapshot = _board(api)
    original = _approved(api, snapshot)
    writes: list[Config] = []
    dialog = _open(api, original, snapshot, writes.append)

    def missing_board() -> BoardSnapshot:
        raise ValueError("The original board is no longer open.")

    dialog.refresh_snapshot = missing_board
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert writes == []
    assert dialog.modal_result is None
    assert dialog._closing is False
    assert dialog.config is original
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.session.config.included_section_ids == ()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert dialog.review_status.GetLabel() == "No workbook rows to approve"
    assert "no longer open" in dialog.status.GetValue()


def test_stackup_layer_mismatch_can_be_saved_as_draft(
    draft_api: SimpleNamespace,
) -> None:
    """Changing board construction does not discard its old editable stackup intent."""
    api = draft_api
    selected = replace(_stackup(api), layer_count=4, inner_copper_oz="0.5")
    original = api.model.Config(enabled=True, stackup=selected)
    snapshot = _board(api)
    dialog = _open(api, original, snapshot)
    assert "layer count differs" in dialog.stackup_summary.GetLabel()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.config.stackup == selected
    assert dialog.config.enabled is True
    assert dialog.config.reviewed_digest == ""


def test_edit_and_remove_require_a_selected_specification(
    draft_api: SimpleNamespace,
) -> None:
    """Edit/Remove stay inactive until a specifications row is selected."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    edit, remove = dialog._selection_buttons
    assert dialog.specifications.GetItemCount() == 1
    assert dialog.specifications.GetFirstSelected() < 0
    assert edit.enabled is False
    assert remove.enabled is False

    dialog.specifications.selection = 0
    dialog._update_selection_buttons()
    assert edit.enabled is True
    assert remove.enabled is True

    dialog.specifications.selection = -1
    dialog._update_selection_buttons()
    assert edit.enabled is False
    assert remove.enabled is False

    dialog.specifications.selection = 0
    dialog._update_selection_buttons()
    dialog.summary_tabs.selection = 1
    dialog._update_selection_buttons()
    assert edit.enabled is False
    assert remove.enabled is False
