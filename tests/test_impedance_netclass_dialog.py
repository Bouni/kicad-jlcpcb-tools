"""Exercise class-based configuration through real dialog constructors/events.

The fake wx runtime explicitly models queued capture callbacks and visible paints;
these regressions do not claim native platform rendering coverage.
"""

from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests import test_impedance_dialog as dialog_tests
from tests.test_impedance_dialog import _checked_reference_layers, _snapshot

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.model import BoardSnapshot

constructor_api = dialog_tests.constructor_api


def class_snapshot(api: SimpleNamespace) -> BoardSnapshot:
    """Expose case-distinct classes, neckdowns, and top/inner/bottom routing."""
    snapshot = _snapshot(api)
    first = snapshot.traces[0]
    return replace(
        snapshot,
        layers=("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "B.Cu"),
        traces=snapshot.traces
        + (
            replace(first, trace_id="neckdown", width_nm=100_000),
            replace(first, trace_id="inner", layer="In2.Cu", width_nm=175_001),
            replace(first, trace_id="bottom", layer="B.Cu", width_nm=200_000),
            replace(snapshot.traces[1], trace_id="neckdown-n", width_nm=100_000),
            replace(
                snapshot.traces[1], trace_id="inner-n", layer="In2.Cu", width_nm=175_001
            ),
            replace(
                snapshot.traces[1], trace_id="bottom-n", layer="B.Cu", width_nm=200_000
            ),
        ),
        net_classes=("Default", "USB", "usb", "Fast"),
        net_class_memberships=(
            ("CLK_P", ("usb", "Fast")),
            ("CLK_N", ("usb",)),
            ("GND", ("Default",)),
        ),
        net_class_context_digest="class-context",
    )


def choose_class(
    api: SimpleNamespace, dialog: SpecificationDialog, name: str = "usb"
) -> None:
    """Use exact indices just as a user explicitly chooses an item in the list."""
    dialog.net_class.SetSelection(dialog._net_class_names.index(name) + 1)
    dialog.net_class.emit(api.wx.EVT_CHOICE)
    api.drain()
    api.paint(dialog.preview_pane)


def choose_layer(api: SimpleNamespace, dialog: SpecificationDialog, name: str) -> None:
    """Dispatch the real layer-navigation handler after native selection changes."""
    dialog.layer.SetSelection(dialog._layer_names.index(name))
    dialog.layer.emit(api.wx.EVT_CHOICE)
    api.drain()
    api.paint(dialog.preview_pane)


def apply_layer(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Explicitly confirm the current layer using its bound button handler."""
    view_all_rows(api, dialog)
    dialog.approve_layer.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(dialog.preview_pane)


def save(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Accept through the dialog's actual ID_OK event handler."""
    view_all_rows(api, dialog)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    api.drain()
    api.paint(dialog.preview_pane)


def view_all_rows(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Visit every exact capture using the row selector's real bound native event."""
    api.drain()
    for index in range(len(dialog.preview_pane.rows.items)):
        dialog.preview_pane.rows.SetSelection(index)
        dialog.preview_pane.rows.emit(api.wx.EVT_CHOICE)
        api.drain()
        api.paint(dialog.preview_pane)


def test_class_discovery_includes_actual_widths_and_requires_every_routed_layer(
    constructor_api: SimpleNamespace,
) -> None:
    """Discover all physical widths and block save until each routed layer is handled."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    assert dialog._layer_names == ["F.Cu", "In2.Cu", "B.Cu"]
    assert "0.1, 0.15 mm" in dialog.layer_hint.GetValue()
    assert "0.175001 mm" in dialog.layer_hint.GetValue()
    assert "0.4" not in dialog.layer_hint.GetValue()
    assert not hasattr(dialog, "width")
    assert _checked_reference_layers(dialog) == ("In1.Cu",)
    save(api, dialog)
    assert dialog.modal_result is None
    assert api.errors == []
    assert dialog._active_layer == "In2.Cu"
    choose_layer(api, dialog, "In2.Cu")
    assert _checked_reference_layers(dialog) == ("In1.Cu", "In3.Cu")
    apply_layer(api, dialog)
    choose_layer(api, dialog, "B.Cu")
    save(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.specification.net_class == "usb"
    assert tuple(item.layer for item in dialog.specification.layer_settings) == (
        "F.Cu",
        "In2.Cu",
        "B.Cu",
    )


@pytest.mark.parametrize("kind_index", [0, 1, 2, 3])
def test_per_layer_references_and_dimensions_survive_switch_save_and_reopen(
    constructor_api: SimpleNamespace,
    kind_index: int,
) -> None:
    """Preserve individual plane overrides, relevant dimensions, and exclusions."""
    api = constructor_api
    snapshot = class_snapshot(api)
    dialog = api.create_dialog(snapshot)
    choose_class(api, dialog)
    dialog.kind.SetSelection(kind_index)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    values = (("F.Cu", "0.200001", "0.1"), ("In2.Cu", "0.3", "0.15"))
    for layer, pair, ground in values:
        choose_layer(api, dialog, layer)
        dialog.spacing.SetValue(pair)
        dialog.ground_gap.SetValue(ground)
        apply_layer(api, dialog)
    choose_layer(api, dialog, "In2.Cu")
    dialog.references.Check(0, True)
    dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    apply_layer(api, dialog)
    choose_layer(api, dialog, "B.Cu")
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    save(api, dialog)
    assert api.errors == []
    assert dialog.modal_result == api.wx.ID_OK
    spec = dialog.specification
    assert spec.excluded_layers == ("B.Cu",)
    assert spec.layer_settings[1].reference_layers == ("F.Cu", "In1.Cu", "In3.Cu")
    assert spec.target_ohms == ("90" if kind_index in (1, 3) else "50")
    for setting, (_, pair, ground) in zip(spec.layer_settings, values):
        assert setting.spacing_nm == (
            api.model.parse_length(pair) if kind_index in (1, 3) else None
        )
        assert setting.ground_gap_nm == (
            api.model.parse_length(ground) if kind_index in (2, 3) else None
        )
    restored = api.model.Config.from_dict(
        json.loads(json.dumps(api.model.Config(specifications=(spec,)).to_dict()))
    ).specifications[0]
    reopened = api.create_dialog(snapshot, restored)
    assert reopened.net_class.GetStringSelection() == "usb"
    assert reopened.layer.items == [
        "F.Cu — needs review",
        "In2.Cu — needs review",
        "B.Cu — excluded",
    ]
    choose_layer(api, reopened, "In2.Cu")
    assert _checked_reference_layers(reopened) == ("F.Cu", "In1.Cu", "In3.Cu")
    choose_layer(api, reopened, "B.Cu")
    assert reopened.include_layer.GetValue() is False
    assert reopened.references.enabled is False
    for layer in ("F.Cu", "In2.Cu"):
        choose_layer(api, reopened, layer)
        apply_layer(api, reopened)
    save(api, reopened)
    assert reopened.specification == spec


def test_invalid_layer_text_is_retained_without_cross_layer_contamination(
    constructor_api: SimpleNamespace,
) -> None:
    """Let navigation retain unfinished input without approving or leaking it."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("unfinished dimension")
    choose_layer(api, dialog, "In2.Cu")
    assert dialog.spacing.GetValue() == ""
    dialog.spacing.SetValue("0.2")
    apply_layer(api, dialog)
    choose_layer(api, dialog, "F.Cu")
    assert dialog.spacing.GetValue() == "unfinished dimension"
    apply_layer(api, dialog)
    assert api.errors == []
    assert dialog.validation_message.GetValue()
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    assert dialog._layer_drafts["In2.Cu"].confirmed is True


def test_new_routed_layer_is_unreviewed_when_saved_class_is_reopened(
    constructor_api: SimpleNamespace,
) -> None:
    """Require explicit settings for copper discovered since the saved specification."""
    api = constructor_api
    snapshot = class_snapshot(api)
    old = api.model.Specification(
        "usb",
        "USB",
        "50",
        "single_ended",
        "usb",
        layer_settings=(api.model.LayerSettings("F.Cu", ("In1.Cu",)),),
    )
    dialog = api.create_dialog(snapshot, old)
    assert "In2.Cu" in dialog._layer_names
    save(api, dialog)
    assert dialog.specification is None
    assert api.errors == []
    assert dialog._active_layer == "In2.Cu"


def test_missing_class_never_selects_case_insensitive_default_or_other_class(
    constructor_api: SimpleNamespace,
) -> None:
    """Keep a removed name visibly invalid instead of silently falling back."""
    api = constructor_api
    snapshot = class_snapshot(api)
    old = api.model.Specification(
        "old",
        "Old",
        "50",
        "single_ended",
        "Removed",
        layer_settings=(api.model.LayerSettings("F.Cu", ("In1.Cu",)),),
    )
    dialog = api.create_dialog(snapshot, old)
    assert dialog.net_class.GetStringSelection() == "Removed"
    assert "missing" in dialog.class_hint.GetValue()
    save(api, dialog)
    assert dialog.specification is None
    assert "unavailable" in dialog.validation_message.GetValue()


@pytest.mark.parametrize("stale_part", ["reference", "included", "excluded"])
def test_changed_stack_is_rejected_actionably_without_discarding_saved_intent(
    constructor_api: SimpleNamespace,
    stale_part: str,
) -> None:
    """Keep unavailable saved layer data intact and report a repairable stack mismatch."""
    api = constructor_api
    snapshot = class_snapshot(api)
    spec = api.model.Specification(
        "usb",
        "USB",
        "50",
        "single_ended",
        "usb",
        layer_settings=(api.model.LayerSettings("F.Cu", ("In1.Cu",)),),
    )
    if stale_part == "reference":
        spec = replace(
            spec, layer_settings=(api.model.LayerSettings("F.Cu", ("Missing.Cu",)),)
        )
    elif stale_part == "included":
        spec = replace(
            spec,
            layer_settings=spec.layer_settings
            + (api.model.LayerSettings("Missing.Cu", ("F.Cu",)),),
        )
    elif stale_part == "excluded":
        spec = replace(spec, excluded_layers=("Missing.Cu",))
    before = spec
    with pytest.raises(
        ValueError, match="enabled board stack.*replace this specification"
    ):
        api.create_dialog(snapshot, spec)
    assert spec == before


@pytest.mark.parametrize("field", ["spacing", "ground_gap", "references"])
def test_editing_a_confirmed_layer_immediately_marks_it_as_needing_review(
    constructor_api: SimpleNamespace,
    field: str,
) -> None:
    """The visible configured status must never hide unapplied layer edits."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.2")
    dialog.ground_gap.SetValue("0.1")
    apply_layer(api, dialog)
    choose_layer(api, dialog, "F.Cu")
    assert "F.Cu: 0.1, 0.15 mm (approved)" in dialog.layer_hint.GetValue()
    if field == "references":
        dialog.references.Check(3, True)
        dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    else:
        getattr(dialog, field).SetValue("0.3")
    assert "F.Cu: 0.1, 0.15 mm (needs review)" in dialog.layer_hint.GetValue()


def test_layer_unit_changes_preserve_exact_values_and_existing_confirmation(
    constructor_api: SimpleNamespace,
) -> None:
    """Display-only unit changes and navigation must not require renewed layer approval."""
    api = constructor_api
    snapshot = class_snapshot(api)
    spec = api.model.Specification(
        "usb",
        "USB",
        "90",
        "differential_coplanar",
        "usb",
        layer_settings=(
            api.model.LayerSettings("F.Cu", ("In1.Cu",), 200_001, 100_001),
            api.model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu"), 225_003, 120_005),
        ),
        excluded_layers=("B.Cu",),
    )
    dialog = api.create_dialog(snapshot, spec)
    for layer in ("F.Cu", "In2.Cu"):
        choose_layer(api, dialog, layer)
        apply_layer(api, dialog)
    choose_layer(api, dialog, "F.Cu")
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    assert dialog._read_dimension(dialog.spacing.GetValue(), "mil") == 200_001
    choose_layer(api, dialog, "In2.Cu")
    assert dialog.units.GetStringSelection() == "mm"
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    choose_layer(api, dialog, "F.Cu")
    assert dialog.units.GetStringSelection() == "mil"
    choose_layer(api, dialog, "B.Cu")
    save(api, dialog)
    assert api.errors == []
    assert dialog.specification == spec


@pytest.mark.parametrize(
    "class_name", ["Default", "USB", "usb", "USB, USB3", "Choose a net class…"]
)
def test_explicit_class_choice_and_reopen_use_exact_values_not_display_labels(
    constructor_api: SimpleNamespace,
    class_name: str,
) -> None:
    """Case, punctuation, and a placeholder-like real name remain valid explicit choices."""
    api = constructor_api
    snapshot = class_snapshot(api)
    names = tuple(dict.fromkeys(snapshot.net_classes + (class_name,)))
    snapshot = replace(
        snapshot,
        traces=(snapshot.traces[0],),
        net_classes=names,
        net_class_memberships=(("CLK_P", (class_name,)),),
    )
    dialog = api.create_dialog(snapshot)
    assert dialog.net_class.GetSelection() == 0
    assert dialog._active_class == ""
    dialog.net_class.SetSelection(names.index(class_name) + 1)
    dialog.net_class.emit(api.wx.EVT_CHOICE)
    save(api, dialog)
    assert api.errors == []
    assert dialog.specification.net_class == class_name
    reopened = api.create_dialog(snapshot, dialog.specification)
    assert reopened._active_class == class_name
    assert reopened.net_class.GetSelection() == names.index(class_name) + 1


def mixed_default_snapshot(api: SimpleNamespace) -> BoardSnapshot:
    """Include both the inherited RF pair and unrelated routed context in Default."""
    board = class_snapshot(api)
    context = replace(board.traces[0], trace_id="context", net="CONTEXT_1")
    return replace(
        board,
        traces=board.traces + (context,),
        net_class_memberships=(
            ("CLK_P", ("Default", "usb")),
            ("CLK_N", ("Default", "usb")),
            ("CONTEXT_1", ("Default",)),
            ("GND", ("Default",)),
        ),
    )


def assert_field_marker(
    dialog: SpecificationDialog, field: str, marker: str = ""
) -> None:
    """Require both readable text and native colour state, not colour alone."""
    label = dialog._field_labels[field]
    assert ("Required" in label.GetLabel()) == (marker == "Required")
    assert ("Invalid" in label.GetLabel()) == (marker == "Invalid")
    if marker:
        assert label.GetForegroundColour() == (205, 40, 40)
    else:
        assert label.GetForegroundColour() == "native-label-colour"


def test_default_single_ended_previews_actual_widths_without_manual_input(
    constructor_api: SimpleNamespace,
) -> None:
    """Default is a native class, never an implicit switch to manual widths."""
    api = constructor_api
    dialog = api.create_dialog(mixed_default_snapshot(api))
    choose_class(api, dialog, "Default")
    assert dialog.preview_pane._sections
    assert "All actual widths are matched" in dialog.class_hint.GetValue()
    assert not hasattr(dialog, "width")
    assert not hasattr(dialog, "nets")
    assert not hasattr(dialog, "selection_mode")


def test_default_mixed_differential_error_precedes_missing_gap_and_recovers(
    constructor_api: SimpleNamespace,
) -> None:
    """Retain the real blocker until the user explicitly selects a complete pair."""
    api = constructor_api
    dialog = api.create_dialog(mixed_default_snapshot(api))
    choose_class(api, dialog, "Default")
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == ""
    assert "CONTEXT_1" in dialog.preview_pane.status.GetValue()
    assert "Single Ended" in dialog.preview_pane.status.GetValue()
    assert "dedicated net class" in dialog.preview_pane.status.GetValue()
    with pytest.raises(ValueError, match="CONTEXT_1"):
        dialog.preview_pane.ensure_reviewed()
    apply_layer(api, dialog)
    assert "CONTEXT_1" in dialog.validation_message.GetValue()
    assert dialog.kind.focused is True
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    assert dialog.preview_pane._sections == ()
    assert_field_marker(dialog, "kind", "Invalid")
    assert_field_marker(dialog, "spacing", "Required")
    assert_field_marker(dialog, "ground_gap", "Required")
    choose_class(api, dialog, "usb")
    assert dialog.preview_pane._sections
    assert dialog.kind.GetSelection() == 3
    assert dialog.target.GetValue() == "90"


def test_required_markers_follow_valid_invalid_and_irrelevant_fields(
    constructor_api: SimpleNamespace,
) -> None:
    """Typing and kind changes update markers immediately for the relevant fields."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    for name in ("spacing", "ground_gap", "target"):
        control = getattr(dialog, name)
        control.SetValue("")
        assert_field_marker(dialog, name, "Required")
        control.SetValue("NaN")
        assert_field_marker(dialog, name, "Invalid")
        control.SetValue("90" if name == "target" else "0.2")
        assert_field_marker(dialog, name)
    for index in range(len(dialog.layers)):
        dialog.references.Check(index, False)
    dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    assert_field_marker(dialog, "references", "Required")
    dialog.adjacent_button.emit(api.wx.EVT_BUTTON)
    assert_field_marker(dialog, "references")
    dialog.spacing.SetValue("bad draft")
    dialog.ground_gap.SetValue("")
    dialog.kind.SetSelection(0)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert_field_marker(dialog, "spacing")
    assert_field_marker(dialog, "ground_gap")
    assert dialog.spacing.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.ground_gap.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog._layer_drafts[dialog._active_layer].spacing == "bad draft"
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert_field_marker(dialog, "spacing", "Invalid")
    assert_field_marker(dialog, "ground_gap", "Required")
    assert dialog.spacing.GetValue() == "bad draft"


def test_markers_restore_per_layer_drafts_and_ignore_excluded_layers(
    constructor_api: SimpleNamespace,
) -> None:
    """Excluded values are neither required nor discarded during layer navigation."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("invalid F draft")
    choose_layer(api, dialog, "In2.Cu")
    assert_field_marker(dialog, "spacing", "Required")
    dialog.spacing.SetValue("0.2")
    dialog.ground_gap.SetValue("0.2")
    assert_field_marker(dialog, "spacing")
    choose_layer(api, dialog, "F.Cu")
    assert_field_marker(dialog, "spacing", "Invalid")
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    assert_field_marker(dialog, "spacing")
    assert_field_marker(dialog, "ground_gap")
    dialog.include_layer.SetValue(True)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    assert_field_marker(dialog, "spacing", "Invalid")
    assert dialog.spacing.GetValue() == "invalid F draft"


def test_marker_updates_do_not_rewrap_or_relayout_unchanged_native_labels(
    constructor_api: SimpleNamespace,
) -> None:
    """Native Wrap inserts line breaks; repeated validation must not grow layout."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    label = dialog._field_labels["spacing"]
    calls = []

    def wrap(width: int) -> None:
        calls.append(width)
        label.SetLabel(label.GetLabel().replace(" — ", "\n— "))

    label.Wrap = wrap
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    before = len(calls)
    for _ in range(4):
        dialog._update_field_indicators()
    assert len(calls) == before
    assert label.GetLabel().count("Required") == 1


def test_unpairable_layer_can_be_explicitly_excluded_without_filling_its_gaps(
    constructor_api: SimpleNamespace,
) -> None:
    """Layer-local pair errors do not prevent a deliberate workbook exclusion."""
    api = constructor_api
    dialog = api.create_dialog(mixed_default_snapshot(api))
    choose_class(api, dialog, "Default")
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert "CONTEXT_1" in dialog.preview_pane.status.GetValue()
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    apply_layer(api, dialog)
    assert dialog._layer_drafts["F.Cu"].confirmed is True
    assert dialog._layer_status("F.Cu") == "excluded"
    choose_layer(api, dialog, "F.Cu")
    assert_field_marker(dialog, "kind")
    assert_field_marker(dialog, "spacing")
    assert_field_marker(dialog, "ground_gap")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "junk", "0.0000001"])
def test_dimension_marker_rejects_nonpositive_nonfinite_and_subnanometre_values(
    constructor_api: SimpleNamespace, value: str
) -> None:
    """Invalid means physically unrepresentable, not merely nonempty input."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue(value)
    dialog.ground_gap.SetValue(value)
    assert_field_marker(dialog, "spacing", "Invalid")
    assert_field_marker(dialog, "ground_gap", "Invalid")


def test_layer_approval_is_visible_in_signal_layer_choices(
    constructor_api: SimpleNamespace,
) -> None:
    """Put review state beside each layer and name the approval action plainly."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    assert dialog.layer.items == [
        "F.Cu — needs review",
        "In2.Cu — needs review",
        "B.Cu — needs review",
    ]
    assert dialog.approve_layer.GetValue() == "Approve layer"
    assert dialog.include_layer.GetValue() is True
    assert dialog.include_layer.GetLabel() == "Include this layer in the workbook"
    assert dialog.include_layer.parent is dialog.form


