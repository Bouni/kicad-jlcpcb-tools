"""Keep readable maximal workbook regions without losing routed signal coverage."""

from dataclasses import replace
from itertools import permutations

import pytest

from impedance import matching, review_tracking
from impedance.model import (
    EXCLUSION_COVERED_STUB,
    BoardSnapshot,
    Config,
    CopperFeature,
    LayerSettings,
    Section,
    Specification,
    Trace,
    ValidationError,
)


def _trace(
    identity: str,
    net: str,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    width_nm: int = 203_200,
    layer: str = "F.Cu",
) -> Trace:
    """Supply independent endpoints with exact copper-inclusive bounds."""
    return Trace(identity, layer, net, width_nm, (start, end))


def _board(
    *traces: Trace, copper_features: tuple[CopperFeature, ...] = ()
) -> BoardSnapshot:
    """Provide native class membership for all routed nets, including other intent."""
    return BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=traces,
        context_digest="region-board",
        net_classes=("USB", "Other"),
        net_class_memberships=tuple(
            (net, ("Other",) if net == "OTHER" else ("USB",))
            for net in sorted({trace.net for trace in traces})
        ),
        net_class_context_digest="region-classes",
        differential_pairs=(("/D+", "/D-"), ("INNER+", "INNER-")),
        copper_features=copper_features,
    )


def _config(kind: str = "single_ended") -> Config:
    """Configure both physical signal layers and all required manufacturing fields."""
    return Config(
        enabled=True,
        specifications=(
            Specification(
                "usb",
                "USB",
                "90",
                kind,
                "USB",
                tuple(
                    LayerSettings(
                        layer,
                        (reference,),
                        200_000 if kind.startswith("differential") else None,
                        300_000 if kind.endswith("_coplanar") else None,
                    )
                    for layer, reference in (("F.Cu", "B.Cu"), ("B.Cu", "F.Cu"))
                ),
            ),
        ),
    )


def _outer() -> Trace:
    """Span a rectangle independently of the small route it documents."""
    return _trace("outer", "/D+", (0, 0), (4_000_000, 2_000_000))


def _assert_complete_coverage(
    sections: tuple[Section, ...], traces: tuple[Trace, ...]
) -> None:
    """Every source item belongs to one row which names its signal and encloses it."""
    actual = [trace.trace_id for section in sections for trace in section.traces]
    assert sorted(actual) == sorted(trace.trace_id for trace in traces)
    for section in sections:
        assert section.net_names == tuple(
            sorted({trace.net for trace in section.traces})
        )
        left, top, right, bottom = section.bounds
        for trace in section.traces:
            radius = (trace.width_nm + 1) // 2
            assert left <= min(point[0] for point in trace.points) - radius
            assert top <= min(point[1] for point in trace.points) - radius
            assert right >= max(point[0] for point in trace.points) + radius
            assert bottom >= max(point[1] for point in trace.points) + radius


def test_disconnected_dplus_pieces_grow_into_one_complete_signal_region() -> None:
    """Nearby 6 mm pieces remain recognizable together despite a 1 mm track gap."""
    traces = (
        _trace("positive-left", "/D+", (0, 0), (6_000_000, 0)),
        _trace("positive-right", "/D+", (7_000_000, 0), (13_000_000, 0)),
        _trace("negative", "/D-", (0, 400_000), (13_000_000, 400_000)),
    )
    config = _config()
    snapshot = _board(*traces)
    result = matching.analyze(config, snapshot)
    assert len(result.sections) == 2
    _assert_complete_coverage(result.sections, traces)
    positive = next(
        section for section in result.sections if section.net_names == ("/D+",)
    )
    assert {trace.trace_id for trace in positive.traces} == {
        "positive-left",
        "positive-right",
    }
    assert (
        matching.preview_sections(config.specifications[0], snapshot, "F.Cu")
        == result.sections
    )


