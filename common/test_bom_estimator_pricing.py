"""Tests for BOM estimator pricing/core logic."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimation.pricing import (  # pylint: disable=import-error
    DEFAULT_PRICING,
    AssemblyPricing,
    _collect_billable_bom_parts,
    _PricingRunContext,
    _scan_assembly_state,
    calculate_bom_estimate,
    get_assembly_flags,
    get_unit_price,
    is_tht_part,
)


def test_get_unit_price_quantity_tiers():
    """Tier parser picks expected unit prices."""
    assert get_unit_price(1, "1-9:0.12,10-99:0.08,100-:0.05") == 0.12
    assert get_unit_price(10, "1-9:0.12,10-99:0.08,100-:0.05") == 0.08
    assert get_unit_price(250, "1-9:0.12,10-99:0.08,100-:0.05") == 0.05


def test_calculate_bom_estimate_smt_and_extended_once_per_lcsc():
    """Economic mode charges extended surcharge once per distinct LCSC."""
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

    p = DEFAULT_PRICING
    expected_assembly = (
        p.economic_setup_fee
        + p.economic_stencil_fee
        + p.extended_part_fee
        + 10 * p.smt_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.000
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)


def test_calculate_bom_estimate_tht_setup_and_no_extended_surcharge_for_tht():
    """THT setup/joint fees apply and SMT extended surcharge is skipped for THT."""
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

    p = DEFAULT_PRICING
    expected_assembly = (
        p.economic_setup_fee + p.tht_setup_fee + 10 * p.tht_per_joint_fee
    )
    assert round(summary.component_cost, 3) == 2.500
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)


def test_standard_mode_does_not_charge_extended_surcharge():
    """Standard mode uses std-parts surcharge and does not apply extended fee."""
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
        return {"price": "1-:1.00", "type": "Extended" if lcsc == "CEXT1" else "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=1,
        get_part_details=get_details,
        board_standard=True,
        smt_populated_sides=1,
    )

    p = DEFAULT_PRICING
    expected_assembly = (
        p.standard_setup_fee
        + p.standard_stencil_fee
        + 2 * p.standard_part_fee
        + 3 * p.smt_per_joint_fee
    )
    assert round(summary.extended_cost, 3) == 0.000
    assert round(summary.standard_part_surcharge_cost, 3) == round(
        2 * p.standard_part_fee, 3
    )
    assert round(summary.assembly_cost, 3) == round(expected_assembly, 3)


def test_get_assembly_flags_handles_bad_json():
    """Malformed JSON in assembly flags falls back to empty dict."""
    assert get_assembly_flags({"assembly_flags": "not-json"}) == {}


def test_is_tht_part_uses_assembly_process_fallback():
    """THT detection falls back to assembly process text when has_tht is missing."""
    assert is_tht_part({"has_tht": None, "assembly_process": "Wave soldering"})
    assert not is_tht_part({"has_tht": None, "assembly_process": "SMT"})


def test_assembly_pricing_custom_instance_overrides_defaults():
    """Custom pricing inputs propagate through total assembly calculation."""
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
    """Billable filter excludes unassigned, excluded, and DNP rows."""
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
    """Scan helper reports joint counts and surcharge sets correctly."""
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
        run_context=_PricingRunContext(get_part_details=lambda lcsc: details_map[lcsc]),
    )

    assert scan.standard_present is True
    assert scan.populated_part_present is True
    assert scan.tht_present is True
    assert scan.smt_joints == 10
    assert scan.tht_joints == 15
    assert scan.smt_lcsc == {"C-SMT-STD"}
    assert scan.extended_lcsc == {"C-SMT-EXT-NOPOS"}
