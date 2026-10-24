"""Save reuses this session's verified pixels and matching inputs.

These workflows use real dialog constructors, handlers, and CaptureSession.
Only native source/theme/render boundaries and wx controls are test doubles.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
import importlib
import inspect
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import apply_layer, view_all_rows

constructor_api = _constructor_api
draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.model import BoardSnapshot, Config, Section
    from impedance.service import CapturedImage


@pytest.fixture
def save_api(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[SimpleNamespace]:
    """Count real session work while source and native resources remain stateful."""
    api = draft_api
    package = api.dialog.__package__
    integration = importlib.import_module(f"{package}.integration")
    render = importlib.import_module(f"{package}.render")
    state = SimpleNamespace(
        snapshot=None,
        source_error=None,
        palette="initial-native-theme",
        variant=None,
        owners=[],
        source_reads=[],
        appearance_reads=[],
        provider_calls=[],
        renders=[],
        analyses=[],
        saved=[],
        events=[],
        fail_write=False,
        notice_dialog=None,
        notice_states=[],
    )

    def snapshot() -> BoardSnapshot:
        """Read the same live source state later used by render verification."""
        state.source_reads.append(state.snapshot)
        if state.source_error is not None:
            raise state.source_error
        assert state.snapshot is not None
        return state.snapshot

    def appearance(_pcbnew: object = None) -> str:
        """Expose one appearance revision to capture and dialog verification."""
        state.appearance_reads.append(state.palette)
        return state.palette

    def source_revision() -> tuple[BoardSnapshot, Optional[str]]:
        """Keep variant identity separate when two outputs have identical geometry."""
        return snapshot(), state.variant

    class Owner:
        """Retain a detached output board until the real session releases it."""

        def __init__(self, name: str) -> None:
            self.board = SimpleNamespace(variant=name)
            self.closed = False
            state.owners.append(self)

        def close(self) -> None:
            self.closed = True

    class Renderer:
        """Retain lifetime and write valid immutable PNG bytes for real loading."""

        def __init__(
            self, _board: object, _pcbnew: object, *, appearance: object = None
        ) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def render(self, section: Section, destination: Path) -> Path:
            assert not self.closed
            state.renders.append(section)
            destination.write_bytes(api.capture.data)
            return destination

    original_analyze = api.dialog.analyze

    def analyze(config: Config, board: BoardSnapshot) -> object:
        """Count matching without replacing its analysis or validation behavior."""
        state.analyses.append((config, board))
        return original_analyze(config, board)

    def save(config: Config) -> None:
        """Publish neither config nor closure after a failed persistence attempt."""
        state.events.append("write")
        if state.fail_write:
            raise ValueError("Database revision changed; retry")
        state.saved.append(config)

    def notice(message: str, *_args: object) -> None:
        """Retain the observable message and its ordering relative to dismissal."""
        state.events.append(("notice", message))
        api.errors.append(message)
        dialog = state.notice_dialog
        if dialog is not None:
            state.notice_states.append(
                (
                    dialog._closing,
                    dialog.modal_result,
                    dialog.preview_pane._closed,
                    dialog._width_refresh_timer.IsRunning(),
                    dialog._width_slow_timer.IsRunning(),
                )
            )

    monkeypatch.setattr(integration, "read_theme_context", appearance)
    monkeypatch.setattr(render, "SectionRenderer", Renderer)
    monkeypatch.setattr(api.dialog, "analyze", analyze)
    monkeypatch.setattr(api.wx, "MessageBox", notice)
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    controls = SimpleNamespace(
        _selected_output_variant=lambda: state.variant,
        _snapshot=snapshot,
        _source_revision=source_revision,
        snapshot_output_board=lambda _board: state.snapshot,
        parent=SimpleNamespace(
            pcbnew=SimpleNamespace(GetBoard=lambda: object()),
            fabrication=SimpleNamespace(copy_variant_board=Owner),
        ),
    )
    captures = integration.CaptureSession(controls, tmp_path)

    def preview(section: Section, *, refresh: bool = False) -> CapturedImage:
        """Keep image-provider requests distinct from native render invocations."""
        state.provider_calls.append((section, refresh))
        return captures.preview(section, refresh=refresh)

    def reset_counts() -> None:
        for name in (
            "source_reads",
            "appearance_reads",
            "provider_calls",
            "renders",
            "analyses",
            "events",
        ):
            getattr(state, name).clear()

    def main(config: Config) -> ImpedanceDialog:
        """Use the optional batch API once both production producer/consumer exist."""
        kwargs = {}
        if hasattr(captures, "verify_captures") and "verify_captures" in (
            inspect.signature(api.dialog.ImpedanceDialog).parameters
        ):
            kwargs["verify_captures"] = captures.verify_captures
        if (
            "prepare_capture"
            in inspect.signature(api.dialog.ImpedanceDialog).parameters
        ):
            kwargs["prepare_capture"] = captures.prepare
        dialog = api.dialog.ImpedanceDialog(
            None,
            config,
            captures.snapshot(),
            preview,
            captures.snapshot,
            verify_snapshot=captures.verify,
            appearance_context=lambda: (appearance(), state.variant),
            save_config=save,
            **kwargs,
        )
        original_end = dialog.EndModal

        def end(result: int) -> None:
            state.events.append("close")
            original_end(result)

        dialog.EndModal = end
        return dialog

    yield SimpleNamespace(
        api=api,
        state=state,
        captures=captures,
        preview=preview,
        main=main,
        reset_counts=reset_counts,
    )
    captures.close()


def _config(env: SimpleNamespace, count: int = 2) -> Config:
    """Give each routed signal its own workbook image with valid layer intent."""
    model = env.api.model
    env.state.snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                f"track-{index}",
                "F.Cu",
                f"RF{index}",
                150_000,
                ((0, index * 1_000_000), (2_000_000, index * 1_000_000)),
            )
            for index in range(count)
        ),
        context_digest="initial-board",
        net_classes=("RF",),
        net_class_memberships=tuple((f"RF{index}", ("RF",)) for index in range(count)),
        net_class_context_digest="save-evidence-fixture",
    )
    return model.Config(
        enabled=True,
        specifications=(
            model.Specification(
                "rf",
                "RF",
                "50",
                "single_ended",
                "RF",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        ),
    )


def _approve_child(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, count: int = 2
) -> ImpedanceDialog:
    """Review actual pixels in a real child, Apply, and return to the parent."""
    api = env.api
    parent = env.main(_config(env, count))
    parent.on_screen = True

    def show(child: SpecificationDialog) -> int:
        child.on_screen = True
        child.bindings[api.wx.EVT_SHOW, -1](
            SimpleNamespace(
                GetEventObject=lambda: child, IsShown=lambda: True, Skip=lambda: None
            )
        )
        api.drain()
        api.paint(child.preview_pane)
        api.finish_preparation(child)
        view_all_rows(api, child)
        apply_layer(api, child)
        assert child._layer_drafts["F.Cu"].confirmed
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", show, raising=False
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    assert parent.session.approved
    assert len(parent.session.config.review_tracking.images) == count
    return parent


def _save(env: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Dispatch Save as a user click without running unrelated queued work."""
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)


