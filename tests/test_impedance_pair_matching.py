"""Use native differential identity to make complete paired rows automatically."""

from dataclasses import replace
from typing import Any

import pytest

from impedance import matching, model

LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "B.Cu")


def trace(
    identity: str,
    net: str,
    layer: str = "F.Cu",
    width: int = 200000,
    start: int = 0,
    end: int = 2000000,
    y: int = 0,
) -> model.Trace:
    """Return one piece of routed copper with deliberately arbitrary net names."""
    return model.Trace(identity, layer, net, width, ((start, y), (end, y)))


def board(*traces: model.Trace, **changes: Any) -> model.BoardSnapshot:
    """Populate metadata exactly as the adapter contract, without suffix inference."""
    values = {
        "layers": LAYERS,
        "traces": traces,
        "context_digest": "board",
        "net_classes": ("USB",),
        "net_class_memberships": tuple(
            (net, ("USB",)) for net in sorted({item.net for item in traces})
        ),
        "net_class_context_digest": "classes",
        "differential_pairs": (("A", "B"), ("C", "D")),
    }
    values.update(changes)
    return model.BoardSnapshot(**values)


def specification(**changes: Any) -> model.Specification:
    """Configure independently editable gaps and references for three signal layers."""
    values = {
        "spec_id": "usb",
        "label": "USB",
        "target_ohms": "90",
        "kind": "differential",
        "net_class": "USB",
        "layer_settings": (
            model.LayerSettings("F.Cu", ("In1.Cu",), 200000),
            model.LayerSettings("In2.Cu", ("In1.Cu", "In3.Cu"), 90000),
            model.LayerSettings("B.Cu", ("In3.Cu",), 200000),
        ),
    }
    values.update(changes)
    return model.Specification(**values)


def _approved_capture(
    paired: bool,
) -> tuple[model.Config, model.BoardSnapshot]:
    """Approve a real complete 120 mm class route using current public matching APIs."""
    spec = specification(
        kind="differential_coplanar" if paired else "single_ended_coplanar",
        target_ohms="90" if paired else "50",
        layer_settings=(
            model.LayerSettings(
                "F.Cu",
                ("In1.Cu",),
                200000 if paired else None,
                300000,
            ),
        ),
    )
    traces = (trace("a", "A", end=120000000),)
    if paired:
        traces += (trace("b", "B", end=120000000, y=400000),)
    config = model.Config(True, (spec,))
    snapshot = board(*traces)
    analysis = matching.analyze(config, snapshot)
    return replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(row.section_id for row in analysis.sections),
    ), snapshot


@pytest.mark.parametrize("paired", [False, True], ids=["single-ended", "differential"])
def test_stale_capture_approval_preserves_intent_until_explicit_reapproval(
    paired: bool,
) -> None:
    """An old review hash cannot authorize images, and reapproval survives reopening."""
    current, snapshot = _approved_capture(paired)
    original = replace(current, reviewed_digest="previous-capture-policy")
    payload = original.to_dict()
    assert payload["schema_version"] == 5
    reopened = model.Config.from_dict(payload)
    analysis = matching.analyze(reopened, snapshot)
    assert reopened == original
    assert (
        tuple(row.section_id for row in analysis.sections)
        == original.included_section_ids
    )
    with pytest.raises(model.ValidationError, match="capture policy|changed"):
        matching.validate_review(reopened, analysis)
    assert reopened.to_dict() == payload

    reset = replace(reopened, reviewed_digest="", included_section_ids=())
    with pytest.raises(model.ValidationError, match="review"):
        matching.validate_review(reset, matching.analyze(reset, snapshot))
    restored = model.Config.from_dict(current.to_dict())
    assert restored.specifications == original.specifications
    assert restored.included_section_ids == original.included_section_ids
    matching.validate_review(restored, matching.analyze(restored, snapshot))


