"""Link equivalent layer settings through production dialog constructors/events."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from .test_impedance_dialog import _snapshot, constructor_api  # noqa: F401
from .test_impedance_netclass_dialog import choose_class, choose_layer
from .test_impedance_spec_width_hint import _stackup


def _open(api: SimpleNamespace, specification: object = None) -> object:
    """Route both sides of the same symmetric two-layer construction."""
    original = _snapshot(api)
    board = replace(
        original,
        traces=original.traces
        + tuple(
            replace(trace, trace_id=trace.trace_id + "-bottom", layer="B.Cu")
            for trace in original.traces
        ),
        net_classes=("USB",),
        net_class_memberships=(("CLK_P", ("USB",)), ("CLK_N", ("USB",))),
    )
    dialog = api.dialog.SpecificationDialog(
        None, board, specification=specification, stackup=_stackup(api)
    )
    if specification is None:
        choose_class(api, dialog, "USB")
        dialog.kind.SetSelection(3)
        dialog.kind.emit(api.wx.EVT_CHOICE)
    return dialog


def test_equivalent_layers_share_dimensions_and_mapped_references(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Editing one equivalent side fills its counterpart without reviewing it."""
    api = constructor_api
    dialog = _open(api)
    dialog.spacing.SetValue("0.200001")
    dialog.ground_gap.SetValue("0.100001")
    choose_layer(api, dialog, "B.Cu")
    assert dialog.spacing.GetValue() == "0.200001"
    assert dialog.ground_gap.GetValue() == "0.100001"
    assert dialog.reference_selection.selected == ("F.Cu",)
    assert "F.Cu" in dialog.link_hint.GetLabel()
    assert not dialog._layer_drafts["B.Cu"].confirmed
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.specification.linked_layer_groups == (("F.Cu", "B.Cu"),)
    assert {item.spacing_nm for item in dialog.specification.layer_settings} == {
        200_001
    }


def test_apply_unfinished_settings_does_not_require_review(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """A blank differential draft can be accepted without approving any captures."""
    api = constructor_api
    dialog = _open(api)
    dialog.target.SetValue("")
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert dialog.specification.target_ohms == ""
    assert all(item.spacing_nm is None for item in dialog.specification.layer_settings)
    assert not dialog.review_tracking.layers


def test_layer_override_survives_apply_and_reopen(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """An explicit override keeps its physical value and detachment on reopening."""
    api = constructor_api
    dialog = _open(api)
    dialog.spacing.SetValue("0.2")
    choose_layer(api, dialog, "B.Cu")
    dialog.independent_layer.SetValue(True)
    dialog.independent_layer.emit(api.wx.EVT_CHECKBOX)
    dialog.spacing.SetValue("0.25")
    choose_layer(api, dialog, "F.Cu")
    assert dialog.spacing.GetValue() == "0.2"
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    spec = dialog.specification
    assert "B.Cu" in spec.independent_layers
    reopened = _open(api, spec)
    choose_layer(api, reopened, "B.Cu")
    assert reopened.independent_layer.GetValue() is True
    assert reopened.spacing.GetValue() == "0.25"


def test_existing_divergent_settings_are_preserved_as_overrides(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Opening old per-layer intent never chooses a value to overwrite the other."""
    api = constructor_api
    spec = api.model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (
            api.model.LayerSettings("F.Cu", ("B.Cu",), 200_001),
            api.model.LayerSettings("B.Cu", ("F.Cu",), 250_003),
        ),
    )
    dialog = _open(api, spec)
    assert dialog.spacing.GetValue() == "0.200001"
    choose_layer(api, dialog, "B.Cu")
    assert dialog.spacing.GetValue() == "0.250003"
    assert dialog.independent_layer.GetValue() is True
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert tuple(item.spacing_nm for item in dialog.specification.layer_settings) == (
        200_001,
        250_003,
    )


def test_linked_units_preserve_exact_nanometres(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Synchronization converts physical dimensions into each peer's display unit."""
    api = constructor_api
    dialog = _open(api)
    dialog.spacing.SetValue("0.200001")
    choose_layer(api, dialog, "B.Cu")
    dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    choose_layer(api, dialog, "F.Cu")
    dialog.spacing.SetValue("0.225003")
    choose_layer(api, dialog, "B.Cu")
    assert dialog.units.GetStringSelection() == "mil"
    assert dialog._read_dimension(dialog.spacing.GetValue(), "mil") == 225_003


def test_different_saved_reference_selections_remain_independent(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """A missing plane selection cannot be advertised as linked to a configured one."""
    api = constructor_api
    spec = api.model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (
            api.model.LayerSettings("F.Cu", (), 200_000),
            api.model.LayerSettings("B.Cu", ("F.Cu",), 200_000),
        ),
    )
    dialog = _open(api, spec)
    assert not dialog._linked_layer_groups
    assert dialog.independent_layer.GetValue() is True
    dialog.spacing.SetValue("0.25")
    choose_layer(api, dialog, "B.Cu")
    assert dialog.spacing.GetValue() == "0.2"


def test_rejoining_replaces_invalid_peer_with_valid_source(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Explicit rejoining applies the chosen source even to unfinished peer input."""
    api = constructor_api
    dialog = _open(api)
    dialog.spacing.SetValue("0.2")
    choose_layer(api, dialog, "B.Cu")
    dialog.independent_layer.SetValue(True)
    dialog.independent_layer.emit(api.wx.EVT_CHECKBOX)
    dialog.spacing.SetValue("unfinished")
    choose_layer(api, dialog, "F.Cu")
    dialog.independent_layer.SetValue(False)
    dialog.independent_layer.emit(api.wx.EVT_CHECKBOX)
    choose_layer(api, dialog, "B.Cu")
    assert dialog.spacing.GetValue() == "0.2"


def test_kind_reactivation_synchronizes_linked_hidden_dimensions(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Rejoining while gaps are inactive cannot save conflicting linked values."""
    api = constructor_api
    dialog = _open(api)
    dialog.spacing.SetValue("0.200001")
    dialog.ground_gap.SetValue("0.100001")
    choose_layer(api, dialog, "B.Cu")
    dialog.independent_layer.SetValue(True)
    dialog.independent_layer.emit(api.wx.EVT_CHECKBOX)
    dialog.ground_gap.SetValue("0.250003")
    dialog.kind.SetSelection(0)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.independent_layer.SetValue(False)
    dialog.independent_layer.emit(api.wx.EVT_CHECKBOX)
    assert dialog._linked_layer_groups == (("F.Cu", "B.Cu"),)
    dialog.kind.SetSelection(3)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert {item.ground_gap_nm for item in dialog.specification.layer_settings} == {
        250_003
    }
    reopened = _open(api, dialog.specification)
    assert reopened._linked_layer_groups == (("F.Cu", "B.Cu"),)
    choose_layer(api, reopened, "F.Cu")
    assert reopened.ground_gap.GetValue() == "0.250003"
