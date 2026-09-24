"""Validate net-class-only intent and explicit handling of older board settings."""

from copy import deepcopy
from typing import Any

import pytest

from impedance import model


def class_payload() -> dict[str, Any]:
    """Describe schema five independently of the implementation's serializer."""
    return {
        "schema_version": 5,
        "enabled": True,
        "specifications": [
            {
                "spec_id": "usb",
                "label": "USB Ω",
                "target_ohms": "90.0",
                "kind": "differential_coplanar",
                "net_class": "USB",
                "layer_settings": [
                    {
                        "layer": "F.Cu",
                        "reference_layers": ["In1.Cu"],
                        "spacing_nm": 203200,
                        "ground_gap_nm": 220000,
                    },
                    {
                        "layer": "In2.Cu",
                        "reference_layers": ["In1.Cu", "In3.Cu"],
                        "spacing_nm": 150000,
                        "ground_gap_nm": 130000,
                    },
                ],
                "excluded_layers": ["B.Cu"],
            }
        ],
        "reviewed_digest": "approved-revision",
        "included_section_ids": ["pair-row"],
        "review_tracking": {"images": [], "layers": []},
        "stackup": None,
        "width_results": [],
    }


def class_spec(**changes: Any) -> model.Specification:
    """Create a class-selected single-ended multilayer requirement."""
    values = {
        "spec_id": "rf",
        "label": "RF",
        "target_ohms": "50",
        "kind": "single_ended",
        "net_class": "RF",
        "layer_settings": (
            model.LayerSettings("F.Cu", ("In1.Cu",)),
            model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu")),
        ),
    }
    values.update(changes)
    return model.Specification(**values)


def test_schema_five_roundtrips_class_intent_and_layer_overrides_exactly() -> None:
    """Keep target precision, pair/ground gaps, exclusions, and both inner planes."""
    payload = class_payload()
    original = deepcopy(payload)
    config = model.Config.from_dict(payload)
    assert config.to_dict() == original
    assert payload == original
    assert config.specifications[0].net_class == "USB"
    assert config.specifications[0].target_ohms == "90.0"
    assert config.specifications[0].excluded_layers == ("B.Cu",)
    assert model.resolved_layer_settings(config.specifications[0], "In2.Cu") == (
        model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu"), 150000, 130000)
    )


def test_schema_four_preserves_class_intent_but_requires_report_reapproval() -> None:
    """Old grouping does not survive as manufacturing authorization or mutate input."""
    payload = class_payload()
    payload.update(
        schema_version=4,
        section_groups=[["positive", "negative"]],
        legacy_specifications=[],
    )
    original = deepcopy(payload)
    config = model.Config.from_dict(payload)
    expected = class_payload()
    expected.update(reviewed_digest="", included_section_ids=[])
    assert config.to_dict() == expected
    assert payload == original


@pytest.mark.parametrize("version", [1, 2, 3])
def test_retired_selection_schemas_require_explicit_reset(version: int) -> None:
    """Never reinterpret historical width/net filters or overwrite their source."""
    payload = {
        "schema_version": version,
        "enabled": True,
        "specifications": [{"selectors": [{"layer": "F.Cu", "width_nm": 180000}]}],
    }
    original = deepcopy(payload)
    with pytest.raises(model.ValidationError, match="Reset settings.*replace"):
        model.Config.from_dict(payload)
    assert payload == original


def test_schema_four_with_retired_intent_requires_reset_without_mutation() -> None:
    """Mixed documents must not silently lose specifications when loaded."""
    payload = class_payload()
    payload.update(
        schema_version=4,
        section_groups=[],
        legacy_specifications=[{"selection_mode": "width", "selectors": []}],
    )
    original = deepcopy(payload)
    with pytest.raises(model.ValidationError, match="Reset settings.*replace"):
        model.Config.from_dict(payload)
    assert payload == original


@pytest.mark.parametrize(
    "changes",
    [
        {"net_class": ""},
        {"net_class": " "},
        {"layer_settings": ()},
        {"layer_settings": (model.LayerSettings("F.Cu", ("B.Cu",)),) * 2},
        {"excluded_layers": ("F.Cu",)},
        {"excluded_layers": ("B.Cu", "B.Cu")},
    ],
)
def test_netclass_rejects_ambiguous_or_incomplete_intent(
    changes: dict[str, Any],
) -> None:
    """Each class requires unique configured layers distinct from its exclusions."""
    with pytest.raises(model.ValidationError):
        model.validate_config(model.Config(True, (class_spec(**changes),)))


@pytest.mark.parametrize(
    ("kind", "spacing", "ground_gap"),
    [
        ("differential", None, None),
        ("single_ended_coplanar", None, None),
        ("differential_coplanar", 100000, None),
        ("differential_coplanar", None, 100000),
        ("single_ended", True, None),
        ("single_ended", None, -1),
    ],
)
def test_each_layer_requires_valid_gaps_for_its_kind(
    kind: str, spacing: Any, ground_gap: Any
) -> None:
    """Per-layer validation cannot borrow a required gap from another layer."""
    spec = class_spec(
        kind=kind,
        layer_settings=(
            model.LayerSettings("F.Cu", ("In1.Cu",), 203200, 220000),
            model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu"), spacing, ground_gap),
        ),
    )
    with pytest.raises(model.ValidationError):
        model.validate_config(model.Config(True, (spec,)))


@pytest.mark.parametrize(
    "references", [(), ("F.Cu",), ("In1.Cu", "In1.Cu"), ("missing",)]
)
def test_layer_reference_names_are_nonempty_unique_and_real(
    references: tuple[str, ...],
) -> None:
    """Signal layers cannot reference themselves or unavailable copper."""
    spec = class_spec(layer_settings=(model.LayerSettings("F.Cu", references),))
    with pytest.raises(model.ValidationError):
        model.validate_config(model.Config(True, (spec,)), ("F.Cu", "In1.Cu", "B.Cu"))


def test_unconfigured_layer_cannot_borrow_another_layers_settings() -> None:
    """Missing layer intent must be reviewed rather than silently inferred."""
    with pytest.raises(model.ValidationError, match="no configured settings"):
        model.resolved_layer_settings(class_spec(), "B.Cu")


@pytest.mark.parametrize(
    "field",
    [
        "selectors",
        "reference_layers",
        "spacing_nm",
        "net_names",
        "selection_mode",
        "typo",
    ],
)
def test_current_class_json_rejects_retired_and_unknown_fields(field: str) -> None:
    """Reopening a document must never silently discard unsupported selection data."""
    payload = class_payload()
    payload["specifications"][0][field] = []
    with pytest.raises(model.ValidationError, match="Invalid specification"):
        model.Config.from_dict(payload)


@pytest.mark.parametrize("field", ["section_groups", "legacy_specifications"])
def test_schema_five_rejects_retired_top_level_fields(field: str) -> None:
    """Only schema four has a read-only migration contract for retired keys."""
    payload = class_payload()
    payload[field] = []
    with pytest.raises(model.ValidationError, match="Invalid configuration"):
        model.Config.from_dict(payload)