def test_pruned_nested_dminus_fragment_keeps_membership_and_signal_label() -> None:
    """Removing a redundant inner box never removes its routed signal from review."""
    outer = _outer()
    fragment = _trace(
        "negative-fragment", "/D-", (1_000_000, 1_000_000), (1_200_000, 1_000_000)
    )
    snapshot = _board(outer, fragment)
    result = matching.analyze(_config(), snapshot)
    assert len(result.sections) == 1
    _assert_complete_coverage(result.sections, snapshot.traces)
    assert result.sections[0].net_names == ("/D+", "/D-")
    assert result.sections[0].bounds == (-101_600, -101_600, 4_101_600, 2_101_600)


def test_nearly_overlapping_usb_boxes_keep_complete_coverage_of_each_signal() -> None:
    """Partial overlap leaves two useful boxes, including both disconnected D+ pieces."""
    traces = (
        _trace("positive-left", "/D+", (0, 0), (6_000_000, 1_000_000)),
        _trace(
            "positive-right", "/D+", (7_000_000, 1_000_000), (13_000_000, 2_000_000)
        ),
        _trace("negative", "/D-", (0, 500_000), (13_000_000, 2_500_000)),
    )
    config = _config()
    snapshot = _board(*traces)
    result = matching.analyze(config, snapshot)
    assert len(result.sections) == 2
    _assert_complete_coverage(result.sections, traces)
    assert {section.net_names for section in result.sections} == {("/D+",), ("/D-",)}
    assert (
        matching.preview_sections(config.specifications[0], snapshot, "F.Cu")
        == result.sections
    )


@pytest.mark.parametrize("reverse_direction", [False, True])
def test_signal_growth_and_pruning_are_stable_under_enumeration_and_direction(
    reverse_direction: bool,
) -> None:
    """Track enumeration and storage direction cannot alter images or review identity."""
    traces = (
        _trace("positive-left", "/D+", (0, 0), (6_000_000, 1_000_000)),
        _trace(
            "positive-right", "/D+", (7_000_000, 1_000_000), (13_000_000, 2_000_000)
        ),
        _trace("negative", "/D-", (1_000_000, 500_000), (12_000_000, 1_500_000)),
    )
    config = _config()
    original = matching.analyze(config, _board(*traces))
    for permutation in permutations(traces):
        items = tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            if reverse_direction
            else trace
            for trace in permutation
        )
        actual = matching.analyze(config, _board(*items))
        _assert_complete_coverage(actual.sections, items)
        assert actual.digest == original.digest
        assert tuple(
            (
                row.section_id,
                row.bounds,
                row.net_names,
                tuple(trace.trace_id for trace in row.traces),
            )
            for row in actual.sections
        ) == tuple(
            (
                row.section_id,
                row.bounds,
                row.net_names,
                tuple(trace.trace_id for trace in row.traces),
            )
            for row in original.sections
        )


@pytest.mark.parametrize("kind", ["single_ended", "single_ended_coplanar"])
@pytest.mark.parametrize("inner_net", ["/D+", "/D-"])
def test_nested_region_is_absent_from_analysis_and_incomplete_draft_preview(
    kind: str, inner_net: str
) -> None:
    """A tiny disconnected region adds membership without a redundant inner box."""
    outer = _outer()
    inner = _trace(
        "fragment", inner_net, (1_000_000, 1_000_000), (1_200_000, 1_000_000)
    )
    config = _config(kind)
    snapshot = _board(outer, inner)
    actual = matching.analyze(config, snapshot).sections
    assert len(actual) == 1
    _assert_complete_coverage(actual, snapshot.traces)
    assert actual[0].bounds == (-101_600, -101_600, 4_101_600, 2_101_600)
    assert actual[0].net_names == tuple(sorted({outer.net, inner_net}))
    draft = replace(config.specifications[0], layer_settings=())
    assert matching.preview_sections(draft, snapshot, "F.Cu") == actual


