"""Tests for BOM estimator presentation/view helpers."""

import pytest

from bom_estimation.assembly_mode import (  # pylint: disable=import-error
    AssemblyModeDecision,
)
from bom_estimation.pricing import (  # pylint: disable=import-error
    BomEstimateSummary,
    calculate_part_bom_cost,
)
from bom_estimation.view import (  # pylint: disable=import-error
    ASSEMBLY_CAPABILITIES_SOURCE,
    COMPONENT_MATCHING_SOURCE,
    STANDARD_ONLY_SOURCE,
    assembly_mode_details_button_label,
    build_affected_part_rows,
    build_assembly_mode_reasons,
    build_bom_estimate_view_model,
    format_assembly_mode_status,
    format_bom_estimate_summary,
    format_part_bom_price_label,
    prepare_bom_price_labels,
    standard_signal_reasons,
)


def _decision(**overrides):
    """Return a decision with Economic-compatible defaults."""
    values = {
        "board_count": 5,
        "top_refs": frozenset({"R1"}),
        "smt_populated_side_count": 1,
    }
    values.update(overrides)
    return AssemblyModeDecision(**values)


def _summary(**overrides):
    """Return a representative estimate summary."""
    values = {
        "total_cost": 25.50,
        "cost_per_board": 12.75,
        "missing_prices": 0,
        "component_cost": 10.00,
        "fixed_cost": 8.00,
        "extended_cost": 3.00,
        "economic_setup_cost": 8.00,
        "standard_setup_cost": 0.00,
        "stencil_cost": 1.50,
        "tht_setup_cost": 0.00,
        "variable_assembly_cost": 7.50,
        "standard_part_surcharge_cost": 0.00,
        "smt_joint_count": 100,
        "tht_joint_count": 0,
    }
    values.update(overrides)
    return BomEstimateSummary(**values)


@pytest.mark.parametrize(
    ("overrides", "status", "button"),
    [
        (
            {"board_count": 51, "economic_only_refs": {"R1"}},
            "Mode conflict · 1 selected part is Economic Only",
            "Review conflict…",
        ),
        (
            {
                "board_count": 51,
                "top_refs": {"R1", "R2"},
                "economic_only_refs": {"R1", "R2"},
            },
            "Mode conflict · 2 selected parts are Economic Only",
            "Review conflict…",
        ),
        (
            {"manual_enabled": True, "classification_missing_refs": {"R1"}},
            "Estimated pricing mode: Standard · 1 part classification missing",
            "Why Standard…",
        ),
        (
            {
                "manual_enabled": True,
                "top_refs": {"R1", "R2"},
                "classification_missing_refs": {"R1", "R2"},
            },
            "Estimated pricing mode: Standard · 2 part classifications missing",
            "Why Standard…",
        ),
        (
            {"manual_enabled": True},
            "Estimated pricing mode: Standard",
            "Why Standard…",
        ),
        ({}, "Estimated pricing mode: Economic", None),
        (
            {"classification_missing_refs": {"R1"}},
            "Assembly mode unknown · 1 part classification missing",
            "Review parts…",
        ),
        (
            {"top_refs": set(), "smt_populated_side_count": 0},
            "Assembly mode: N/A",
            None,
        ),
    ],
    ids=(
        "conflict-one",
        "conflict-many",
        "standard-missing-one",
        "standard-missing-many",
        "standard",
        "economic",
        "unknown",
        "not-applicable",
    ),
)
def test_assembly_mode_status_and_action(overrides, status, button):
    """Each mode state has terse status text and the most useful action."""
    decision = _decision(**overrides)

    assert format_assembly_mode_status(decision) == status
    assert assembly_mode_details_button_label(decision) == button


