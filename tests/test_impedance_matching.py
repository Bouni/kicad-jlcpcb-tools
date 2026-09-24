"""Exercise native class matching, complete route geometry, and review persistence."""

from copy import deepcopy
from dataclasses import replace
from math import cos, pi, sin
from typing import Any, Optional

import pytest

from impedance.matching import analyze, validate_review
from impedance.model import (
    BoardSnapshot,
    Config,
    LayerSettings,
    Specification,
    Trace,
    ValidationError,
    format_length,
    parse_length,
    validate_config,
)


def _spec(
    spec_id: str = "clock",
    *,
    reference_layers: tuple[str, ...] = ("B.Cu",),
    spacing_nm: Optional[int] = None,
    ground_gap_nm: Optional[int] = None,
    **changes: Any,
) -> Specification:
    """Return class intent with independently specified per-layer fabrication fields."""
    specification = Specification(
        spec_id=spec_id,
        label="Clock",
        target_ohms="50",
        kind="single_ended",
        net_class="RF",
        layer_settings=(
            LayerSettings("F.Cu", reference_layers, spacing_nm, ground_gap_nm),
        ),
    )
    return replace(specification, **changes)


def _trace(
    trace_id: str,
    points: tuple[tuple[int, int], ...],
    *,
    layer: str = "F.Cu",
    net: str = "CLK",
    width_nm: int = 150000,
) -> Trace:
    """Create an extracted trace without requiring the KiCad Python module."""
    return Trace(trace_id, layer, net, width_nm, points)


def _snapshot(*traces: Trace, context_digest: str = "board-context") -> BoardSnapshot:
    """Return complete native class metadata for every named routed net."""
    return BoardSnapshot(
        ("F.Cu", "B.Cu"),
        tuple(traces),
        context_digest,
        net_classes=("RF", "Other"),
        net_class_memberships=tuple(
            (net, ("Other",) if net == "ORDINARY" else ("RF",))
            for net in sorted({trace.net for trace in traces if trace.net})
        ),
        net_class_context_digest="class-settings",
    )


def _config(*specifications: Specification) -> Config:
    """Create an enabled configuration with at least one useful default rule."""
    return Config(enabled=True, specifications=specifications or (_spec(),))


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("0.15", "mm", 150000),
        (" 0.150000 ", "mm", 150000),
        ("0.000001", "mm", 1),
        ("1", "mil", 25400),
        ("5.5", "mil", 139700),
        ("0.005", "mil", 127),
    ],
)
def test_parse_length_preserves_exact_physical_units(
    text: str, unit: str, expected: int
) -> None:
    """Convert decimal input without floating-point width mismatches."""
    assert parse_length(text, unit) == expected


@pytest.mark.parametrize(
    "text", ["", " ", "0", "-1", "NaN", "Infinity", "-Infinity", "abc", "1,5"]
)
def test_parse_length_rejects_nonpositive_or_nonfinite_input(text: str) -> None:
    """Reject invalid physical widths before they reach the matching engine."""
    with pytest.raises(ValidationError):
        parse_length(text)


@pytest.mark.parametrize("text", ["0.0000001", "0.1500001"])
def test_parse_length_rejects_fractional_nanometres(text: str) -> None:
    """Do not silently round an entered width into a different physical dimension."""
    with pytest.raises(ValidationError):
        parse_length(text)


@pytest.mark.parametrize("unit", ["inch", "cm", "", "unknown"])
def test_length_units_must_be_explicitly_supported(unit: str) -> None:
    """Avoid silently interpreting an unsupported unit as millimetres."""
    with pytest.raises(ValidationError):
        parse_length("1", unit)
    with pytest.raises(ValidationError):
        format_length(150000, unit)


@pytest.mark.parametrize(
    ("value_nm", "unit", "expected"),
    [(150000, "mm", "0.15"), (1000000, "mm", "1"), (25400, "mil", "1")],
)
def test_format_length_uses_clean_decimal_values(
    value_nm: int, unit: str, expected: str
) -> None:
    """Display exact common fabrication widths without gratuitous zeros."""
    assert format_length(value_nm, unit) == expected


