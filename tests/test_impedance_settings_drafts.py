"""Exercise applying unfinished impedance settings independently of capture review.

These tests use real constructors and bound event handlers with stateful wx
controls. They do not claim native event-ordering or visual-layout coverage.
"""

from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests import test_impedance_dialog as dialog_tests
from tests.test_impedance_netclass_dialog import (
    apply_layer,
    choose_class,
    choose_layer,
    class_snapshot,
    view_all_rows,
)

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.model import BoardSnapshot, Specification

constructor_api = dialog_tests.constructor_api


def _apply(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Press the real standard button without visiting or rendering preview rows."""
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)


def _outer_snapshot(api: SimpleNamespace) -> BoardSnapshot:
    """Keep valid inner copper planes but route this class on the outer faces only."""
    snapshot = class_snapshot(api)
    return replace(
        snapshot,
        traces=tuple(trace for trace in snapshot.traces if trace.layer != "In2.Cu"),
    )


def _old_specification(api: SimpleNamespace) -> Specification:
    """Retain a historically routed inner layer whose differential gap is unknown."""
    return api.model.Specification(
        "usb",
        "USB",
        "50",
        "single_ended",
        "usb",
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("In1.Cu",)),
            api.model.LayerSettings("In3.Cu", ("In2.Cu", "B.Cu")),
            api.model.LayerSettings("B.Cu", ("In3.Cu",)),
        ),
    )


def test_apply_ignores_obsolete_saved_layer_after_kind_change(
    constructor_api: SimpleNamespace,
) -> None:
    """A no-longer-routed In3.Cu cannot block OK after both real layers are reviewed."""
    api = constructor_api
    source = _old_specification(api)
    dialog = api.create_dialog(_outer_snapshot(api), source)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    for layer, spacing in (("F.Cu", "0.200001"), ("B.Cu", "0.300002")):
        choose_layer(api, dialog, layer)
        dialog.spacing.SetValue(spacing)
        # Deliberately approve the routed layers to isolate the reported stale
        # layer validation defect from the separate save/review coupling defect.
        view_all_rows(api, dialog)
        dialog.approve_layer.emit(api.wx.EVT_BUTTON)
        api.drain()
        api.paint(dialog.preview_pane)
    choose_layer(api, dialog, "B.Cu")
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    assert tuple(item.layer for item in dialog.specification.layer_settings) == (
        "F.Cu",
        "B.Cu",
    )
    assert tuple(item.spacing_nm for item in dialog.specification.layer_settings) == (
        200_001,
        300_002,
    )
    assert all(item.layer != "In3.Cu" for item in dialog.review_tracking.layers)
    assert len(source.layer_settings) == 3


def test_removed_unused_signal_layer_does_not_block_open_or_apply(
    constructor_api: SimpleNamespace,
) -> None:
    """Reducing copper count drops an unused saved signal entry, not active references."""
    api = constructor_api
    snapshot = _outer_snapshot(api)
    snapshot = replace(snapshot, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    source = api.model.Specification(
        "usb",
        "USB",
        "50",
        "single_ended",
        "usb",
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("In1.Cu",)),
            api.model.LayerSettings("In3.Cu", ("In2.Cu", "B.Cu")),
            api.model.LayerSettings("B.Cu", ("In2.Cu",)),
        ),
    )
    dialog = api.create_dialog(snapshot, source)
    assert dialog._layer_names == ["F.Cu", "B.Cu"]
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK
    assert tuple(item.layer for item in dialog.specification.layer_settings) == (
        "F.Cu",
        "B.Cu",
    )
    assert len(source.layer_settings) == 3


def test_review_skips_a_layer_already_explicitly_excluded(
    constructor_api: SimpleNamespace,
) -> None:
    """An unchecked layer is addressed without an extra empty review stop."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    choose_layer(api, dialog, "B.Cu")
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    for layer in ("F.Cu", "In2.Cu"):
        choose_layer(api, dialog, layer)
        apply_layer(api, dialog)
    assert dialog._active_layer == "In2.Cu"
    assert "Apply settings" in dialog.validation_message.GetValue()
    _apply(api, dialog)
    assert dialog.specification.excluded_layers == ("B.Cu",)


def test_apply_accepts_complete_unreviewed_settings_without_capture_renderer(
    constructor_api: SimpleNamespace,
) -> None:
    """Saving a valid specification never waits for unavailable production captures."""
    api = constructor_api
    dialog = api.create_dialog(dialog_tests._snapshot(api), render=None)
    choose_class(api, dialog, "RF")
    before = dialog.review_tracking
    assert not before.layers
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    assert dialog.specification.net_class == "RF"
    assert dialog.review_tracking == before
    assert not api.preview_calls


@pytest.mark.parametrize(
    "unfinished", ["target", "spacing", "references", "ground_gap"]
)
def test_apply_round_trips_unfinished_settings_without_approval(
    constructor_api: SimpleNamespace,
    unfinished: str,
) -> None:
    """Blank fields survive configuration persistence and reopening as unfinished work."""
    api = constructor_api
    snapshot = dialog_tests._snapshot(api)
    dialog = api.create_dialog(snapshot, render=None)
    choose_class(api, dialog, "RF")
    if unfinished in ("spacing", "ground_gap"):
        dialog.kind.SetSelection(3)
        dialog.kind.emit(api.wx.EVT_CHOICE)
        dialog.spacing.SetValue("0.200001")
        dialog.ground_gap.SetValue("0.100001")
    if unfinished == "references":
        for index in range(len(dialog.layers)):
            dialog.references.Check(index, False)
        dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    else:
        getattr(dialog, unfinished).SetValue("")
    before = dialog.review_tracking
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    spec = dialog.specification
    assert tuple(item.layer for item in spec.layer_settings) == ("F.Cu",)
    assert not spec.excluded_layers
    assert dialog.review_tracking == before
    if unfinished == "target":
        assert spec.target_ohms == ""
    elif unfinished == "references":
        assert spec.layer_settings[0].reference_layers == ()
    else:
        assert getattr(spec.layer_settings[0], f"{unfinished}_nm") is None
    restored = api.model.Config.from_dict(
        json.loads(json.dumps(api.model.Config(specifications=(spec,)).to_dict()))
    ).specifications[0]
    reopened = api.create_dialog(snapshot, restored, render=None)
    assert "needs configuration" in reopened.layer.items[0].lower()
    assert not reopened.review_tracking.layers
    if unfinished == "references":
        assert dialog_tests._checked_reference_layers(reopened) == ()
    else:
        assert getattr(reopened, unfinished).GetValue() == ""
    _apply(api, reopened)
    assert reopened.modal_result == api.wx.ID_OK
    assert reopened.specification == spec


@pytest.mark.parametrize("text", ["unfinished dimension", "-0.2"])
def test_apply_focuses_actual_invalid_layer_without_losing_other_edits(
    constructor_api: SimpleNamespace,
    text: str,
) -> None:
    """Malformed values block saving on their own layer, without manufacturing approval."""
    api = constructor_api
    dialog = api.create_dialog(_outer_snapshot(api), render=None)
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue(text)
    choose_layer(api, dialog, "B.Cu")
    dialog.spacing.SetValue("0.300002")
    before = dialog.review_tracking
    _apply(api, dialog)
    assert dialog.modal_result is None
    assert dialog.specification is None
    assert dialog._active_layer == "F.Cu"
    assert dialog.spacing.GetValue() == text
    assert getattr(dialog.spacing, "focused", False)
    assert dialog._layer_drafts["B.Cu"].spacing == "0.300002"
    assert dialog.validation_message.GetValue()
    assert dialog.review_tracking == before


def test_incomplete_routed_layers_are_saved_unless_explicitly_excluded(
    constructor_api: SimpleNamespace,
) -> None:
    """An untouched gap is unfinished work; only an explicit unchecked box excludes it."""
    api = constructor_api
    snapshot = class_snapshot(api)
    dialog = api.create_dialog(snapshot, render=None)
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.200001")
    choose_layer(api, dialog, "B.Cu")
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    spec = dialog.specification
    assert spec.excluded_layers == ("B.Cu",)
    assert tuple(item.layer for item in spec.layer_settings) == ("F.Cu", "In2.Cu")
    assert spec.layer_settings[1].spacing_nm is None
    reopened = api.create_dialog(snapshot, spec, render=None)
    choose_layer(api, reopened, "In2.Cu")
    assert reopened.include_layer.GetValue() is True
    assert reopened.spacing.GetValue() == ""
    choose_layer(api, reopened, "B.Cu")
    assert reopened.include_layer.GetValue() is False


def test_cancel_retains_obsolete_saved_layer_and_original_dimensions(
    constructor_api: SimpleNamespace,
) -> None:
    """Pruning inactive layer drafts or editing dimensions cannot mutate the source."""
    api = constructor_api
    source = _old_specification(api)
    before = api.model.Config(specifications=(source,)).to_dict()
    dialog = api.create_dialog(_outer_snapshot(api), source, render=None)
    dialog.target.SetValue("")
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.2")
    dialog.EndModal(api.wx.ID_CANCEL)
    assert dialog.specification is None
    assert api.model.Config(specifications=(source,)).to_dict() == before
    assert tuple(item.layer for item in source.layer_settings) == (
        "F.Cu",
        "In3.Cu",
        "B.Cu",
    )


def test_outer_dimensions_stay_independent_without_physical_stackup_evidence(
    constructor_api: SimpleNamespace,
) -> None:
    """Copper position alone must not silently synchronize distinct layer dimensions."""
    api = constructor_api
    snapshot = _outer_snapshot(api)
    dialog = api.create_dialog(snapshot, render=None)
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.200001")
    choose_layer(api, dialog, "B.Cu")
    dialog.spacing.SetValue("0.300002")
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    spec = dialog.specification
    assert tuple(item.spacing_nm for item in spec.layer_settings) == (200_001, 300_002)
    reopened = api.create_dialog(snapshot, spec, render=None)
    assert reopened.spacing.GetValue() == "0.200001"
    choose_layer(api, reopened, "B.Cu")
    assert reopened.spacing.GetValue() == "0.300002"


def test_reappearing_route_requires_configuration_and_fresh_review(
    constructor_api: SimpleNamespace,
) -> None:
    """Accepting stale-layer cleanup cannot later resurrect its obsolete dimensions."""
    api = constructor_api
    snapshot = _outer_snapshot(api)
    dialog = api.create_dialog(snapshot, _old_specification(api), render=None)
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK, dialog.validation_message.GetValue()
    saved = dialog.specification
    first = snapshot.traces[0]
    routed_again = replace(
        snapshot,
        traces=snapshot.traces + (replace(first, trace_id="returned", layer="In3.Cu"),),
    )
    reopened = api.create_dialog(routed_again, saved, render=None)
    assert "In3.Cu" in reopened._layer_names
    choose_layer(api, reopened, "In3.Cu")
    assert reopened._layer_drafts["In3.Cu"].confirmed is False
    assert not reopened.review_tracking.layers
    reopened.kind.SetSelection(1)
    reopened.kind.emit(api.wx.EVT_CHOICE)
    assert reopened.spacing.GetValue() == ""
    assert "needs configuration" in reopened.active_layer_status.GetValue().lower()


def test_apply_unchanged_settings_retains_existing_approval_without_restamping(
    constructor_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting an unchanged, previously reviewed layer preserves its existing evidence."""
    api = constructor_api
    snapshot = dialog_tests._snapshot(api)
    dialog = api.create_dialog(snapshot)
    choose_class(api, dialog, "RF")
    view_all_rows(api, dialog)
    apply_layer(api, dialog)
    assert len(dialog.review_tracking.layers) == 1
    _apply(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK
    reopened = api.dialog.SpecificationDialog(
        None,
        snapshot,
        dialog.specification,
        preview=api.preview,
        review_tracking=dialog.review_tracking,
    )
    api.finish_preparation(reopened)
    before = reopened.review_tracking

    def unexpected_timestamp() -> str:
        """Fail if Apply turns an unchanged setting into a new review event."""
        pytest.fail("Apply settings must not record a new view or approval timestamp")

    monkeypatch.setattr(api.dialog.tracking, "utc_now", unexpected_timestamp)
    _apply(api, reopened)
    assert reopened.modal_result == api.wx.ID_OK
    assert reopened.specification == dialog.specification
    assert reopened.review_tracking == before
