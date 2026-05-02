"""Tests for BOM estimator business logic."""

from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimator import (
    DEFAULT_PRICING,
    AssemblyPricing,
    BomEstimateSummary,
    _collect_billable_bom_parts,
    _scan_assembly_state,
    build_bom_estimate_view_model,
    build_standard_mode_context,
    calculate_bom_estimate,
    calculate_part_bom_cost,
    fetch_assembly_processes,
    format_bom_estimate_summary,
    format_part_bom_price_label,
    get_assembly_flags,
    get_unit_price,
    is_tht_part,
    prepare_bom_price_labels,
    standard_signal_reasons,
)


def test_get_unit_price_quantity_tiers():
    """Quantity tier parser picks expected unit prices."""
    assert get_unit_price(1, "1-9:0.12,10-99:0.08,100-:0.05") == 0.12
    assert get_unit_price(10, "1-9:0.12,10-99:0.08,100-:0.05") == 0.08
    assert get_unit_price(250, "1-9:0.12,10-99:0.08,100-:0.05") == 0.05


def test_calculate_bom_estimate_smt_and_extended_once_per_lcsc():
    """SMD extended surcharge is charged once per unique extended LCSC."""
    parts = [
        {
            "lcsc": "C123",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "C123",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_):
        return {"price": "1-9:0.30,10-:0.20", "type": "Extended"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    P = DEFAULT_PRICING
    # $8 setup + $1.5 stencil + $3 extended + 10 smt joints
    expected_assembly = (
        P.economic_setup_fee
        + P.economic_stencil_fee
        + P.extended_part_fee
        + 10 * P.smt_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.000  # qty=10 at $0.20
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert round(summary.total_cost, 3) == round(2.0 + expected_assembly, 3)
    assert summary.missing_prices == 0
    assert summary.standard_part_count == 0


def test_calculate_bom_estimate_tht_setup_and_no_extended_surcharge_for_tht():
    """THT applies setup/joint fees and skips extended surcharge."""
    parts = [
        {
            "lcsc": "C777",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 1,
            "assembly_process": "Wave soldering",
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:0.50", "type": "Extended"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    P = DEFAULT_PRICING
    # 8.00 setup + 3.50 tht_setup + (10 * 0.0157) tht joints
    expected_assembly = (
        P.economic_setup_fee + P.tht_setup_fee + 10 * P.tht_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.500
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)
    assert round(summary.total_cost, 3) == round(2.5 + expected_assembly, 3)


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


def test_calculate_bom_estimate_accepts_policy_kwargs_and_reports_policy_cost():
    """Policy kwargs are accepted and included in fixed/policy cost totals."""
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
        order_handling_fee=2.5,
        panelization_per_board_fee=0.1,
        panelization_threshold_boards=5,
    )

    assert round(summary.policy_cost, 3) == 3.5
    assert summary.fixed_cost >= summary.policy_cost


class _FakeApi:
    def get_part_data(self, lcsc):
        if lcsc == "C1":
            return {
                "success": True,
                "data": {
                    "data": {
                        "assemblyProcess": "SMT",
                        "componentProductType": 2,
                    }
                },
            }
        return {"success": False}


def test_fetch_assembly_processes_uses_api_contract():
    """Assembly fetch helper maps success/failure API responses correctly."""
    result = fetch_assembly_processes(["C1", "C2"], api=_FakeApi())
    assert result == {
        "C1": {
            "assembly_process": "SMT",
            "component_product_type": 2,
            "is_standard_assembly": True,
        },
        "C2": {
            "assembly_process": "",
            "component_product_type": None,
            "is_standard_assembly": False,
        },
    }


def test_get_assembly_flags_handles_bad_json():
    """Malformed assembly flag JSON falls back to an empty dict."""
    assert get_assembly_flags({"assembly_flags": "not-json"}) == {}


def test_is_tht_part_uses_assembly_process_fallback():
    """THT detection falls back to assembly process text when has_tht is missing."""
    assert is_tht_part({"has_tht": None, "assembly_process": "Wave soldering"})
    assert not is_tht_part({"has_tht": None, "assembly_process": "SMT"})


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


def test_standard_mode_does_not_charge_extended_surcharge():
    """Standard mode should use per-SMT-part surcharge instead of Extended fee."""
    parts = [
        {
            "lcsc": "CEXT1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CBAS1",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(lcsc):
        return {
            "price": "1-:1.00",
            "type": "Extended" if lcsc == "CEXT1" else "Basic",
        }

    summary = calculate_bom_estimate(
        parts,
        board_count=1,
        get_part_details=get_details,
        board_standard=True,
        smt_populated_sides=1,
    )

    P = DEFAULT_PRICING
    expected_assembly = (
        P.standard_setup_fee
        + P.standard_stencil_fee
        + 2 * P.standard_part_fee
        + 3 * P.smt_per_joint_fee
    )
    assert round(summary.extended_cost, 3) == 0.000
    assert round(summary.standard_part_surcharge_cost, 3) == round(
        2 * P.standard_part_fee, 3
    )
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


def test_format_bom_estimate_summary_basic():
    """format_bom_estimate_summary produces expected display lines."""
    summary = BomEstimateSummary(
        total_cost=25.50,
        cost_per_board=12.75,
        missing_prices=0,
        component_cost=10.00,
        fixed_cost=8.00,
        extended_cost=3.00,
        economic_setup_cost=8.00,
        standard_setup_cost=0.00,
        stencil_cost=1.50,
        tht_setup_cost=0.00,
        variable_assembly_cost=7.50,
        standard_part_surcharge_cost=0.00,
        smt_joint_count=100,
        tht_joint_count=0,
    )

    overview, details = format_bom_estimate_summary(
        summary, board_count=2, mode="Economic", reason_text="none"
    )

    assert "2 boards" in overview
    assert "Mode Economic" in overview
    assert "Total $25.50" in overview
    assert "Per board $12.75" in overview
    assert "Missing prices 0" in overview

    assert "Direct BOM Cost: $10.00" in details
    assert "Fixed $11.00" in details  # 8.0 + 3.0
    assert "extended: $3.00" in details
    assert "setup: $8.00" in details
    assert "Assembly $7.50" in details
    assert "100 joints" in details


def test_format_bom_estimate_summary_with_standard_surcharge():
    """format_bom_estimate_summary shows standard surcharge in breakdown."""
    summary = BomEstimateSummary(
        total_cost=35.00,
        cost_per_board=17.50,
        missing_prices=2,
        component_cost=10.00,
        fixed_cost=8.00,
        extended_cost=3.00,
        economic_setup_cost=0.00,
        standard_setup_cost=25.00,
        stencil_cost=7.80,
        tht_setup_cost=0.00,
        variable_assembly_cost=12.20,
        standard_part_surcharge_cost=1.50,
        smt_joint_count=50,
        tht_joint_count=10,
    )
    overview, details = format_bom_estimate_summary(
        summary, board_count=1, mode="Standard", reason_text="standard parts"
    )

    assert "Mode Standard" in overview
    assert "Triggers standard parts" in overview
    assert "Missing prices 2" in overview

    assert "std-parts: $1.50" in details
    assert "extended:" not in details
    assert "Assembly $10.70" in details
    assert "setup: $25.00" in details
    assert "50 joints, tht: 10 joints" in details


def test_standard_signal_reasons_orders_labels_consistently():
    """standard_signal_reasons returns active labels in display order."""
    reasons = standard_signal_reasons(
        {
            "qty_50_plus": True,
            "manual_enabled": True,
            "multi_side_populated": True,
            "v_cut_drawings": False,
            "standard_part_present": True,
        }
    )

    assert reasons == [
        "manual",
        "qty≥50",
        "standard part",
        "both sides populated",
    ]


def test_standard_signal_reasons_ignores_inactive_flags():
    """standard_signal_reasons returns an empty list when no triggers are active."""
    assert standard_signal_reasons({"manual_enabled": False, "qty_50_plus": 0}) == []


def test_calculate_part_bom_cost_uses_raw_component_price_only():
    """Per-part BOM contribution uses only component pricing bands."""
    part = {"lcsc": "C123", "exclude_from_bom": 0}
    details = {"price": "1-9:0.30,10-:0.20", "type": "Extended"}

    assert calculate_part_bom_cost(part, details, board_count=10) == 2.0


def test_format_part_bom_price_label_handles_missing_and_excluded_parts():
    """Part BOM label helper preserves UI behavior for excluded and missing-price rows."""
    assert (
        format_part_bom_price_label(
            {"lcsc": "C1", "exclude_from_bom": 1},
            {"price": "1-:0.10"},
            board_count=5,
        )
        == ""
    )


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


def test_build_bom_estimate_view_model_returns_summary_and_highlights():
    """Populated BOM rows produce a formatted summary and conditional highlights."""
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
    assert "Mode Standard" in view_model["summary_label"]
    assert "Triggers standard part" in view_model["summary_label"]


def test_build_standard_mode_context_combines_policy_signals():
    """Standard mode turns on when any pure policy trigger is active."""
    context = build_standard_mode_context(
        manual_enabled=False,
        board_count=50,
        has_v_cut_drawings=True,
        populated_refs={"R1", "R2"},
        populated_sides={"top", "bottom"},
        smt_populated_sides={"top"},
        standard_part_refs={"R1"},
    )

    assert context["board_standard"] is True
    assert context["signals"] == {
        "manual_enabled": False,
        "qty_50_plus": True,
        "v_cut_drawings": True,
        "standard_part_present": True,
        "multi_side_populated": True,
    }
    assert context["smt_populated_sides"] == 1


def test_build_standard_mode_context_highlights_standard_parts_and_multiside_refs():
    """Highlight refs include standard parts and all populated refs for multi-side boards."""
    context = build_standard_mode_context(
        manual_enabled=False,
        board_count=5,
        has_v_cut_drawings=False,
        populated_refs={"R1", "R2", "R3"},
        populated_sides={"top", "bottom"},
        smt_populated_sides={"top", "bottom"},
        standard_part_refs={"R2"},
    )

    assert context["trigger_references"] == {"R1", "R2", "R3"}

    single_side_context = build_standard_mode_context(
        manual_enabled=False,
        board_count=5,
        has_v_cut_drawings=False,
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


def test_prepare_bom_price_labels_returns_reference_to_label_mapping():
    """prepare_bom_price_labels builds a complete {reference: label} dict."""
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


def test_assembly_pricing_custom_instance_overrides_defaults():
    """Passing a custom AssemblyPricing instance changes the estimate totals."""
    part = {
        "lcsc": "C1",
        "exclude_from_bom": 0,
        "pad_count": 2,
        "has_tht": 0,
        "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
    }
    cheap = AssemblyPricing(
        economic_setup_fee=1.0,
        economic_stencil_fee=0.5,
        extended_part_fee=0.0,
        smt_per_joint_fee=0.001,
    )
    summary = calculate_bom_estimate(
        [part],
        board_count=5,
        get_part_details=lambda _: {"price": "1-:0.10", "type": "Basic"},
        pricing=cheap,
    )
    expected_assembly = 1.0 + 0.5 + 10 * 0.001
    assert round(summary.assembly_cost, 4) == round(expected_assembly, 4)


def test_collect_billable_bom_parts_filters_excluded_unassigned_and_dnp():
    """Helper keeps only rows eligible for BOM costing."""
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "assembly_flags": "{}",
        },
        {"reference": "R2", "lcsc": "", "exclude_from_bom": 0, "assembly_flags": "{}"},
        {
            "reference": "R3",
            "lcsc": "C3",
            "exclude_from_bom": 1,
            "assembly_flags": "{}",
        },
        {
            "reference": "R4",
            "lcsc": "C4",
            "exclude_from_bom": 0,
            "assembly_flags": '{"is_dnp": true}',
        },
    ]

    filtered = _collect_billable_bom_parts(parts)

    assert [part["reference"] for part in filtered] == ["R1"]


def test_scan_assembly_state_reports_joints_standard_and_extended_sets():
    """Assembly scan helper computes core mode/surcharge diagnostics."""
    bom_parts = [
        {
            "reference": "U1",
            "lcsc": "C-SMT-STD",
            "pad_count": 2,
            "has_tht": 0,
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false}',
        },
        {
            "reference": "J1",
            "lcsc": "C-THT-EXT",
            "pad_count": 3,
            "has_tht": 1,
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false}',
        },
        {
            "reference": "R9",
            "lcsc": "C-SMT-EXT-NOPOS",
            "pad_count": 2,
            "has_tht": 0,
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": true}',
        },
    ]

    details_map = {
        "C-SMT-STD": {"type": "Basic", "price": "1-:0.10"},
        "C-THT-EXT": {"type": "Extended", "price": "1-:0.20"},
        "C-SMT-EXT-NOPOS": {"type": "Extended", "price": "1-:0.30"},
    }
    scan = _scan_assembly_state(
        bom_parts,
        board_count=5,
        details_cache={},
        get_part_details=lambda lcsc: details_map[lcsc],
    )

    assert scan.standard_present is True
    assert scan.standard_part_count == 1
    assert scan.populated_part_present is True
    assert scan.tht_present is True
    assert scan.smt_joints == 10
    assert scan.tht_joints == 15
    assert scan.smt_lcsc == {"C-SMT-STD"}
    assert scan.extended_lcsc == {"C-SMT-EXT-NOPOS"}