def test_config_round_trip_preserves_specs_and_review_state() -> None:
    """Persist class intent and explicit review selection in the current payload."""
    original = replace(
        _config(_spec(kind="differential", target_ohms="90.0", spacing_nm=200000)),
        reviewed_digest="reviewed-board-hash",
        included_section_ids=("section-a", "section-b"),
    )
    payload = original.to_dict()
    assert payload["schema_version"] == 5
    assert Config.from_dict(payload) == original


def test_default_disabled_config_is_valid_and_round_trips() -> None:
    """A board without impedance settings keeps the normal export path."""
    config = Config()
    validate_config(config)
    assert Config.from_dict(config.to_dict()) == config


@pytest.mark.parametrize("version", [None, 0, 3, "1", True])
def test_config_rejects_unknown_or_invalid_schema_versions(version: Any) -> None:
    """Never reinterpret a future, missing, or incorrectly typed schema."""
    payload = _config().to_dict()
    payload["schema_version"] = version
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


def test_config_rejects_unknown_root_fields() -> None:
    """Prevent unnoticed configuration loss from misspelled or new fields."""
    payload = _config().to_dict()
    payload["enabledd"] = True
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


@pytest.mark.parametrize("value", [None, [], "{}", 1])
def test_config_requires_a_mapping_payload(value: Any) -> None:
    """Reject corrupt database payloads with a domain validation error."""
    with pytest.raises(ValidationError):
        Config.from_dict(value)


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None])
def test_config_requires_boolean_enabled(enabled: Any) -> None:
    """Do not let truthy text accidentally enable fabrication output."""
    payload = _config().to_dict()
    payload["enabled"] = enabled
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


def test_config_rejects_unknown_specification_fields() -> None:
    """Keep strict validation at the nested specification boundary."""
    payload = deepcopy(_config().to_dict())
    payload["specifications"][0]["target_ohmss"] = "90"
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


def test_config_rejects_unknown_layer_setting_fields() -> None:
    """A misspelled layer setting cannot silently drop a fabrication constraint."""
    payload = deepcopy(_config().to_dict())
    payload["specifications"][0]["layer_settings"][0]["ground_gapp_nm"] = 200000
    with pytest.raises(ValidationError):
        Config.from_dict(payload)


def test_enabled_draft_saves_but_report_requires_a_specification() -> None:
    """Saving unfinished intent cannot authorize a document with no rules."""
    config = Config(enabled=True)
    snapshot = BoardSnapshot(("F.Cu", "B.Cu"), ())
    validate_config(config)
    assert Config.from_dict(config.to_dict()) == config
    with pytest.raises(ValidationError, match="requires at least one specification"):
        validate_review(config, analyze(config, snapshot))