def test_ok_progresses_to_next_unreviewed_layer_without_popup(
    constructor_api: SimpleNamespace,
) -> None:
    """OK is a sequential review action until the final layer has been addressed."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    save(api, dialog)
    assert api.errors == []
    assert dialog._active_layer == "In2.Cu"
    assert dialog.modal_result is None


def test_invalid_layer_input_is_inline_and_focuses_spacing(
    constructor_api: SimpleNamespace,
) -> None:
    """Incomplete gap entry must retain text and focus, never open a modal alert."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("unfinished")
    save(api, dialog)
    assert api.errors == []
    assert dialog.spacing.GetValue() == "unfinished"
    assert dialog.spacing.focused is True
    assert dialog.validation_message.GetValue()


@pytest.mark.parametrize("target", ["", "nonsense", "-50", "NaN"])
def test_approve_layer_rejects_invalid_target_without_hiding_geometry(
    constructor_api: SimpleNamespace,
    target: str,
) -> None:
    """Geometry remains visible while invalid global intent prevents approval."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    view_all_rows(api, dialog)
    dialog.target.SetValue(target)
    apply_layer(api, dialog)
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    assert dialog.target.focused is True
    assert dialog.validation_message.GetValue()
    assert dialog.preview_pane.canvas._image is not None
    assert api.errors == []


@pytest.mark.parametrize("action", ["approve", "save"])
def test_native_snapshot_failure_clears_preview_and_is_inline_for_every_acceptance(
    constructor_api: SimpleNamespace,
    action: str,
) -> None:
    """Native failures outside ValueError must not escape callbacks or retain stale evidence."""
    api = constructor_api
    snapshot = class_snapshot(api)
    state = {"fail": False}
    seen = []

    def verify(expected: BoardSnapshot) -> None:
        """Bind freshness to this exact window snapshot, then simulate an API failure."""
        seen.append(expected)
        if state["fail"]:
            raise RuntimeError("The live board cannot be read")

    dialog = api.create_dialog(snapshot)
    dialog.verify_snapshot = verify
    choose_class(api, dialog)
    apply_layer(api, dialog)
    assert dialog._layer_drafts["F.Cu"].confirmed is True
    assert dialog.preview_pane.canvas._image is not None
    state["fail"] = True
    if action == "approve":
        dialog.approve_layer.emit(api.wx.EVT_BUTTON)
    else:
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    assert dialog.preview_pane.canvas._image is None
    assert dialog.specification is None
    assert "live board cannot be read" in dialog.validation_message.GetValue()
    assert api.errors == []
    assert seen and all(expected is snapshot for expected in seen)


def test_exact_pair_captures_precede_missing_fields_and_survive_full_ok_workflow(
    constructor_api: SimpleNamespace,
) -> None:
    """Approve each actual pair/width image before saving identical production rows."""
    api = constructor_api
    snapshot = class_snapshot(api)
    dialog = api.create_dialog(snapshot)
    choose_class(api, dialog)
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.target.SetValue("")
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.spacing.GetValue() == ""
    assert len(dialog.preview_pane.rows.items) == 2
    assert dialog.preview_pane.canvas._image is not None
    assert all(len(section.net_names) == 2 for section in dialog.preview_pane._sections)
    dialog.target.SetValue("90")
    for layer, spacing in (("F.Cu", "0.2"), ("In2.Cu", "0.15"), ("B.Cu", "0.25")):
        assert dialog._active_layer == layer
        dialog.spacing.SetValue(spacing)
        save(api, dialog)
        assert api.errors == []
    assert dialog.modal_result == api.wx.ID_OK
    result = api.dialog.analyze(
        api.model.Config(specifications=(dialog.specification,)), snapshot
    )
    assert all(section in api.preview_calls for section in result.sections)
    assert all(section.spec_id == dialog._spec_id for section in result.sections)
    assert all(len(section.net_names) == 2 for section in result.sections)


def test_ok_cycles_from_bottom_and_exclusion_has_explicit_dropdown_status(
    constructor_api: SimpleNamespace,
) -> None:
    """Starting at the last layer must wrap to the first pending layer without closing."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    choose_layer(api, dialog, "B.Cu")
    dialog.include_layer.SetValue(False)
    dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    save(api, dialog)
    assert dialog._active_layer == "F.Cu"
    assert "B.Cu — excluded" in dialog.layer.items
    assert dialog.modal_result is None
    save(api, dialog)
    assert dialog._active_layer == "In2.Cu"
    assert "F.Cu — approved" in dialog.layer.items
    save(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.specification.excluded_layers == ("B.Cu",)
    assert api.errors == []


def test_unavailable_production_preview_never_grants_visual_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """Standalone construction is safe, but cannot approve captures it never displayed."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api), render=None)
    choose_class(api, dialog)
    save(api, dialog)
    assert dialog.specification is None
    assert dialog.modal_result is None
    assert "preview" in dialog.validation_message.GetValue().lower()
    assert "approved" not in "\n".join(dialog.layer.items)
    assert dialog.preview_pane.canvas._image is None


def test_unavailable_membership_has_no_manual_fallback(
    constructor_api: SimpleNamespace,
) -> None:
    """A failed native membership read must not guess routes or silently change mode."""
    api = constructor_api
    board = replace(
        class_snapshot(api), net_class_error="Membership API is unavailable."
    )
    dialog = api.create_dialog(board)
    assert dialog.net_class.enabled is False
    assert "reopen" in dialog.class_hint.GetValue()
    save(api, dialog)
    assert dialog.specification is None
    assert "Membership API is unavailable" in dialog.validation_message.GetValue()
    assert not dialog.preview_pane.sections
    assert api.preview_calls == []
    assert not hasattr(dialog, "selection_mode")


def test_required_unit_conversion_is_atomic_when_a_later_field_is_invalid(
    constructor_api: SimpleNamespace,
) -> None:
    """A valid first gap cannot be converted if a later required gap is invalid."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.200001")
    dialog.ground_gap.SetValue("unfinished")
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    assert dialog.units.GetStringSelection() == "mm"
    assert dialog.spacing.GetValue() == "0.200001"
    assert dialog.ground_gap.GetValue() == "unfinished"
    assert dialog.ground_gap.focused is True
    assert "valid dimensions" in dialog.validation_message.GetValue()
    assert api.errors == []
    dialog.ground_gap.SetValue("0.100001")
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    assert dialog._read_dimension(dialog.spacing.GetValue(), "mil") == 200_001
    assert dialog._read_dimension(dialog.ground_gap.GetValue(), "mil") == 100_001


