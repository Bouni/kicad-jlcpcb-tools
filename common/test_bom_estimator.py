"""Tests for BOM estimator business logic."""

from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimation import (
    DEFAULT_PRICING,
    AssemblyPricing,
    build_bom_estimate_view_model,
    build_standard_mode_context,
    calculate_bom_estimate,
    format_part_bom_price_label,
    prepare_bom_price_labels,
)


def test_calculate_bom_estimate_missing_price_counts_unknown_lcsc_price():
    """Missing price bands increment diagnostics and keep component cost zero."""
    parts = [
        {
            "lcsc": "C404",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "", "type": "Basic"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    assert summary.component_cost == 0.0
    assert summary.missing_prices == 1


def test_calculate_bom_estimate_computes_fixed_cost_without_policy_kwargs():
    """Fixed costs are computed via the current pricing model inputs only."""
    parts = [
        {
            "lcsc": "C100",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=10,
        get_part_details=get_details,
    )

    assert summary.fixed_cost > 0
    assert summary.assembly_cost >= summary.fixed_cost
    assert summary.variable_assembly_cost > 0


def test_standard_fees_apply_for_standard_smt_part():
    """Standard fixed fees apply for standard parts even when assembly process is SMT."""
    parts = [
        {
            "lcsc": "CSTD1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=2,
        get_part_details=get_details,
        pricing=AssemblyPricing(
            standard_setup_fee=4.0,
            standard_stencil_fee=1.2,
            standard_part_fee=0.5,
        ),
        board_standard=True,
        smt_populated_sides=1,
    )

    # 4.0 setup + 1.2 stencil + 0.5 standard_part + 4*0.0016 smt joints
    expected_assembly = 4.0 + 1.2 + 0.5 + 4 * DEFAULT_PRICING.smt_per_joint_fee
    assert round(summary.component_cost, 3) == 2.000
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert summary.standard_part_count == 1


def test_standard_fees_are_orthogonal_to_tht_fees():
    """Standard and THT fixed-fee policies can apply together when both conditions are true."""
    parts = [
        {
            "lcsc": "CSTD2",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 1,
            "assembly_process": "THT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CSMT2",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_lcsc):
        return {"price": "1-:0.80", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=3,
        get_part_details=get_details,
        pricing=AssemblyPricing(
            standard_setup_fee=2.0,
            standard_stencil_fee=0.5,
            standard_part_fee=1.0,
        ),
        board_standard=True,
        smt_populated_sides=1,
    )

    # 3.5 tht_setup + 2.0 standard_setup + 0.5 stencil + 1.0 standard_part + 6*0.0157 + 3*0.0016
    expected_assembly = (
        DEFAULT_PRICING.tht_setup_fee
        + 2.0
        + 0.5
        + 1.0
        + 6 * DEFAULT_PRICING.tht_per_joint_fee
        + 3 * DEFAULT_PRICING.smt_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 4.800
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert summary.standard_part_count == 1


def test_standard_fees_do_not_apply_for_non_standard_parts():
    """Non-standard parts do not incur standard fixed fees."""
    parts = [
        {
            "lcsc": "CNONSTD",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:0.50", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=4,
        get_part_details=get_details,
        pricing=AssemblyPricing(
            standard_setup_fee=99.0,
            standard_part_fee=99.0,
        ),
        board_standard=False,
    )

    P = DEFAULT_PRICING
    # 8.0 setup + 1.5 stencil + 4*0.0016 smt joints
    expected_assembly = (
        P.economic_setup_fee + P.economic_stencil_fee + 4 * P.smt_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.000
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert summary.standard_part_count == 0


def test_standard_per_side_base_fees_and_all_smt_surcharge_apply():
    """Standard base fees apply per populated SMT side and surcharge all SMT LCSC values."""
    parts = [
        {
            "lcsc": "CSTD3",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CBASIC3",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=1,
        get_part_details=get_details,
        board_standard=True,
        smt_populated_sides=2,
    )

    P = DEFAULT_PRICING
    # (25+7.8)*2 setup+stencil + 2*1.5 standard_part + 3*0.0016 smt joints
    expected_assembly = (
        (P.standard_setup_fee + P.standard_stencil_fee) * 2
        + 2 * P.standard_part_fee
        + 3 * P.smt_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.000
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)


def test_cost_breakdown_fields_sum_to_total_and_per_board():
    """Breakdown buckets should reconcile with assembly and total costs."""
    parts = [
        {
            "lcsc": "C101",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Extended"}

    summary = calculate_bom_estimate(
        parts,
        board_count=5,
        get_part_details=get_details,
        board_standard=False,
    )

    P = DEFAULT_PRICING
    assert round(summary.component_cost, 3) == 5.000
    assert round(summary.fixed_cost, 3) == round(
        P.economic_setup_fee + P.economic_stencil_fee, 3
    )
    assert round(summary.economic_setup_cost, 3) == round(P.economic_setup_fee, 3)
    assert round(summary.stencil_cost, 3) == round(P.economic_stencil_fee, 3)
    assert round(summary.tht_setup_cost, 3) == 0.000
    assert round(summary.standard_setup_cost, 3) == 0.000
    assert round(summary.extended_cost, 3) == round(P.extended_part_fee, 3)
    assert round(summary.standard_part_surcharge_cost, 3) == 0.000
    assert round(summary.variable_assembly_cost, 3) == round(
        10 * P.smt_per_joint_fee, 3
    )
    assert summary.smt_joint_count == 10
    assert summary.tht_joint_count == 0
    expected_assembly = (
        P.economic_setup_fee
        + P.economic_stencil_fee
        + P.extended_part_fee
        + 10 * P.smt_per_joint_fee
    )
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert round(summary.total_cost, 3) == round(5.0 + expected_assembly, 3)
    assert round(summary.cost_per_board, 3) == round((5.0 + expected_assembly) / 5, 3)


def test_standard_surcharge_and_joint_counts_are_reported_separately():
    """UI-facing summary fields expose joint counts and standard surcharge cleanly."""
    parts = [
        {
            "lcsc": "CSTDUI",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CTHTUI",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 1,
            "assembly_process": "Wave soldering",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=2,
        get_part_details=get_details,
        board_standard=True,
        smt_populated_sides=1,
    )

    assert summary.smt_joint_count == 4
    assert summary.tht_joint_count == 2
    assert round(summary.standard_part_surcharge_cost, 3) == round(
        DEFAULT_PRICING.standard_part_fee, 3
    )


def test_dnp_parts_are_excluded_from_bom_estimator_counts():
    """DNP rows should not contribute to component or assembly totals."""
    parts = [
        {
            "lcsc": "C401",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "C402",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": true}',
        },
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=5,
        get_part_details=get_details,
        board_standard=False,
    )

    assert round(summary.component_cost, 3) == 5.000
    assert summary.standard_part_count == 0


def test_build_bom_estimate_view_model_handles_empty_parts():
    """Empty part lists produce the expected empty-state label."""
    view_model = build_bom_estimate_view_model(
        parts=[],
        board_count=5,
        get_part_details=lambda _lcsc: {},
        standard_context={"board_standard": False},
    )

    assert view_model["summary"] is None
    assert view_model["highlight_refs"] == set()
    assert view_model["summary_label"] == "BOM Estimate (5 boards): no parts"


def test_build_bom_estimate_view_model_handles_no_assigned_bom_parts():
    """Parts without assigned BOM rows produce the no-assigned state."""
    view_model = build_bom_estimate_view_model(
        parts=[
            {"reference": "R1", "lcsc": "", "exclude_from_bom": 0},
            {"reference": "R2", "lcsc": "C2", "exclude_from_bom": 1},
        ],
        board_count=10,
        get_part_details=lambda _lcsc: {},
        standard_context={"board_standard": False},
    )

    assert view_model["summary"] is None
    assert view_model["highlight_refs"] == set()
    assert (
        view_model["summary_label"] == "BOM Estimate (10 boards): no assigned BOM parts"
    )


def test_build_standard_mode_context_combines_policy_signals():
    """Standard mode turns on when any pure policy trigger is active."""
    context = build_standard_mode_context(
        manual_enabled=False,
        board_count=50,
        populated_refs={"R1", "R2"},
        populated_sides={"top", "bottom"},
        smt_populated_sides={"top"},
        standard_part_refs={"R1"},
    )

    assert context["board_standard"] is True
    assert context["signals"] == {
        "manual_enabled": False,
        "qty_50_plus": True,
        "standard_part_present": True,
        "multi_side_populated": True,
    }
    assert context["smt_populated_sides"] == 1


def test_build_standard_mode_context_highlights_standard_parts_and_multiside_refs():
    """Highlight refs include standard parts and all populated refs for multi-side boards."""
    context = build_standard_mode_context(
        manual_enabled=False,
        board_count=5,
        populated_refs={"R1", "R2", "R3"},
        populated_sides={"top", "bottom"},
        smt_populated_sides={"top", "bottom"},
        standard_part_refs={"R2"},
    )

    assert context["trigger_references"] == {"R1", "R2", "R3"}

    single_side_context = build_standard_mode_context(
        manual_enabled=False,
        board_count=5,
        populated_refs={"R1", "R2", "R3"},
        populated_sides={"top"},
        smt_populated_sides={"top"},
        standard_part_refs={"R2"},
    )

    assert single_side_context["trigger_references"] == {"R2"}
    assert (
        format_part_bom_price_label(
            {"lcsc": "C1", "exclude_from_bom": 0},
            {"price": ""},
            board_count=5,
        )
        == "N/A"
    )
    assert (
        format_part_bom_price_label(
            {"lcsc": "", "exclude_from_bom": 0},
            {"price": "1-:0.10"},
            board_count=5,
        )
        == ""
    )


def test_prepare_bom_price_labels_deduplicates_detail_fetches():
    """Parts sharing an LCSC code cause only one get_part_details call."""
    call_count = [0]

    def counting_get(lcsc):
        call_count[0] += 1
        return {"price": "1-:0.50"}

    parts = [
        {"reference": "R1", "lcsc": "C1", "exclude_from_bom": 0},
        {"reference": "R2", "lcsc": "C1", "exclude_from_bom": 0},
    ]
    prepare_bom_price_labels(parts, board_count=5, get_part_details=counting_get)

    assert call_count[0] == 1


def test_prepare_bom_price_labels_uses_aggregate_lcsc_quantity_tiers():
    """Rows sharing an LCSC use tier selected from aggregated quantity."""
    parts = [
        {"reference": "R1", "lcsc": "C1", "exclude_from_bom": 0},
        {"reference": "R2", "lcsc": "C1", "exclude_from_bom": 0},
    ]

    labels = prepare_bom_price_labels(
        parts,
        board_count=5,
        get_part_details=lambda _lcsc: {"price": "1-9:1.00,10-:0.50"},
    )

    # Aggregate qty is 10 across both rows -> unit price 0.50, per-row label is 5 * 0.50
    assert labels == {"R1": "$2.5000", "R2": "$2.5000"}


def test_prepare_bom_price_labels_skips_excluded_and_unassigned_parts():
    """Excluded and LCSC-less parts produce empty labels, not missing keys."""
    parts = [
        {"reference": "R1", "lcsc": "C1", "exclude_from_bom": 1},
        {"reference": "R2", "lcsc": "", "exclude_from_bom": 0},
    ]
    labels = prepare_bom_price_labels(
        parts, board_count=5, get_part_details=lambda _: {"price": "1-:0.10"}
    )

    assert labels == {"R1": "", "R2": ""}


def test_prepare_bom_price_labels_skips_parts_without_reference():
    """Parts missing the reference key are silently omitted."""
    parts = [{"lcsc": "C1", "exclude_from_bom": 0}]
    labels = prepare_bom_price_labels(
        parts, board_count=5, get_part_details=lambda _: {"price": "1-:0.10"}
    )

    assert labels == {}