@pytest.mark.parametrize("target", ["", "0", "-5", "NaN", "Infinity", "word"])
def test_impedance_target_must_be_positive_and_finite(target: str) -> None:
    """Reject values that cannot describe a physical impedance target."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(target_ohms=target)))


@pytest.mark.parametrize("width", [0, -1, 1.5, True])
def test_actual_trace_width_requires_a_positive_integer(width: Any) -> None:
    """Bad extracted widths fail closed rather than bypassing class matching."""
    trace = _trace("invalid-width", ((0, 0), (1000000, 0)), width_nm=width)
    with pytest.raises(ValidationError):
        analyze(_config(), _snapshot(trace))


@pytest.mark.parametrize("kind", ["differential", "differential_coplanar"])
@pytest.mark.parametrize("spacing", [None, 0, -1])
def test_differential_specification_requires_positive_pair_spacing(
    kind: str, spacing: Optional[int]
) -> None:
    """A coplanar ground gap cannot substitute for differential pair spacing."""
    with pytest.raises(ValidationError):
        validate_config(
            _config(_spec(kind=kind, spacing_nm=spacing, ground_gap_nm=300000))
        )


@pytest.mark.parametrize("kind", ["single_ended_coplanar", "differential_coplanar"])
@pytest.mark.parametrize("ground_gap", [None, 0, -1])
def test_coplanar_specification_requires_positive_ground_gap(
    kind: str, ground_gap: Optional[int]
) -> None:
    """Differential pair spacing cannot substitute for the coplanar ground gap."""
    with pytest.raises(ValidationError):
        validate_config(
            _config(_spec(kind=kind, spacing_nm=200000, ground_gap_nm=ground_gap))
        )


@pytest.mark.parametrize(
    "kind",
    ["single_ended", "differential", "single_ended_coplanar", "differential_coplanar"],
)
@pytest.mark.parametrize("ground_gap", [0, -1, 0.5, True])
def test_supplied_ground_gap_requires_positive_integer_nanometres(
    kind: str, ground_gap: Any
) -> None:
    """Reject an invalid ground-gap value even when the chosen kind does not need it."""
    with pytest.raises(ValidationError):
        validate_config(
            _config(_spec(kind=kind, spacing_nm=200000, ground_gap_nm=ground_gap))
        )


def test_single_ended_coplanar_requires_ground_gap_without_pair_spacing() -> None:
    """A single signal trace has a coplanar ground gap and no differential gap."""
    config = _config(_spec(kind="single_ended_coplanar", ground_gap_nm=300000))
    validate_config(config)
    assert Config.from_dict(config.to_dict()) == config
    assert config.specifications[0].layer_settings[0].spacing_nm is None


def test_differential_coplanar_round_trip_keeps_two_independent_gaps() -> None:
    """Store pair spacing and ground gap separately when both are required."""
    config = _config(
        _spec(kind="differential_coplanar", spacing_nm=180000, ground_gap_nm=300000)
    )
    payload = config.to_dict()
    record = payload["specifications"][0]["layer_settings"][0]
    assert record["spacing_nm"] == 180000
    assert record["ground_gap_nm"] == 300000
    assert Config.from_dict(payload) == config


def test_unknown_impedance_kind_is_invalid() -> None:
    """Do not coerce an unknown transmission-line model to a known one."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(kind="mystery")))


def test_duplicate_specification_ids_are_invalid() -> None:
    """Stable review identity requires unique specification identifiers."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(), _spec()))


def test_duplicate_signal_layer_settings_are_invalid() -> None:
    """Each signal layer has exactly one set of reviewed fabrication settings."""
    settings = LayerSettings("F.Cu", ("B.Cu",))
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(layer_settings=(settings, settings))))


def test_signal_layer_must_exist_on_board() -> None:
    """Catch a removed signal layer before matching geometry."""
    config = _config(_spec(layer_settings=(LayerSettings("In1.Cu", ("B.Cu",)),)))
    with pytest.raises(ValidationError):
        validate_config(config, ("F.Cu", "B.Cu"))


def test_reference_layer_must_exist_on_board() -> None:
    """Do not export a reference-plane label absent from the live board."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(reference_layers=("In2.Cu",))), ("F.Cu", "B.Cu"))