@pytest.mark.parametrize("invalid_field", ["spacing", "ground_gap"])
def test_irrelevant_invalid_gaps_do_not_block_unit_changes(
    constructor_api: SimpleNamespace, invalid_field: str
) -> None:
    """Unfinished inactive input stays intact without blocking the relevant dimensions."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    active_field = "ground_gap" if invalid_field == "spacing" else "spacing"
    dialog.kind.SetSelection(2 if invalid_field == "spacing" else 1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    getattr(dialog, active_field).SetValue("0.200001")
    getattr(dialog, invalid_field).SetValue("unfinished")
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    assert dialog.units.GetStringSelection() == "mil"
    assert (
        dialog._read_dimension(getattr(dialog, active_field).GetValue(), "mil")
        == 200_001
    )
    assert (
        getattr(dialog, invalid_field).GetValue()
        == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    )
    assert (
        getattr(dialog._layer_drafts[dialog._active_layer], invalid_field)
        == "unfinished"
    )
    assert dialog.validation_message.GetValue() == ""
    assert_field_marker(dialog, invalid_field)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert getattr(dialog, invalid_field).GetValue() == "unfinished"
    assert_field_marker(dialog, invalid_field, "Invalid")


def test_dormant_valid_dimensions_keep_physical_values_through_kind_and_unit_changes(
    constructor_api: SimpleNamespace,
) -> None:
    """Reenabling a previously inactive gap must not reinterpret old mm text as mil."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.200001")
    dialog.ground_gap.SetValue("0.100001")
    dialog.kind.SetSelection(0)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    choose_layer(api, dialog, "In2.Cu")
    choose_layer(api, dialog, "F.Cu")
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.units.GetStringSelection() == "mil"
    assert dialog._read_dimension(dialog.spacing.GetValue(), "mil") == 200_001
    assert dialog._read_dimension(dialog.ground_gap.GetValue(), "mil") == 100_001
    dialog.units.SetStringSelection("mm")
    dialog.units.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == "0.200001"
    assert dialog.ground_gap.GetValue() == "0.100001"