@pytest.mark.parametrize(
    ("start", "end", "suppressed"),
    [
        ((0, 0), (200_000, 0), True),
        ((3_800_000, 2_000_000), (4_000_000, 2_000_000), True),
        ((0, 0), (4_000_000, 2_000_000), True),
        ((-1, 1_000_000), (200_000, 1_000_000), False),
        ((3_800_000, 1_000_000), (4_000_001, 1_000_000), False),
        ((1_000_000, -1), (1_200_000, 0), False),
        ((1_000_000, 2_000_000), (1_200_000, 2_000_001), False),
        ((3_000_000, 1_000_000), (5_000_000, 3_000_000), False),
        ((5_000_000, 0), (6_000_000, 0), False),
    ],
    ids=[
        "shared-top-left",
        "shared-bottom-right",
        "equal-bounds",
        "one-nm-outside-left",
        "one-nm-outside-right",
        "one-nm-outside-top",
        "one-nm-outside-bottom",
        "partial-overlap",
        "disjoint",
    ],
)
def test_only_complete_raw_bounds_containment_suppresses_a_region(
    start: tuple[int, int], end: tuple[int, int], suppressed: bool
) -> None:
    """Shared edges and equal boxes prune; viewport padding and partial overlap do not."""
    outer = _outer()
    inner = _trace("fragment", "/D-", start, end)
    result = matching.analyze(_config(), _board(outer, inner))
    assert len(result.sections) == (1 if suppressed else 2)
    _assert_complete_coverage(result.sections, (outer, inner))
    if suppressed:
        assert result.sections[0].net_names == ("/D+", "/D-")
        assert result.sections[0].bounds == (-101_600, -101_600, 4_101_600, 2_101_600)


def test_nested_chain_keeps_only_outermost_region_in_every_input_order() -> None:
    """Removing a containing intermediate row cannot resurrect its inner row."""
    outer = _outer()
    middle = _trace("middle", "MIDDLE", (500_000, 500_000), (3_500_000, 1_500_000))
    inner = _trace("inner", "INNER", (1_000_000, 1_000_000), (1_200_000, 1_000_000))
    results = [
        matching.analyze(_config(), _board(*items))
        for items in permutations((outer, middle, inner))
    ]
    assert all(result == results[0] for result in results)
    assert len(results[0].sections) == 1
    _assert_complete_coverage(results[0].sections, (outer, middle, inner))
    assert results[0].sections[0].bounds == (-101_600, -101_600, 4_101_600, 2_101_600)
    assert results[0].sections[0].net_names == ("/D+", "INNER", "MIDDLE")


@pytest.mark.parametrize("difference", ["width", "layer", "specification"])
def test_containment_preserves_distinct_manufacturing_geometry(difference: str) -> None:
    """Another width, signal layer or specification still needs its own row."""
    outer = _outer()
    inner = _trace("fragment", "/D-", (1_000_000, 1_000_000), (1_200_000, 1_000_000))
    config = _config()
    if difference == "width":
        inner = replace(inner, width_nm=inner.width_nm + 1)
    elif difference == "layer":
        inner = replace(inner, layer="B.Cu")
    else:
        inner = replace(inner, net="OTHER")
        other = replace(config.specifications[0], spec_id="other", net_class="Other")
        config = replace(config, specifications=(*config.specifications, other))
    result = matching.analyze(config, _board(outer, inner))
    assert len(result.sections) == 2
    assert {trace for section in result.sections for trace in section.traces} == {
        outer,
        inner,
    }


@pytest.mark.parametrize("covered", ["outer", "fragment"])
def test_covered_stub_neither_suppresses_nor_is_suppressed_by_an_ordinary_row(
    covered: str,
) -> None:
    """An unchecked audit row cannot replace the useful included region."""
    outer = _outer()
    inner = _trace("fragment", "/D-", (1_000_000, 1_000_000), (1_200_000, 1_000_000))
    feature = CopperFeature(
        "pad",
        outer.net if covered == "outer" else inner.net,
        "F.Cu",
        "pad",
        "rect",
        (2_000_000, 1_000_000),
        10_000_000,
        10_000_000,
    )
    result = matching.analyze(
        _config(), _board(outer, inner, copper_features=(feature,))
    )
    assert len(result.sections) == 2
    assert {
        section.traces[0].trace_id: section.exclusion_reason
        for section in result.sections
    } == {
        "outer": EXCLUSION_COVERED_STUB if covered == "outer" else "",
        "fragment": EXCLUSION_COVERED_STUB if covered == "fragment" else "",
    }