@pytest.mark.parametrize("paired", [False, True], ids=["single-ended", "differential"])
def test_future_capture_policy_revision_invalidates_approval_without_changing_rows(
    monkeypatch: pytest.MonkeyPatch,
    paired: bool,
) -> None:
    """Changing visual policy invalidates single-ended and paired review, not geometry."""
    approved, snapshot = _approved_capture(paired)
    initial = matching.analyze(approved, snapshot)
    matching.validate_review(approved, initial)
    monkeypatch.setattr(
        matching, "CAPTURE_POLICY_REVISION", matching.CAPTURE_POLICY_REVISION + 1
    )
    changed = matching.analyze(approved, snapshot)
    assert changed.sections == initial.sections
    assert changed.warnings == initial.warnings
    with pytest.raises(model.ValidationError, match="capture policy|changed"):
        matching.validate_review(approved, changed)
    assert changed.digest != initial.digest
    assert model.Config.from_dict(approved.to_dict()) == approved


@pytest.mark.parametrize("paired", [False, True], ids=["single-ended", "differential"])
def test_disabled_capture_configuration_keeps_saved_intent_without_approval(
    paired: bool,
) -> None:
    """Disabling documentation preserves intent without requiring a new image review."""
    saved, snapshot = _approved_capture(paired)
    disabled = replace(saved, enabled=False, reviewed_digest="old-policy")
    reopened = model.Config.from_dict(disabled.to_dict())
    assert reopened == disabled
    matching.validate_review(reopened, matching.analyze(reopened, snapshot))


@pytest.mark.parametrize("layer", ["F.Cu", "In2.Cu", "B.Cu"])
@pytest.mark.parametrize("length", [2000000, 120000000])
@pytest.mark.parametrize("coplanar", [False, True])
def test_native_pair_is_one_complete_row_for_every_design_layer_and_length(
    layer: str, length: int, coplanar: bool
) -> None:
    """Both conductors share a bounding box even at 2 mm or across 120 mm of board."""
    spec = specification()
    if coplanar:
        spec = replace(
            spec,
            kind="differential_coplanar",
            layer_settings=tuple(
                replace(settings, ground_gap_nm=300000)
                for settings in spec.layer_settings
            ),
        )
    snapshot = board(
        trace("a", "A", layer, end=length), trace("b", "B", layer, end=length, y=400000)
    )
    config = model.Config(True, (spec,))
    result = matching.analyze(config, snapshot)
    assert len(result.sections) == 1
    row = result.sections[0]
    assert row.net_names == ("A", "B")
    assert {item.trace_id for item in row.traces} == {"a", "b"}
    assert row.bounds == (-100000, -100000, length + 100000, 500000)
    reviewed = replace(
        config, reviewed_digest=result.digest, included_section_ids=(row.section_id,)
    )
    matching.validate_review(reviewed, result)
    reopened = model.Config.from_dict(reviewed.to_dict())
    matching.validate_review(reopened, matching.analyze(reopened, snapshot))


def test_multiple_pairs_never_merge_and_single_ended_components_stay_independent() -> (
    None
):
    """The same class can hold multiple native pairs without one giant combined row."""
    snapshot = board(
        trace("a", "A"),
        trace("b", "B"),
        trace("c", "C"),
        trace("d", "D"),
        trace("a-island", "A", start=100000000, end=120000000),
        trace("b-island", "B", start=100000000, end=120000000),
    )
    paired = matching.analyze(model.Config(True, (specification(),)), snapshot)
    assert {row.net_names for row in paired.sections} == {("A", "B"), ("C", "D")}
    assert {item.trace_id for row in paired.sections for item in row.traces} == {
        item.trace_id for item in snapshot.traces
    }
    single = matching.analyze(
        model.Config(True, (specification(kind="single_ended", target_ohms="50"),)),
        snapshot,
    )
    assert len(single.sections) == 6
    assert all(len(row.net_names) == 1 for row in single.sections)