def test_specification_requires_reference_layer() -> None:
    """Require the reference layer requested by the manufacturing template."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(reference_layers=())))


def test_reference_layer_cannot_be_its_own_signal_layer() -> None:
    """A transmission line cannot use its signal copper as its reference plane."""
    with pytest.raises(ValidationError):
        validate_config(_config(_spec(reference_layers=("F.Cu",))), ("F.Cu", "B.Cu"))


def test_class_matching_keeps_all_actual_widths_and_excludes_nonmembers() -> None:
    """Class intent includes even one-nanometre width changes, never unrelated copper."""
    candidates = (
        _trace("wanted", ((0, 0), (1000000, 0))),
        _trace("narrow", ((0, 1000000), (1000000, 1000000)), width_nm=149999),
        _trace("wide", ((0, 2000000), (1000000, 2000000)), width_nm=150001),
        _trace("back", ((0, 3000000), (1000000, 3000000)), layer="B.Cu"),
        _trace("other-net", ((0, 4000000), (1000000, 4000000)), net="ORDINARY"),
    )
    result = analyze(_config(_spec(excluded_layers=("B.Cu",))), _snapshot(*candidates))
    assert {trace.trace_id for row in result.sections for trace in row.traces} == {
        "wanted",
        "narrow",
        "wide",
    }


def test_layer_transitions_keep_actual_widths_in_separate_rows() -> None:
    """A class crossing the board produces rows for each actual layer and width."""
    config = _config(
        _spec(
            layer_settings=(
                LayerSettings("F.Cu", ("B.Cu",)),
                LayerSettings("B.Cu", ("F.Cu",)),
            )
        )
    )
    traces = (
        _trace("front", ((0, 0), (1000000, 0))),
        _trace("back", ((0, 0), (1000000, 0)), layer="B.Cu", width_nm=200000),
        _trace("front-neck", ((1000000, 0), (2000000, 0)), width_nm=200000),
        _trace("back-neck", ((1000000, 0), (2000000, 0)), layer="B.Cu"),
    )
    result = analyze(config, _snapshot(*traces))
    assert {(row.layer, row.width_nm) for row in result.sections} == {
        ("F.Cu", 150000),
        ("F.Cu", 200000),
        ("B.Cu", 150000),
        ("B.Cu", 200000),
    }
    assert {trace.trace_id for row in result.sections for trace in row.traces} == {
        trace.trace_id for trace in traces
    }


def test_connected_lines_and_sampled_arc_form_one_section() -> None:
    """Connected same-net geometry stays together through bends and arcs."""
    traces = (
        _trace("line", ((0, 0), (1000000, 0))),
        _trace("arc", ((1000000, 0), (1500000, 500000), (1000000, 1000000))),
        _trace("bend", ((1000000, 1000000), (2000000, 1000000))),
    )
    result = analyze(_config(), _snapshot(*traces))
    assert len(result.sections) == 1
    assert {trace.trace_id for trace in result.sections[0].traces} == {
        "line",
        "arc",
        "bend",
    }
    assert result.sections[0].bounds == (-75000, -75000, 2075000, 1075000)


def test_disconnected_same_net_routes_are_separate_sections() -> None:
    """Separate board regions produce separately reviewable document rows."""
    first = _trace("a", ((0, 0), (1000000, 0)))
    second = _trace("b", ((3000000, 0), (4000000, 0)))
    result = analyze(_config(), _snapshot(first, second))
    assert len(result.sections) == 2
    assert {section.net_names for section in result.sections} == {("CLK",)}


def test_touching_different_nets_are_never_connected() -> None:
    """Coincident endpoints do not establish electrical net identity."""
    first = _trace("a", ((0, 0), (1000000, 0)), net="FIRST")
    second = _trace("b", ((1000000, 0), (2000000, 0)), net="SECOND")
    result = analyze(_config(), _snapshot(first, second))
    assert len(result.sections) == 2
    assert {section.net_names for section in result.sections} == {
        ("FIRST",),
        ("SECOND",),
    }


def test_crossing_routes_do_not_join_at_midpoints() -> None:
    """A geometric crossing without shared endpoints is not connectivity."""
    horizontal = _trace("a", ((-1000000, 0), (1000000, 0)))
    vertical = _trace("b", ((0, -1000000), (0, 1000000)))
    result = analyze(_config(), _snapshot(horizontal, vertical))
    assert len(result.sections) == 2


def test_branched_route_emits_review_warning() -> None:
    """A shared endpoint with three arms requires explicit user attention."""
    traces = (
        _trace("a", ((0, 0), (1000000, 0))),
        _trace("b", ((0, 0), (-1000000, 0))),
        _trace("c", ((0, 0), (0, 1000000))),
    )
    result = analyze(_config(), _snapshot(*traces))
    assert result.warnings
    assert any("branch" in str(warning).lower() for warning in result.warnings)


def test_ids_digest_and_order_are_independent_of_track_enumeration() -> None:
    """KiCad container ordering must not invalidate a reviewed board."""
    traces = (
        _trace("b", ((1000000, 0), (2000000, 0))),
        _trace("a", ((0, 0), (1000000, 0))),
        _trace("c", ((10000000, 0), (11000000, 0))),
    )
    first = analyze(_config(), _snapshot(*traces))
    second = analyze(_config(), _snapshot(*reversed(traces)))
    assert first == second


def test_geometry_change_invalidates_existing_review() -> None:
    """A modified route must be reviewed again even if its UUID survives."""
    trace = _trace("a", ((0, 0), (1000000, 0)))
    original = analyze(_config(), _snapshot(trace))
    reviewed = replace(
        _config(),
        reviewed_digest=original.digest,
        included_section_ids=tuple(section.section_id for section in original.sections),
    )
    validate_review(reviewed, original)
    changed = analyze(
        reviewed, _snapshot(replace(trace, points=((0, 0), (2000000, 0))))
    )
    assert changed.digest != original.digest
    with pytest.raises(ValidationError):
        validate_review(reviewed, changed)


def test_board_context_change_invalidates_existing_review() -> None:
    """Changes to surrounding copper or stackup cannot reuse a stale preview."""
    trace = _trace("a", ((0, 0), (1000000, 0)))
    original = analyze(_config(), _snapshot(trace))
    changed = analyze(_config(), _snapshot(trace, context_digest="new-board-context"))
    assert original.digest != changed.digest


def test_review_state_does_not_change_analysis_digest() -> None:
    """Saving review results must not itself make that review stale."""
    board = _snapshot(_trace("a", ((0, 0), (1000000, 0))))
    original = analyze(_config(), board)
    reviewed = replace(
        _config(),
        reviewed_digest=original.digest,
        included_section_ids=(original.sections[0].section_id,),
    )
    assert analyze(reviewed, board).digest == original.digest


def test_review_requires_included_sections() -> None:
    """A current scan alone does not authorize an empty manufacturing form."""
    result = analyze(_config(), _snapshot(_trace("a", ((0, 0), (1000000, 0)))))
    with pytest.raises(ValidationError):
        validate_review(replace(_config(), reviewed_digest=result.digest), result)


def test_review_rejects_unknown_section_ids() -> None:
    """Stored selection corruption must not silently omit intended geometry."""
    result = analyze(_config(), _snapshot(_trace("a", ((0, 0), (1000000, 0)))))
    config = replace(
        _config(), reviewed_digest=result.digest, included_section_ids=("missing",)
    )
    with pytest.raises(ValidationError):
        validate_review(config, result)


def test_review_requires_a_selected_section_for_each_specification() -> None:
    """A specification with no included class routes cannot silently disappear."""
    config = _config(_spec(), _spec("other", net_class="Other"))
    result = analyze(config, _snapshot(_trace("a", ((0, 0), (1000000, 0)))))
    reviewed = replace(
        config,
        reviewed_digest=result.digest,
        included_section_ids=tuple(section.section_id for section in result.sections),
    )
    with pytest.raises(ValidationError):
        validate_review(reviewed, result)


def test_native_differential_pair_automatically_shares_one_row() -> None:
    """A verified native pair shares one viewport without manual grouping."""
    config = _config(_spec(kind="differential", target_ohms="90", spacing_nm=200000))
    traces = (
        _trace("positive", ((0, 0), (1000000, 0)), net="USB+"),
        _trace("negative", ((0, 350000), (1000000, 350000)), net="USB-"),
    )
    board = replace(_snapshot(*traces), differential_pairs=(("USB+", "USB-"),))
    combined = analyze(config, board).sections
    assert len(combined) == 1
    assert set(combined[0].net_names) == {"USB+", "USB-"}
    assert {trace.trace_id for trace in combined[0].traces} == {"positive", "negative"}
    assert combined[0].bounds == (-75000, -75000, 1075000, 425000)


def test_reversed_centerline_preserves_digest_and_section_identity() -> None:
    """Changing a segment's stored direction leaves the reviewed geometry intact."""
    trace = _trace("arc", ((0, 0), (1000000, 1000000), (2000000, 0)))
    original = analyze(_config(), _snapshot(trace))
    reversed_result = analyze(
        _config(), _snapshot(replace(trace, points=tuple(reversed(trace.points))))
    )
    assert reversed_result.digest == original.digest
    assert reversed_result.sections[0].section_id == original.sections[0].section_id
    assert reversed_result.sections[0].bounds == original.sections[0].bounds


