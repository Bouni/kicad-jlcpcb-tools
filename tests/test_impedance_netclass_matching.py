"""Match electrical class intent across real widths, layers, and membership edits."""

from dataclasses import replace
from typing import Any

import pytest

from impedance import model
from impedance.matching import analyze, validate_review

LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "B.Cu")


def trace(
    identity: str,
    net: str = "RF",
    layer: str = "F.Cu",
    width: int = 200000,
    start: int = 0,
    end: int = 10000000,
) -> model.Trace:
    """Return named copper without relying on a routing-default width."""
    return model.Trace(identity, layer, net, width, ((start, 0), (end, 0)))


def snapshot(*traces: model.Trace, **changes: Any) -> model.BoardSnapshot:
    """Represent the native effective constituent membership, not a composite name."""
    values = {
        "layers": LAYERS,
        "traces": traces,
        "context_digest": "board",
        "net_classes": ("Default", "RF", "USB", "Other"),
        "net_class_memberships": tuple(
            (net, ("RF", "USB") if net.startswith("D") else ("RF",))
            for net in sorted({item.net for item in traces if item.net})
        ),
        "net_class_context_digest": "live-classes",
    }
    values.update(changes)
    return model.BoardSnapshot(**values)


def specification(**changes: Any) -> model.Specification:
    """Configure all routed signal layers with explicit local fabrication settings."""
    values = {
        "spec_id": "rf",
        "label": "RF",
        "target_ohms": "50",
        "kind": "single_ended",
        "net_class": "RF",
        "layer_settings": (
            model.LayerSettings("F.Cu", ("In1.Cu",)),
            model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu")),
            model.LayerSettings("B.Cu", ("In3.Cu",)),
        ),
    }
    values.update(changes)
    return model.Specification(**values)


def test_class_selection_includes_neckdowns_and_entire_150mm_multilayer_route() -> None:
    """Actual widths on every signal layer generate rows without default-width filtering."""
    traces = (
        trace("top", start=0, end=50000000),
        trace("neck", width=100000, start=50000000, end=51000000),
        trace("inner", layer="In2.Cu", width=90000, start=51000000, end=101000000),
        trace("bottom", layer="B.Cu", width=210000, start=101000000, end=151000000),
    )
    result = analyze(model.Config(True, (specification(),)), snapshot(*traces))
    assert {item.trace_id for row in result.sections for item in row.traces} == {
        "top",
        "neck",
        "inner",
        "bottom",
    }
    assert {(row.layer, row.width_nm) for row in result.sections} == {
        ("F.Cu", 200000),
        ("F.Cu", 100000),
        ("In2.Cu", 90000),
        ("B.Cu", 210000),
    }
    assert max(row.bounds[2] for row in result.sections) > 150000000


def test_classes_use_constituent_membership_not_names_or_matching_geometry() -> None:
    """A net can belong to multiple classes; unrelated same-width traces stay out."""
    board = snapshot(
        trace("member", "D+"),
        trace("unrelated", "CLK"),
        net_class_memberships=(("D+", ("USB", "RF")), ("CLK", ("Other",))),
    )
    result = analyze(model.Config(True, (specification(),)), board)
    assert [row.net_names for row in result.sections] == [("D+",)]


def test_all_class_members_are_included_without_optional_net_filters() -> None:
    """Selecting a class includes all its routed nets, including additional pair families."""
    board = snapshot(trace("a", "D+"), trace("b", "D-"), trace("c", "D2+"))
    result = analyze(model.Config(True, (specification(),)), board)
    assert {row.net_names for row in result.sections} == {("D+",), ("D-",), ("D2+",)}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"net_class_error": "Native API unavailable"}, "Native API unavailable"),
        ({"net_classes": ()}, "class"),
        ({"net_class_memberships": ()}, "membership|metadata"),
        ({"net_classes": ("Default",)}, "RF"),
    ],
)
def test_missing_class_metadata_fails_actionably(
    changes: dict[str, Any], message: str
) -> None:
    """No unsafe fallback to Default or width matching when class context is absent."""
    with pytest.raises(model.ValidationError, match=message):
        analyze(
            model.Config(True, (specification(),)), snapshot(trace("top"), **changes)
        )


def test_new_routed_layer_requires_settings_or_an_explicit_exclusion() -> None:
    """An unnoticed layer transition cannot silently disappear from the document."""
    spec = specification(layer_settings=(model.LayerSettings("F.Cu", ("In1.Cu",)),))
    board = snapshot(trace("top"), trace("inner", layer="In2.Cu", width=100000))
    with pytest.raises(model.ValidationError, match="In2.Cu"):
        analyze(model.Config(True, (spec,)), board)
    result = analyze(
        model.Config(True, (replace(spec, excluded_layers=("In2.Cu",)),)), board
    )
    assert [row.layer for row in result.sections] == ["F.Cu"]
    assert any(
        "exclud" in warning.lower() and "In2.Cu" in warning
        for warning in result.warnings
    )


def test_duplicate_copper_claims_fail_between_constituent_classes() -> None:
    """Constituent overlap must not emit duplicate or contradictory vendor rows."""
    first = specification()
    second = specification(spec_id="usb", net_class="USB")
    with pytest.raises(model.ValidationError, match="overlap|claimed"):
        analyze(model.Config(True, (first, second)), snapshot(trace("a", "D+")))