@pytest.mark.parametrize("kind", ["differential", "differential_coplanar"])
def test_nested_native_differential_pairs_remain_independent(kind: str) -> None:
    """A containing pair's view cannot replace another pair's manufacturing row."""
    snapshot = _board(
        _outer(),
        _trace("outer-mate", "/D-", (0, 400_000), (4_000_000, 2_400_000)),
        _trace(
            "inner-positive", "INNER+", (1_000_000, 1_000_000), (1_200_000, 1_000_000)
        ),
        _trace(
            "inner-negative", "INNER-", (1_000_000, 1_400_000), (1_200_000, 1_400_000)
        ),
    )
    config = _config(kind)
    result = matching.analyze(config, snapshot)
    assert {section.net_names for section in result.sections} == {
        ("/D+", "/D-"),
        ("INNER+", "INNER-"),
    }
    assert (
        matching.preview_sections(config.specifications[0], snapshot, "F.Cu")
        == result.sections
    )


def test_containment_cannot_hide_overlapping_specification_claims() -> None:
    """Validate all raw routed copper before removing redundant document regions."""
    config = _config()
    duplicate = replace(config.specifications[0], spec_id="duplicate")
    config = replace(config, specifications=(*config.specifications, duplicate))
    inner = _trace("fragment", "/D-", (1_000_000, 1_000_000), (1_200_000, 1_000_000))
    with pytest.raises(ValidationError, match="specifications overlap"):
        matching.analyze(config, _board(_outer(), inner))


def test_containment_cannot_hide_an_incomplete_native_pair() -> None:
    """A nested orphan must still fail complete-pair validation in drafts and exports."""
    snapshot = _board(
        _outer(),
        _trace("outer-mate", "/D-", (0, 400_000), (4_000_000, 2_400_000)),
        _trace("orphan", "INNER+", (1_000_000, 1_000_000), (1_200_000, 1_000_000)),
    )
    config = _config("differential")
    with pytest.raises(ValidationError, match="incomplete"):
        matching.analyze(config, snapshot)
    with pytest.raises(ValidationError, match="incomplete"):
        matching.preview_sections(config.specifications[0], snapshot, "F.Cu")


def test_algorithm_revision_requires_reapproval_without_changing_surviving_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous grouping algorithm's approval cannot authorize the new row policy."""
    config = _config()
    snapshot = _board(_outer())
    monkeypatch.setattr(matching, "ALGORITHM_VERSION", 7, raising=False)
    legacy = matching.analyze(config, snapshot)
    reviewed = replace(
        config,
        reviewed_digest=legacy.digest,
        included_section_ids=tuple(section.section_id for section in legacy.sections),
    )
    matching.validate_review(reviewed, legacy)
    monkeypatch.setattr(matching, "ALGORITHM_VERSION", 8)
    current = matching.analyze(config, snapshot)
    assert current.sections == legacy.sections
    assert current.digest != legacy.digest
    with pytest.raises(ValidationError, match="changed"):
        matching.validate_review(reviewed, current)


def test_editing_a_transferred_trace_invalidates_review_with_unchanged_region_bounds() -> (
    None
):
    """A merged inner signal stays in row identity, review and image fingerprints."""
    config = _config()
    inner = _trace("fragment", "/D-", (1_000_000, 1_000_000), (1_200_000, 1_000_000))
    original = _board(_outer(), inner)
    before = matching.analyze(config, original)
    changed = _board(
        _outer(),
        replace(inner, points=((1_000_000, 1_000_000), (1_300_000, 1_000_000))),
    )
    after = matching.analyze(config, changed)
    assert len(before.sections) == 1
    _assert_complete_coverage(before.sections, original.traces)
    _assert_complete_coverage(after.sections, changed.traces)
    assert after.sections[0].bounds == before.sections[0].bounds
    assert after.sections[0].section_id != before.sections[0].section_id
    assert after.digest != before.digest
    assert review_tracking.source_fingerprint(
        changed
    ) != review_tracking.source_fingerprint(original)
    reviewed = replace(
        config,
        reviewed_digest=before.digest,
        included_section_ids=(before.sections[0].section_id,),
    )
    matching.validate_review(reviewed, before)
    with pytest.raises(ValidationError, match="changed"):
        matching.validate_review(reviewed, after)