def test_odd_width_bounds_round_outward() -> None:
    """Retain every copper edge when the half-width is a fractional nanometre."""
    config = _config(_spec())
    result = analyze(
        config, _snapshot(_trace("a", ((0, 0), (1000000, 0)), width_nm=150001))
    )
    assert result.sections[0].bounds == (-75001, -75001, 1075001, 75001)


def test_zero_length_track_has_bounded_section_and_warning() -> None:
    """Degenerate KiCad geometry remains visible in review without a crash."""
    result = analyze(_config(), _snapshot(_trace("a", ((0, 0), (0, 0)))))
    assert result.sections[0].bounds == (-75000, -75000, 75000, 75000)
    assert any("zero-length" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "changes",
    [
        {"trace_id": ""},
        {"layer": "In1.Cu"},
        {"width_nm": 0},
        {"width_nm": True},
        {"points": ((0, 0),)},
        {"points": ((0.5, 0), (100, 0))},
        {"points": ((True, 0), (100, 0))},
    ],
)
def test_malformed_extracted_geometry_is_rejected(changes: dict[str, Any]) -> None:
    """Adapter mistakes fail before producing a plausible but invalid form."""
    trace = replace(_trace("a", ((0, 0), (1000000, 0))), **changes)
    with pytest.raises(ValidationError):
        analyze(_config(), _snapshot(trace))