@pytest.mark.parametrize(
    ("summary_overrides", "boards", "decision_overrides", "expected"),
    [
        (
            {},
            2,
            {},
            (
                "BOM Estimate (2 boards): Estimated pricing mode: Economic | "
                "Total $25.50 | Per board $12.75 | Missing prices 0",
                "Direct BOM Cost: $10.00 | Fixed $11.00 "
                "(extended: $3.00, setup: $8.00, stencil: $1.50, tht: $0.00) | "
                "Assembly $7.50 (smt: 100 joints, tht: 0 joints)",
            ),
        ),
        (
            {
                "total_cost": 35.00,
                "cost_per_board": 17.50,
                "missing_prices": 2,
                "economic_setup_cost": 0.00,
                "standard_setup_cost": 25.00,
                "stencil_cost": 7.80,
                "variable_assembly_cost": 12.20,
                "standard_part_surcharge_cost": 1.50,
                "smt_joint_count": 50,
                "tht_joint_count": 10,
            },
            1,
            {
                "board_count": 51,
                "manual_enabled": True,
                "standard_only_refs": {"R1"},
            },
            (
                "BOM Estimate (1 boards): Estimated pricing mode: Standard | "
                "Total $35.00 | Per board $17.50 | Missing prices 2",
                "Direct BOM Cost: $10.00 | Fixed $9.50 "
                "(standard service part fees: $1.50, setup: $25.00, "
                "stencil: $7.80, tht: $0.00) | Assembly $10.70 "
                "(smt: 50 joints, tht: 10 joints)",
            ),
        ),
        (
            {"total_cost": 99.00, "cost_per_board": 49.50, "missing_prices": 1},
            2,
            {"classification_missing_refs": {"R1"}},
            (
                "BOM Estimate (2 boards): Assembly mode unknown · "
                "1 part classification missing | Missing prices 1",
                "Direct BOM Cost: $10.00 | Mode-dependent assembly estimate unavailable",
            ),
        ),
        (
            {},
            4,
            {"top_refs": set(), "smt_populated_side_count": 0},
            (
                "BOM Estimate (4 boards): Assembly mode: N/A | Missing prices 0",
                "Direct BOM Cost: $10.00 | No selected parts for assembly",
            ),
        ),
    ],
    ids=("economic", "standard-terse", "unknown", "not-applicable"),
)
def test_format_bom_estimate_summary_scenarios(
    summary_overrides, boards, decision_overrides, expected
):
    """Summary output is exact for every available mode state."""
    assert (
        format_bom_estimate_summary(
            _summary(**summary_overrides),
            boards,
            _decision(**decision_overrides),
        )
        == expected
    )


def test_standard_signal_reasons_orders_labels_consistently():
    """Signal reason labels are ordered for stable display."""
    reasons = standard_signal_reasons(
        {
            "quantity_over_50": True,
            "manual_enabled": True,
            "multi_side_populated": True,
            "standard_part_present": True,
        }
    )
    assert reasons == ["manual", "qty>50", "standard part", "both sides populated"]


def test_standard_signal_reasons_ignores_inactive_flags():
    """No active signals yields no labels."""
    assert (
        standard_signal_reasons({"manual_enabled": False, "quantity_over_50": 0}) == []
    )


def test_jlcpcb_sources_are_official_pages():
    """Reason citations retain their user-facing labels and official URLs."""
    assert ASSEMBLY_CAPABILITIES_SOURCE == (
        "JLCPCB assembly capabilities",
        "https://jlcpcb.com/capabilities/pcb-assembly-capabilities",
    )
    assert STANDARD_ONLY_SOURCE == (
        "JLCPCB BOM and CPL matching guidance",
        "https://jlcpcb.com/help/article/common-bom-and-cpl-matching-issues-and-explanations",
    )
    assert COMPONENT_MATCHING_SOURCE == (
        "JLCPCB component matching guidelines",
        "https://jlcpcb.com/help/article/component-matching-guidelines-for-pcba-orders",
    )


def test_build_assembly_mode_reasons_orders_all_active_reasons_and_sources():
    """Details reasons use stable priority and cite the applicable JLCPCB pages."""
    decision = _decision(
        board_count=51,
        manual_enabled=True,
        standard_only_refs=frozenset({"R1", "R2"}),
        top_refs=frozenset({"R1", "R2", "C2", "U1", "U2"}),
        bottom_refs=frozenset({"C1"}),
        economic_only_refs=frozenset({"C1", "C2"}),
        classification_missing_refs=frozenset({"U1", "U2"}),
        smt_populated_side_count=2,
    )

    reasons = build_assembly_mode_reasons(decision)

    assert [reason["heading"] for reason in reasons] == [
        "Quantity above 50",
        "2 Standard-only parts",
        "Components on TOP and BOT",
        "Force Standard",
        "Compatibility conflict — 2 parts",
        "Assembly classification missing — 2 parts",
    ]
    assert [reason["sources"] for reason in reasons] == [
        [ASSEMBLY_CAPABILITIES_SOURCE],
        [STANDARD_ONLY_SOURCE, COMPONENT_MATCHING_SOURCE],
        [ASSEMBLY_CAPABILITIES_SOURCE, COMPONENT_MATCHING_SOURCE],
        [],
        [],
        [],
    ]
    assert reasons[-1]["body"] == (
        "No Economic/Standard classification was returned for these LCSC parts."
    )