@pytest.mark.parametrize(
    "changed", ["membership", "settings", "geometry", "references"]
)
def test_class_review_invalidates_when_intent_or_native_context_changes(
    changed: str,
) -> None:
    """Unchanged section IDs are not sufficient when class assignments/profile settings move."""
    board = snapshot(trace("top"))
    config = model.Config(True, (specification(),))
    first = analyze(config, board)
    reviewed = replace(
        config,
        reviewed_digest=first.digest,
        included_section_ids=tuple(row.section_id for row in first.sections),
    )
    validate_review(reviewed, first)
    if changed == "membership":
        board = replace(board, net_class_memberships=(("RF", ("RF", "Other")),))
    elif changed == "settings":
        board = replace(board, net_class_context_digest="unsaved-class-width-edit")
    elif changed == "geometry":
        board = replace(board, traces=(replace(board.traces[0], width_nm=90000),))
    else:
        reviewed = replace(
            reviewed,
            specifications=(
                specification(
                    layer_settings=(model.LayerSettings("F.Cu", ("In3.Cu",)),)
                ),
            ),
        )
    with pytest.raises(model.ValidationError, match="changed"):
        validate_review(reviewed, analyze(reviewed, board))


def test_multiple_native_pairs_produce_independent_automatic_class_rows() -> None:
    """A shared class produces one row per verified native pair, never one class-wide group."""
    spec = specification(
        kind="differential",
        target_ohms="90",
        layer_settings=(model.LayerSettings("F.Cu", ("In1.Cu",), 200000),),
    )
    board = snapshot(
        *(trace(str(i), net) for i, net in enumerate(("D1+", "D1-", "D2+", "D2-"))),
        differential_pairs=(("D1+", "D1-"), ("D2+", "D2-")),
    )
    config = model.Config(True, (spec,))
    result = analyze(config, board)
    assert len(result.sections) == 2
    assert {row.net_names for row in result.sections} == {
        ("D1+", "D1-"),
        ("D2+", "D2-"),
    }
    reviewed = replace(
        config,
        reviewed_digest=result.digest,
        included_section_ids=tuple(row.section_id for row in result.sections),
    )
    validate_review(reviewed, result)


def test_default_class_does_not_implicitly_match_unassigned_copper() -> None:
    """No-net geometry does not become impedance-controlled just because Default is chosen."""
    board = snapshot(
        trace("named", "CLK"),
        trace("orphan", ""),
        net_class_memberships=(("CLK", ("Default",)),),
    )
    result = analyze(model.Config(True, (specification(net_class="Default"),)), board)
    assert [row.net_names for row in result.sections] == [("CLK",)]


@pytest.mark.parametrize(
    "changes",
    [
        {"net_classes": ("RF", "RF")},
        {"net_classes": ["RF"]},
        {"net_class_context_digest": ""},
        {"net_class_context_digest": None},
        {"net_class_error": None},
        {"net_class_memberships": [["RF", ("RF",)]]},
        {"net_class_memberships": (("RF", ("RF",)), ("RF", ("RF",)))},
        {"net_class_memberships": (("RF", ("MissingClass",)),)},
        {"net_class_memberships": (("RF", ()),)},
        {"net_class_memberships": (("RF", ("RF", "RF")),)},
        {"net_class_memberships": (("RF", ["RF"]),)},
        {"net_class_memberships": (("", ("RF",)),)},
    ],
)
def test_malformed_native_metadata_fails_closed_without_dropping_routes(
    changes: dict[str, Any],
) -> None:
    """Class matching depends on complete immutable adapter metadata, not partial guesses."""
    with pytest.raises(model.ValidationError):
        analyze(model.Config(True, (specification(),)), snapshot(trace("a"), **changes))


def test_class_and_layer_collection_order_does_not_invalidate_review() -> None:
    """Reordered native maps and reference selections carry the same electrical intent."""
    spec = specification()
    board = snapshot(trace("p", "D+"), trace("n", "D-"))
    original = analyze(model.Config(True, (spec,)), board)
    reordered = replace(
        spec,
        layer_settings=tuple(
            replace(
                settings, reference_layers=tuple(reversed(settings.reference_layers))
            )
            for settings in reversed(spec.layer_settings)
        ),
    )
    reordered_board = replace(
        board,
        net_classes=tuple(reversed(board.net_classes)),
        net_class_memberships=tuple(
            (net, tuple(reversed(names)))
            for net, names in reversed(board.net_class_memberships)
        ),
    )
    assert analyze(model.Config(True, (reordered,)), reordered_board) == original


def test_separate_net_classes_can_have_independent_impedance_requirements() -> None:
    """Independent requirements use engineers' native classes, not hidden name filters."""
    first = specification(spec_id="first", net_class="RF")
    second = specification(spec_id="second", net_class="USB", target_ohms="90")
    board = snapshot(
        *(trace(str(i), net) for i, net in enumerate(("D1+", "D1-", "D2+", "D2-"))),
        net_class_memberships=(
            ("D1+", ("RF",)),
            ("D1-", ("RF",)),
            ("D2+", ("USB",)),
            ("D2-", ("USB",)),
        ),
    )
    result = analyze(model.Config(True, (first, second)), board)
    assert len(result.sections) == 4
    assert {(row.spec_id, row.net_names) for row in result.sections} == {
        ("first", ("D1+",)),
        ("first", ("D1-",)),
        ("second", ("D2+",)),
        ("second", ("D2-",)),
    }