def test_duplicate_extracted_trace_ids_are_rejected() -> None:
    """Duplicated identities must not collapse distinct selected geometry."""
    first = _trace("same", ((0, 0), (1000000, 0)))
    second = _trace("same", ((3000000, 0), (4000000, 0)))
    with pytest.raises(ValidationError):
        analyze(_config(), _snapshot(first, second))


def test_nested_layer_list_is_rejected_with_domain_error() -> None:
    """Malformed layer entries must not escape as unhashable-type failures."""
    snapshot = BoardSnapshot((["F.Cu"], "B.Cu"), ())
    with pytest.raises(ValidationError):
        analyze(_config(), snapshot)


@pytest.mark.parametrize("text", ["1e999999", "1e-999999", "9" * 65])
def test_extreme_decimal_input_is_bounded(text: str) -> None:
    """Avoid unbounded conversion work on malformed persisted numeric text."""
    with pytest.raises(ValidationError):
        parse_length(text)


def test_differential_review_requires_native_pair_identity_not_manual_grouping() -> (
    None
):
    """Similar trace names are insufficient, but native identity groups both mates automatically."""
    config = _config(_spec(kind="differential", spacing_nm=200000))
    board = _snapshot(
        _trace("plus", ((0, 0), (1000000, 0)), net="USB+"),
        _trace("minus", ((0, 350000), (1000000, 350000)), net="USB-"),
    )
    with pytest.raises(ValidationError, match="native differential mate"):
        analyze(config, board)
    board = replace(board, differential_pairs=(("USB+", "USB-"),))
    grouped = analyze(config, board)
    assert len(grouped.sections) == 1
    validate_review(
        replace(
            config,
            reviewed_digest=grouped.digest,
            included_section_ids=tuple(
                section.section_id for section in grouped.sections
            ),
        ),
        grouped,
    )


def test_differential_review_rejects_an_unpaired_third_class_member() -> None:
    """Pairing two class members cannot silently omit a third unpaired member."""
    board = replace(
        _snapshot(
            *(
                _trace(
                    str(index),
                    ((0, index * 350000), (1000000, index * 350000)),
                    net=f"NET{index}",
                )
                for index in range(3)
            )
        ),
        differential_pairs=(("NET0", "NET1"),),
    )
    with pytest.raises(ValidationError, match="native differential mate"):
        analyze(_config(_spec(kind="differential", spacing_nm=200000)), board)