def test_transition_and_neckdown_rows_follow_actual_widths_without_half_pairs() -> None:
    """Every pair/layer/width gets its full extent, including separated equal-width islands."""
    pieces = []
    for net, offset in (("A", 0), ("B", 400000)):
        for index, (layer, width, start, end) in enumerate(
            (
                ("F.Cu", 200000, 0, 20000000),
                ("F.Cu", 100000, 20000000, 21000000),
                ("F.Cu", 200000, 21000000, 50000000),
                ("In2.Cu", 90000, 50000000, 100000000),
                ("B.Cu", 210000, 100000000, 150000000),
            )
        ):
            pieces.append(
                trace(f"{net}-{index}", net, layer, width, start, end, offset)
            )
    result = matching.analyze(model.Config(True, (specification(),)), board(*pieces))
    assert {(row.layer, row.width_nm) for row in result.sections} == {
        ("F.Cu", 200000),
        ("F.Cu", 100000),
        ("In2.Cu", 90000),
        ("B.Cu", 210000),
    }
    assert all(row.net_names == ("A", "B") for row in result.sections)
    top = next(
        row for row in result.sections if row.layer == "F.Cu" and row.width_nm == 200000
    )
    assert len(top.traces) == 4
    assert top.bounds[0] < 0 and top.bounds[2] > 50000000


@pytest.mark.parametrize(
    "failure",
    ["orphan", "other_class", "layer", "width", "native_error", "unknown_names"],
)
def test_incomplete_or_unproven_pair_never_exports_a_half_row(failure: str) -> None:
    """Do not infer a mate from similar names, class membership, or spatial proximity."""
    spec = specification()
    snapshot = board(trace("a", "A"), trace("b", "B"))
    if failure == "orphan":
        snapshot = board(trace("a", "A"))
    elif failure == "other_class":
        snapshot = replace(
            snapshot,
            net_classes=("USB", "Other"),
            net_class_memberships=(("A", ("USB",)), ("B", ("Other",))),
        )
    elif failure == "layer":
        snapshot = board(trace("a", "A"), trace("b", "B", "In2.Cu"))
    elif failure == "width":
        snapshot = board(trace("a", "A"), trace("b", "B", width=100000))
    elif failure == "native_error":
        snapshot = replace(
            snapshot, differential_pair_error="Native pairing unavailable"
        )
    else:
        snapshot = replace(snapshot, differential_pairs=(("USB+", "USB-"),))
    with pytest.raises(model.ValidationError, match="pair|mate|Pair"):
        matching.analyze(model.Config(True, (spec,)), snapshot)


@pytest.mark.parametrize(
    "changes",
    [
        {"differential_pairs": [("A", "B")]},
        {"differential_pairs": (("A",),)},
        {"differential_pairs": (("A", "A"),)},
        {"differential_pairs": (("A", "B"), ("B", "A"))},
        {"differential_pairs": (("A", "B"), ("A", "C"))},
        {"differential_pairs": (("A", ""),)},
        {"differential_pairs": (("A", 3),)},
        {"differential_pair_error": None},
    ],
)
def test_malformed_or_ambiguous_pair_metadata_fails_closed(
    changes: dict[str, Any],
) -> None:
    """A native net cannot be assigned to two competing differential pairs."""
    with pytest.raises(model.ValidationError):
        matching.analyze(
            model.Config(True, (specification(),)),
            board(trace("a", "A"), trace("b", "B"), **changes),
        )


def test_pair_identity_changes_invalidate_review_and_order_changes_do_not() -> None:
    """Native pair metadata participates in review revision independent of geometry."""
    snapshot = board(trace("a", "A"), trace("b", "B"), trace("c", "C"), trace("d", "D"))
    config = model.Config(True, (specification(),))
    original = matching.analyze(config, snapshot)
    reordered = replace(snapshot, differential_pairs=(("D", "C"), ("B", "A")))
    assert matching.analyze(config, reordered) == original
    changed = matching.analyze(
        config, replace(snapshot, differential_pairs=(("A", "C"), ("B", "D")))
    )
    assert changed.digest != original.digest
    reviewed = replace(
        config,
        reviewed_digest=original.digest,
        included_section_ids=tuple(row.section_id for row in original.sections),
    )
    with pytest.raises(model.ValidationError, match="changed"):
        matching.validate_review(reviewed, changed)