def _assert_reused(env: SimpleNamespace) -> None:
    """Require constant cost and no image work for unchanged Save."""
    actual = {
        name: len(getattr(env.state, name))
        for name in (
            "provider_calls",
            "renders",
            "source_reads",
            "appearance_reads",
            "analyses",
        )
    }
    assert actual == {
        "provider_calls": 0,
        "renders": 0,
        "source_reads": 1,
        "appearance_reads": 1,
        "analyses": 0,
    }


@pytest.mark.parametrize("count", [2, 6])
@pytest.mark.parametrize("variant", [None, "A"])
def test_fresh_child_approval_apply_save_reuses_evidence(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    variant: Optional[str],
) -> None:
    """Save consumes approval established in the child, without restoring it again."""
    env = save_api
    env.state.variant = variant
    dialog = _approve_child(env, monkeypatch, count)
    history = dialog.session.config.review_tracking
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result == env.api.wx.ID_OK
    assert env.state.saved[-1].reviewed_digest
    assert env.state.saved[-1].review_tracking == history
    _assert_reused(env)


@pytest.mark.parametrize("evict", [False, True])
def test_reopened_verified_approval_save_reuses_hashes_after_png_eviction(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, evict: bool
) -> None:
    """Pixel-cache eviction is not loss of this session's revision-bound proof."""
    env = save_api
    approved = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    env.reset_counts()
    dialog = env.main(approved)
    assert dialog.session.approved
    assert env.state.renders == []
    dialog.on_screen = True
    dialog.bindings[env.api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    env.api.drain()
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert len(env.state.renders) == 2
    verified = dialog.session.config
    assert replace(verified, review_tracking=approved.review_tracking) == approved
    assert verified.review_tracking.layers == approved.review_tracking.layers
    if evict:
        env.captures.images.clear()
        env.captures.image_bytes = 0
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result == env.api.wx.ID_OK
    assert env.state.saved[-1] == verified
    _assert_reused(env)


def test_failed_write_retry_reuses_verified_hashes_and_retains_edits(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistence failure does not destroy proof, including after PNG eviction."""
    env = save_api
    dialog = _approve_child(env, monkeypatch)
    approved = dialog.session.config
    original = dialog.config
    env.state.fail_write = True
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result is None
    assert not dialog._closing
    assert not dialog.preview_pane._closed
    assert env.state.saved == []
    assert dialog.config is original
    assert dialog.session.config == approved
    assert "Database revision changed" in dialog.status.GetValue()
    _assert_reused(env)

    env.captures.images.clear()
    env.captures.image_bytes = 0
    env.state.fail_write = False
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result == env.api.wx.ID_OK
    assert env.state.saved == [approved]
    assert env.state.events == ["write", "close"]
    _assert_reused(env)


@pytest.mark.parametrize("changed", ["board", "appearance", "variant"])
def test_changed_revision_saves_draft_and_notifies_before_dismissal(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """Historical consent survives as history, with downgrade visible to the user."""
    env = save_api
    if changed == "variant":
        env.state.variant = "A"
    dialog = _approve_child(env, monkeypatch)
    history = dialog.session.config.review_tracking
    specifications = dialog.session.config.specifications
    if changed == "board":
        env.state.snapshot = replace(env.state.snapshot, context_digest="edited-board")
    elif changed == "appearance":
        env.state.palette = "changed-native-theme"
    else:
        env.state.variant = "B"
    cancellation = Event()
    dialog._calculation_cancel = cancellation
    dialog._width_refresh_timer.StartOnce(1)
    dialog._width_slow_timer.StartOnce(1)
    env.state.notice_dialog = dialog
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result == env.api.wx.ID_OK
    assert env.state.saved[-1].reviewed_digest == ""
    assert env.state.saved[-1].included_section_ids == ()
    assert env.state.saved[-1].review_tracking == history
    assert env.state.saved[-1].specifications == specifications
    assert env.state.provider_calls == []
    assert env.state.renders == []
    assert env.state.events[0] == "write"
    assert env.state.events[-1] == "close"
    notices = [
        event[1].lower() for event in env.state.events if isinstance(event, tuple)
    ]
    assert len(notices) == 1
    assert "draft" in notices[0]
    assert "changed" in notices[0] or "review" in notices[0]
    assert env.state.notice_states == [(True, None, True, False, False)]
    assert cancellation.is_set()


def test_unreadable_source_keeps_dialog_edits_and_persisted_configuration(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed freshness check cannot write or discard the private working copy."""
    env = save_api
    dialog = _approve_child(env, monkeypatch)
    original = dialog.config
    edited = replace(dialog.session.config.specifications[0], label="Unsaved RF name")
    dialog.session.config = replace(dialog.session.config, specifications=(edited,))
    history = dialog.session.config.review_tracking
    env.state.source_error = ValueError("The PCB identity is unavailable")
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result is None
    assert not dialog._closing
    assert env.state.saved == []
    assert dialog.config is original
    assert dialog.session.config.specifications == (edited,)
    assert dialog.session.config.review_tracking == history
    assert "unavailable" in dialog.status.GetValue().lower()
    assert "close" not in env.state.events
    assert env.state.provider_calls == []


def test_failed_draft_write_notifies_downgrade_only_when_retry_succeeds(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the pending downgrade explanation across a failed database write."""
    env = save_api
    dialog = _approve_child(env, monkeypatch)
    history = dialog.session.config.review_tracking
    original = dialog.config
    env.state.snapshot = replace(env.state.snapshot, context_digest="changed-board")
    env.state.fail_write = True
    env.reset_counts()
    _save(env, dialog)
    assert dialog.modal_result is None
    assert not dialog._closing
    assert dialog.config is original
    assert env.state.saved == []
    assert env.state.events == ["write"]
    assert dialog.session.config.review_tracking == history
    assert "Database revision changed" in dialog.status.GetValue()

    env.state.fail_write = False
    env.reset_counts()
    _save(env, dialog)
    assert env.state.saved[-1].reviewed_digest == ""
    assert env.state.saved[-1].review_tracking == history
    assert dialog.modal_result == env.api.wx.ID_OK
    notices = [
        event[1].lower() for event in env.state.events if isinstance(event, tuple)
    ]
    assert len(notices) == 1
    assert "draft" in notices[0]
    assert env.state.events[0] == "write"
    assert env.state.events[-1] == "close"
    assert env.state.provider_calls == []
    assert env.state.renders == []


@pytest.mark.parametrize(
    "changed",
    [
        "snapshot",
        "specification",
        "stackup",
        "nominal_result",
        "algorithm_policy",
        "capture_policy",
        "stub_policy",
    ],
)
def test_save_result_reanalyzes_when_any_analysis_input_changes(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """An approved flag cannot stand in for a matching-input cache key."""
    env = save_api
    session = _approve_child(env, monkeypatch).session
    config = session.config
    snapshot = session.snapshot
    stackups = importlib.import_module(f"{env.api.dialog.__package__}.stackup_model")
    matching = importlib.import_module(f"{env.api.dialog.__package__}.matching")
    if changed == "snapshot":
        snapshot = replace(snapshot, context_digest="changed-source")
    elif changed == "specification":
        session.config = replace(
            config,
            specifications=(replace(config.specifications[0], target_ohms="90"),),
        )
    elif changed == "stackup":
        session.config = replace(
            config, stackup=stackups.Stackup("new", "New stackup", 2, "1.6", "1", "")
        )
    elif changed == "nominal_result":
        session.config = replace(
            config,
            width_results=(
                stackups.WidthResult(
                    "rf",
                    "F.Cu",
                    "a" * 64,
                    "success",
                    200_000,
                    calculated_at_utc="2026-10-20T12:00:00.000000Z",
                    model="microstrip",
                    calculation_digest="b" * 64,
                ),
            ),
        )
    else:
        policy = {
            "algorithm_policy": "ALGORITHM_VERSION",
            "capture_policy": "CAPTURE_POLICY_REVISION",
            "stub_policy": "COVERED_STUB_POLICY_REVISION",
        }[changed]
        monkeypatch.setattr(matching, policy, getattr(matching, policy) + 1)
    env.reset_counts()
    candidate = session.save_result(snapshot)
    assert len(env.state.analyses) == 1
    assert candidate.reviewed_digest == ""
    assert candidate.review_tracking == config.review_tracking
    assert env.state.provider_calls == []


def test_uninitialized_analysis_recomputes_before_preserving_approval(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded digest equality requires actual analysis, not an optimistic cache hit."""
    env = save_api
    approved = _approve_child(env, monkeypatch).session.config
    session = env.api.dialog.ReviewSession(approved, env.state.snapshot)
    session.analysis = None
    env.reset_counts()
    assert session.save_result() == approved
    assert len(env.state.analyses) == 1


def test_reused_analysis_still_validates_selected_sections(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached analysis must not authorize a nonexistent included section."""
    env = save_api
    session = _approve_child(env, monkeypatch).session
    session.config = replace(session.config, included_section_ids=("missing-row",))
    env.reset_counts()
    assert session.save_result().reviewed_digest == ""
    assert env.state.provider_calls == []
    assert env.state.analyses == []


def test_old_workbook_approval_without_image_history_still_saves(
    save_api: SimpleNamespace,
) -> None:
    """Compatibility does not invent capture, viewing, or approval records."""
    env = save_api
    config = _config(env)
    session = env.api.dialog.ReviewSession(config, env.state.snapshot)
    session.refresh()
    session.approve()
    old = session.config
    assert not old.review_tracking.images
    dialog = env.main(old)
    assert dialog.session.approved
    env.reset_counts()
    _save(env, dialog)
    assert env.state.saved == [old]
    assert dialog.modal_result == env.api.wx.ID_OK
    _assert_reused(env)