def test_differential_review_rejects_unrouted_pair_member() -> None:
    """No-net copper cannot substitute for a missing routed differential mate."""
    board = replace(
        _snapshot(
            _trace("plus", ((0, 0), (1000000, 0)), net="USB+"),
            _trace("unassigned", ((0, 350000), (1000000, 350000)), net=""),
        ),
        differential_pairs=(("USB+", "USB-"),),
    )
    with pytest.raises(ValidationError, match="mate|pair"):
        analyze(_config(_spec(kind="differential", spacing_nm=200000)), board)


def test_disabled_review_does_not_require_an_impedance_document() -> None:
    """Retained disabled settings do not block ordinary Gerber generation."""
    config = replace(_config(), enabled=False)
    analysis = analyze(config, _snapshot())
    validate_review(config, analysis)


def _route_points(span_nm: int, shape: str) -> tuple[tuple[int, int], ...]:
    """Describe routes with long straight, turning, and sampled-arc geometry."""
    if shape == "horizontal":
        return ((0, 0), (span_nm, 0))
    if shape == "vertical":
        return ((0, 0), (0, span_nm))
    if shape == "diagonal":
        return ((0, 0), (span_nm, span_nm))
    if shape == "meander":
        third = span_nm // 3
        return (
            (0, 0),
            (0, third),
            (third, third),
            (third, 0),
            (2 * third, 0),
            (2 * third, third),
            (span_nm, third),
        )
    if shape == "arc":
        radius = span_nm // 2
        return tuple(
            (
                radius + round(radius * cos(index * pi / 18)),
                round(radius * sin(index * pi / 18)),
            )
            for index in range(19)
        )
    raise AssertionError(f"Unknown test route shape: {shape}")


@pytest.mark.parametrize("span_nm", [1000000, 100000000, 150000000, 200000000])
@pytest.mark.parametrize(
    "shape", ["horizontal", "vertical", "diagonal", "meander", "arc"]
)
def test_connected_route_stays_complete_at_every_length_and_shape(
    span_nm: int, shape: str
) -> None:
    """Each connected route contributes one complete row regardless of its extent."""
    points = _route_points(span_nm, shape)
    trace = _trace("complete-route", points, width_nm=100000)
    config = _config(_spec())
    result = analyze(config, _snapshot(trace))
    assert len(result.sections) == 1
    section = result.sections[0]
    assert section.traces == (trace,)
    assert section.traces[0].points == points
    assert section.bounds == (
        min(point[0] for point in points) - 50000,
        min(point[1] for point in points) - 50000,
        max(point[0] for point in points) + 50000,
        max(point[1] for point in points) + 50000,
    )


def test_thin_two_hundred_mm_route_has_one_full_board_extent() -> None:
    """A 0.1 mm trace spanning 200 mm remains an intact document section."""
    trace = _trace("long-thin", ((0, 0), (200000000, 0)), width_nm=100000)
    config = _config(_spec())
    result = analyze(config, _snapshot(trace))
    assert len(result.sections) == 1
    assert result.sections[0].bounds == (-50000, -50000, 200050000, 50000)
    assert result.sections[0].traces == (trace,)


def test_long_connected_chain_keeps_every_segment_in_one_section() -> None:
    """A routed chain is documented in full across all its constituent track UUIDs."""
    traces = tuple(
        _trace(
            f"segment-{index:02d}",
            ((index * 5000000, 0), ((index + 1) * 5000000, 0)),
            width_nm=100000,
        )
        for index in range(40)
    )
    config = _config(_spec())
    result = analyze(config, _snapshot(*traces))
    assert len(result.sections) == 1
    assert result.sections[0].traces == traces
    assert result.sections[0].bounds == (-50000, -50000, 200050000, 50000)