def test_single_ended_review_ignores_unavailable_or_changed_pair_metadata() -> None:
    """Differential API support does not become a new requirement for single-ended users."""
    snapshot = board(trace("a", "A"))
    config = model.Config(True, (specification(kind="single_ended"),))
    before = matching.analyze(config, snapshot)
    assert (
        matching.analyze(
            config,
            replace(
                snapshot, differential_pairs=(), differential_pair_error="Unsupported"
            ),
        )
        == before
    )


def test_preview_uses_production_pair_geometry_before_document_fields_are_complete() -> (
    None
):
    """Typing references and gaps must not require fake dimensions to see the actual paired capture."""
    snapshot = board(trace("a", "A"), trace("b", "B"))
    final = specification()
    draft = replace(final, target_ohms="", layer_settings=())
    preview = matching.preview_sections(draft, snapshot, "F.Cu")
    assert preview == matching.analyze(model.Config(True, (final,)), snapshot).sections
    with pytest.raises(model.ValidationError):
        matching.analyze(model.Config(True, (draft,)), snapshot)


def test_preview_never_guesses_pair_identity_or_shows_another_layer() -> None:
    """Draft previews still require native identity and stay on the chosen signal layer."""
    draft = replace(specification(), target_ohms="", layer_settings=())
    snapshot = board(trace("a", "A"), trace("b", "B"))
    assert matching.preview_sections(draft, snapshot, "B.Cu") == ()
    with pytest.raises(model.ValidationError, match="pair|mate"):
        matching.preview_sections(
            draft, replace(snapshot, differential_pairs=()), "F.Cu"
        )


@pytest.mark.parametrize("failure", ["kind", "class", "excluded", "layer"])
def test_preview_rejects_malformed_selection_even_when_dimensions_are_drafts(
    failure: str,
) -> None:
    """Incomplete dimensions do not bypass class selection and layer safety."""
    draft = replace(specification(), target_ohms="", layer_settings=())
    snapshot = board(trace("a", "A"), trace("b", "B"))
    layer = "F.Cu"
    if failure == "kind":
        draft = replace(draft, kind="guess")
    elif failure == "class":
        draft = replace(draft, net_class="missing")
    elif failure == "excluded":
        draft = replace(draft, excluded_layers=("missing",))
    else:
        layer = "missing"
    with pytest.raises(model.ValidationError):
        matching.preview_sections(draft, snapshot, layer)


def test_different_class_membership_cannot_silently_drop_a_native_mate() -> None:
    """A mate assigned to another class is an actionable selection error, not a half-pair row."""
    snapshot = board(
        trace("a", "A"),
        trace("b", "B"),
        net_classes=("USB", "Other"),
        net_class_memberships=(("A", ("USB",)), ("B", ("Other",))),
    )
    with pytest.raises(
        model.ValidationError, match="both mates must belong to the selected net class"
    ):
        matching.analyze(model.Config(True, (specification(),)), snapshot)


def test_pair_component_count_is_not_confused_with_final_review_row_limit() -> None:
    """Many same-width pair islands are one full-extent row, not 1000 review rows."""
    snapshot = board(
        *(
            trace(
                f"{net}-{index}",
                net,
                start=index * 3000000,
                end=index * 3000000 + 1000000,
            )
            for net in ("A", "B")
            for index in range(1001)
        )
    )
    result = matching.analyze(model.Config(True, (specification(),)), snapshot)
    assert len(result.sections) == 1
    assert len(result.sections[0].traces) == 2002


def test_distinct_automatic_pair_rows_still_enforce_the_review_limit() -> None:
    """Moving the limit past pair aggregation must not remove the final workbook-row safeguard."""
    pairs = tuple((f"A-{index}", f"B-{index}") for index in range(1001))
    snapshot = board(
        *(trace(net, net) for pair in pairs for net in pair), differential_pairs=pairs
    )
    with pytest.raises(
        model.ValidationError, match="More than 1000 impedance sections"
    ):
        matching.analyze(model.Config(True, (specification(),)), snapshot)