def test_build_assembly_mode_reasons_uses_singular_counts():
    """Counted reason headings use terse singular wording for one part."""
    reasons = build_assembly_mode_reasons(
        _decision(
            standard_only_refs=frozenset({"R1"}),
            top_refs=frozenset({"R1", "C1", "U1"}),
            economic_only_refs=frozenset({"C1"}),
            classification_missing_refs=frozenset({"U1"}),
        ),
    )

    assert [reason["heading"] for reason in reasons] == [
        "1 Standard-only part",
        "Compatibility conflict — 1 part",
        "Assembly classification missing — 1 part",
    ]


def test_build_assembly_mode_reasons_handles_no_active_standard_reason():
    """Economic mode gets one concise fallback reason."""
    assert build_assembly_mode_reasons(_decision()) == [
        {
            "heading": "No active Standard reasons",
            "body": "No modeled reason currently selects Standard.",
            "remedy": "",
            "sources": [],
        }
    ]


def test_build_affected_part_rows_sorts_and_combines_relationships():
    """Affected rows sort naturally and combine every relevant relationship."""
    decision = _decision(
        standard_only_refs=frozenset({"R2", "R10"}),
        top_refs=frozenset({"R2", "R3", "R10"}),
        bottom_refs=frozenset({"U1"}),
        economic_only_refs=frozenset({"R2", "U1"}),
        classification_missing_refs=frozenset({"R2", "R3"}),
        smt_populated_side_count=2,
    )
    parts = [
        {"reference": "R10", "value": "10k", "lcsc": "C10"},
        {"reference": "U1", "value": "MCU", "lcsc": "C20"},
        {"reference": "R2", "value": "2k", "lcsc": "C2"},
    ]

    rows = build_affected_part_rows(decision, parts)

    fields = ("relationship", "reference", "value", "lcsc", "side")
    assert [tuple(row[field] for field in fields) for row in rows] == [
        (
            "Standard Only; Both-side context; Economic Only conflict; "
            "Classification missing",
            "R2",
            "2k",
            "C2",
            "TOP",
        ),
        ("Both-side context; Classification missing", "R3", "", "", "TOP"),
        ("Standard Only; Both-side context", "R10", "10k", "C10", "TOP"),
        ("Both-side context; Economic Only conflict", "U1", "MCU", "C20", "BOT"),
    ]


def test_calculate_part_bom_cost_uses_raw_component_price_only():
    """Per-part cost helper returns only direct component contribution."""
    part = {"lcsc": "C123", "exclude_from_bom": 0}
    details = {"price": "1-9:0.30,10-:0.20", "type": "Extended"}
    assert calculate_part_bom_cost(part, details, board_count=10) == 2.0


def test_format_part_bom_price_label_handles_missing_and_excluded_parts():
    """Excluded rows render empty per-part labels."""
    assert (
        format_part_bom_price_label(
            {"lcsc": "C1", "exclude_from_bom": 1}, {"price": "1-:0.10"}, board_count=5
        )
        == ""
    )


def test_build_bom_estimate_view_model_returns_summary_and_highlights():
    """View-model builder returns summary and trigger highlights for standard mode."""
    parts = [
        {
            "reference": "R1",
            "lcsc": "C100",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    view_model = build_bom_estimate_view_model(
        parts=parts,
        board_count=5,
        get_part_details=lambda _lcsc: {"price": "1-:1.00", "type": "Basic"},
        standard_context={
            "board_standard": True,
            "smt_populated_sides": 1,
            "signals": {"standard_part_present": True},
            "trigger_references": {"R1"},
        },
    )

    assert view_model["summary"] is not None
    assert view_model["mode"] == "Standard"
    assert view_model["reason_text"] == "standard part"
    assert view_model["highlight_refs"] == {"R1"}


def test_prepare_bom_price_labels_returns_reference_to_label_mapping():
    """Price-label helper returns expected per-reference mapping."""
    parts = [
        {"reference": "R1", "lcsc": "C123", "exclude_from_bom": 0},
        {"reference": "R2", "lcsc": "C456", "exclude_from_bom": 0},
    ]
    details_store = {
        "C123": {"price": "1-:0.10"},
        "C456": {"price": "5-:0.20"},
    }

    labels = prepare_bom_price_labels(
        parts, board_count=10, get_part_details=lambda lcsc: details_store.get(lcsc, {})
    )

    assert labels == {"R1": "$1.0000", "R2": "$2.0000"}