@pytest.mark.parametrize("span_nm", [1, 100000000, 200000000, 1000000000000])
def test_disconnected_route_count_is_independent_of_physical_length(
    span_nm: int,
) -> None:
    """Only connectivity determines the number of candidates, even for huge extents."""
    traces = tuple(
        _trace(
            str(index),
            ((0, index * 1000000), (span_nm, index * 1000000)),
            width_nm=100000,
        )
        for index in range(3)
    )
    config = _config(_spec())
    result = analyze(config, _snapshot(*traces))
    assert len(result.sections) == 3
    assert {section.traces for section in result.sections} == {
        (trace,) for trace in traces
    }


def test_complete_long_differential_pair_groups_into_one_row() -> None:
    """Native automatic pairing retains the full extents of both 200 mm nets."""
    config = _config(
        _spec(
            kind="differential",
            spacing_nm=200000,
        )
    )
    traces = (
        _trace("positive", ((0, 0), (200000000, 0)), width_nm=100000, net="USB+"),
        _trace(
            "negative", ((0, 300000), (200000000, 300000)), width_nm=100000, net="USB-"
        ),
    )
    board = replace(_snapshot(*traces), differential_pairs=(("USB+", "USB-"),))
    grouped = analyze(config, board)
    assert len(grouped.sections) == 1
    pair = grouped.sections[0]
    assert set(pair.traces) == set(traces)
    assert set(pair.net_names) == {"USB+", "USB-"}
    assert pair.bounds == (-50000, -50000, 200050000, 350000)
    validate_review(
        replace(
            config,
            reviewed_digest=grouped.digest,
            included_section_ids=(pair.section_id,),
        ),
        grouped,
    )


def test_distant_single_ended_routes_remain_independent_rows() -> None:
    """Separate sections stay individually reviewable even in one net class."""
    traces = (
        _trace("near", ((0, 0), (100000000, 0)), width_nm=100000),
        _trace(
            "far", ((2000000000, 500000000), (2200000000, 500000000)), width_nm=100000
        ),
    )
    result = analyze(_config(), _snapshot(*traces))
    assert len(result.sections) == 2
    assert {row.traces for row in result.sections} == {(trace,) for trace in traces}


def test_long_route_analysis_is_stable_across_input_order() -> None:
    """Container ordering cannot change complete-route IDs or persisted review references."""
    config = _config(_spec())
    traces = (
        _trace("first", ((0, 0), (100000000, 0)), width_nm=100000),
        _trace("second", ((100000000, 0), (200000000, 0)), width_nm=100000),
    )
    first = analyze(config, _snapshot(*traces))
    second = analyze(config, _snapshot(*reversed(traces)))
    assert first == second
    assert len(first.sections) == 1
    assert first.sections[0].traces == traces


def test_candidate_count_limit_allows_one_thousand_tiny_components() -> None:
    """The resource bound counts actual disconnected candidates without using length."""
    traces = tuple(
        _trace(str(index), ((index * 3, 0), (index * 3 + 1, 0)), width_nm=1)
        for index in range(1000)
    )
    config = _config(_spec())
    result = analyze(config, _snapshot(*traces))
    assert len(result.sections) == 1000


def test_candidate_count_limit_rejects_one_thousand_and_one_components() -> None:
    """Too many distinct sections produce an actionable error before rendering."""
    traces = tuple(
        _trace(str(index), ((index * 3, 0), (index * 3 + 1, 0)), width_nm=1)
        for index in range(1001)
    )
    config = _config(_spec())
    with pytest.raises(ValidationError, match="(?i)(1000|sections|limit|many)"):
        analyze(config, _snapshot(*traces))


def test_one_huge_route_does_not_hit_candidate_count_limit() -> None:
    """An enormous physical extent is still exactly one connected route candidate."""
    trace = _trace(
        "huge",
        ((-1000000000000, -1000000000000), (1000000000000, 1000000000000)),
        width_nm=1,
    )
    config = _config(_spec())
    result = analyze(config, _snapshot(trace))
    assert len(result.sections) == 1
    assert result.sections[0].traces == (trace,)
    assert result.sections[0].bounds == (
        -1000000000001,
        -1000000000001,
        1000000000001,
        1000000000001,
    )
