"""Native landing ownership informs spacing without changing captured copper."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from impedance.model import BoardSnapshot, CopperFeature, Specification, ValidationError
from impedance.pcbnew_adapter import _copper_features
from impedance.review_tracking import source_fingerprint
from impedance.spacing_checks import spacing_input_digest

from .test_impedance_pcbnew_copper_features import LayerPad


@dataclass(frozen=True)
class _OwnedFeature(CopperFeature):
    """Express diagnostic-only metadata before the production model provides it."""

    footprint_id: str = ""
    pad_number: str = ""


def test_native_pad_ownership_is_copied_without_retaining_native_objects() -> None:
    """Duplicated connector pins can be grouped by authoritative native owner."""
    pad = LayerPad()
    pad.GetNumber = lambda: "A6"
    owner = SimpleNamespace(
        m_Uuid=SimpleNamespace(AsString=lambda: "connector-native-uuid"),
        Pads=lambda: (pad,),
    )
    board = SimpleNamespace(GetTracks=lambda: (), GetFootprints=lambda: (owner,))
    module = SimpleNamespace(PAD_SHAPE_CIRCLE=0, PAD_SHAPE_RECT=2)
    features = _copper_features(board, module, {"F.Cu": 0, "B.Cu": 31})
    assert features
    assert {(f.footprint_id, f.pad_number) for f in features} == {
        ("connector-native-uuid", "A6")
    }
    owner.m_Uuid.AsString = lambda: "changed-after-snapshot"
    pad.GetNumber = lambda: "B6"
    assert {(f.footprint_id, f.pad_number) for f in features} == {
        ("connector-native-uuid", "A6")
    }


def test_landing_ownership_does_not_change_manufacturing_source_identity() -> None:
    """Spacing grouping metadata must not invalidate unchanged workbook pixels."""
    plain = CopperFeature("pad", "D+", "F.Cu", "pad", "circle", (0, 0), 200, 200)
    snapshot = BoardSnapshot(("F.Cu", "B.Cu"), (), copper_features=(plain,))
    expected = source_fingerprint(snapshot)
    owned = _OwnedFeature(
        **{**vars(plain), "footprint_id": "connector", "pad_number": "A6"}
    )
    assert source_fingerprint(replace(snapshot, copper_features=(owned,))) == expected
    assert (
        source_fingerprint(
            replace(snapshot, copper_features=(replace(owned, footprint_id="other"),))
        )
        == expected
    )
    assert (
        source_fingerprint(
            replace(snapshot, copper_features=(replace(owned, center=(1, 0)),))
        )
        != expected
    )


def test_missing_native_owner_is_not_inferred_from_reference_or_geometry() -> None:
    """An absent native identity cannot merge two distinct connector landings."""
    owner = SimpleNamespace(GetReference=lambda: "J1", Pads=lambda: (LayerPad(),))
    board = SimpleNamespace(GetTracks=lambda: (), GetFootprints=lambda: (owner,))
    module = SimpleNamespace(PAD_SHAPE_CIRCLE=0, PAD_SHAPE_RECT=2)
    features = _copper_features(board, module, {"F.Cu": 0})
    assert features and all(f.footprint_id == "" for f in features)


@pytest.mark.parametrize("field", ["footprint_id", "pad_number"])
def test_malformed_landing_ownership_rejects_the_snapshot(field: str) -> None:
    """Malformed ownership must fail before grouping or approval fingerprinting."""
    feature = CopperFeature("pad", "D+", "F.Cu", "pad", "circle", (0, 0), 200, 200)
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"), (), copper_features=(replace(feature, **{field: None}),)
    )
    with pytest.raises(ValidationError, match="ownership"):
        source_fingerprint(snapshot)


@pytest.mark.parametrize("field", ["footprint_id", "pad_number"])
def test_landing_ownership_invalidates_spacing_evidence_only(field: str) -> None:
    """A different landing association needs fresh diagnostic approval."""
    feature = CopperFeature(
        "pad",
        "D+",
        "F.Cu",
        "pad",
        "circle",
        (0, 0),
        200,
        200,
        footprint_id="connector",
        pad_number="A6",
    )
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (),
        net_class_memberships=(("D+", ("USB",)), ("D-", ("USB",))),
        differential_pairs=(("D+", "D-"),),
        copper_features=(feature,),
    )
    spec = Specification("usb", "", "90", "differential", "USB")
    changed = replace(
        snapshot, copper_features=(replace(feature, **{field: "different"}),)
    )
    assert source_fingerprint(changed) == source_fingerprint(snapshot)
    assert spacing_input_digest(changed, spec, "F.Cu") != spacing_input_digest(
        snapshot, spec, "F.Cu"
    )