def test_empty_manual_references_are_retained_and_self_reference_can_be_repaired(
    constructor_api: SimpleNamespace,
) -> None:
    """Neither navigation nor persistence can silently replace the user's plane choice."""
    api = constructor_api
    snapshot = class_snapshot(api)
    dialog = api.create_dialog(snapshot)
    choose_class(api, dialog)
    for index in range(len(dialog.layers)):
        dialog.references.Check(index, index == 0)
    dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    assert _checked_reference_layers(dialog) == ()
    assert "Removed F.Cu" in dialog.reference_hint.GetValue()
    save(api, dialog)
    assert dialog.specification is None
    assert_field_marker(dialog, "references", "Required")
    choose_layer(api, dialog, "In2.Cu")
    assert _checked_reference_layers(dialog) == ("In1.Cu", "In3.Cu")
    choose_layer(api, dialog, "F.Cu")
    assert _checked_reference_layers(dialog) == ()
    dialog.adjacent_button.emit(api.wx.EVT_BUTTON)
    assert _checked_reference_layers(dialog) == ("In1.Cu",)
    dialog.references.Check(1, False)
    dialog.references.Check(3, True)
    dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
    assert _checked_reference_layers(dialog) == ("In3.Cu",)
    for layer in ("In2.Cu", "B.Cu"):
        choose_layer(api, dialog, layer)
        dialog.include_layer.SetValue(False)
        dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
        apply_layer(api, dialog)
    choose_layer(api, dialog, "F.Cu")
    save(api, dialog)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.specification.layer_settings[0].reference_layers == ("In3.Cu",)
    reopened = api.create_dialog(snapshot, dialog.specification)
    assert _checked_reference_layers(reopened) == ("In3.Cu",)
    assert "F.Cu — needs review" in reopened.layer.items


def test_approve_walks_pending_images_then_records_layer_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """A click revealing a pending capture cannot approve that capture in the same event."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    pane = dialog.preview_pane
    assert "viewed" in pane.rows.items[0]
    assert "needs review" in pane.rows.items[1]
    assert pane.checklist.table_rows[0][0] == "☑"
    assert pane.checklist.table_rows[1][0] == "—"
    assert pane.checklist.row_colours[0] == (0, 150, 50)
    assert pane.checklist.row_colours[1] == api.wx.SystemSettings.GetColour(
        api.wx.SYS_COLOUR_GRAYTEXT
    )
    dialog.approve_layer.emit(api.wx.EVT_BUTTON)
    assert pane.rows.GetSelection() == 1
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    assert "All workbook images must be viewed" in dialog.validation_message.GetValue()
    assert not dialog.review_tracking.layers
    api.drain()
    assert dialog._layer_drafts["F.Cu"].confirmed is False
    api.paint(pane)
    view_all_rows(api, dialog)
    dialog.approve_layer.emit(api.wx.EVT_BUTTON)
    assert dialog._layer_drafts["F.Cu"].confirmed is True
    assert dialog._active_layer == "In2.Cu"
    assert len(dialog.review_tracking.layers) == 1
    record = dialog.review_tracking.layers[0]
    assert record.layer == "F.Cu"
    assert record.approved_at_utc
    assert record.captures
    assert all(record.viewed_at_utc for record in dialog.review_tracking.images)


def test_inactive_dimension_fields_show_na_placeholder(
    constructor_api: SimpleNamespace,
) -> None:
    """Disabled spacing and ground-gap controls display N/A, not an empty box."""
    api = constructor_api
    dialog = api.create_dialog(class_snapshot(api))
    choose_class(api, dialog)
    assert dialog.kind.GetSelection() == 0
    assert dialog.spacing.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.ground_gap.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.spacing.enabled is False
    assert dialog.ground_gap.enabled is False

    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == ""
    assert dialog.ground_gap.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.spacing.enabled is True
    assert dialog.ground_gap.enabled is False
    dialog.spacing.SetValue("0.2")

    dialog.kind.SetSelection(0)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.ground_gap.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog._layer_drafts[dialog._active_layer].spacing == "0.2"

    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == "0.2"
    assert dialog.ground_gap.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER

    dialog.kind.SetSelection(2)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.spacing.GetValue() == api.dialog.INACTIVE_DIMENSION_PLACEHOLDER
    assert dialog.ground_gap.GetValue() == ""
    assert dialog.ground_gap.enabled is True
