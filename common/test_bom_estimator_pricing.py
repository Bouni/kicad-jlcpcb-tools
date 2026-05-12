"""Tests for BOM estimator pricing/core logic."""

import pytest

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


def test_get_unit_price_band_boundaries_are_closed_on_both_ends():
    """Tier bounds are inclusive on both ends; bands are non-overlapping.

    JLC's qFrom/qTo brackets are inclusive: a `1-9` band covers q in [1, 9]
    and the next bracket starts at q=10. The previous half-open implementation
    returned -1.0 at every band's upper boundary (q=9, q=99).
    """
    bands = "1-9:0.12,10-99:0.08,100-999:0.05,1000-:0.02"

    # Band 1-9 (closed both ends): just below lower clamps to first band,
    # then 1, 9, just-over (10) crosses into next band.
    assert get_unit_price(0, bands) == 0.12  # below first lower → first price
    assert get_unit_price(1, bands) == 0.12  # exact lower
    assert get_unit_price(9, bands) == 0.12  # exact upper (was the off-by-one)
    assert get_unit_price(10, bands) == 0.08  # exact next lower

    # Band 10-99: lower, just-over, just-under upper, exact upper
    assert get_unit_price(11, bands) == 0.08  # just over lower
    assert get_unit_price(98, bands) == 0.08  # just under upper
    assert get_unit_price(99, bands) == 0.08  # exact upper
    assert get_unit_price(100, bands) == 0.05  # exact next lower

    # Band 100-999: middle band, both boundaries inclusive
    assert get_unit_price(100, bands) == 0.05
    assert get_unit_price(500, bands) == 0.05
    assert get_unit_price(999, bands) == 0.05  # exact upper
    assert get_unit_price(1000, bands) == 0.02  # exact next lower

    # Open-ended last band
    assert get_unit_price(1000, bands) == 0.02
    assert get_unit_price(99999, bands) == 0.02


def test_get_unit_price_drops_malformed_tokens_and_resolves_valid_bands():
    """Garbage tokens are dropped silently and valid bands still resolve."""
    # Mix of valid and malformed tokens; valid bands should still produce
    # the right price.
    mixed = "junk,1-9:0.50,no-colon,abc-9:0.10,10-99:0.30,100-:bad,200-:0.05"
    assert get_unit_price(1, mixed) == 0.50
    assert get_unit_price(9, mixed) == 0.50
    assert get_unit_price(10, mixed) == 0.30
    assert get_unit_price(99, mixed) == 0.30
    # 200- is the only valid open-ended band; "100-:bad" was dropped.
    assert get_unit_price(500, mixed) == 0.05


def test_get_unit_price_returns_negative_one_for_fully_malformed_string():
    """Wholly malformed input returns the -1.0 sentinel."""
    assert get_unit_price(5, "garbage,nope,also-bad") == -1.0
    assert get_unit_price(5, "") == -1.0
    assert get_unit_price(5, "1-9") == -1.0  # no colon
    assert get_unit_price(5, "abc-def:0.10") == -1.0  # non-int range


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
    assert summary.component_cost == pytest.approx(2.000, abs=1e-3)
    assert summary.assembly_cost == pytest.approx(expected_assembly, abs=1e-3)


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
    assert summary.component_cost == pytest.approx(2.500, abs=1e-3)
    assert summary.assembly_cost == pytest.approx(expected_assembly, abs=1e-3)


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
    assert summary.standard_part_surcharge_cost == pytest.approx(
        2 * p.standard_part_fee, abs=1e-3
    )
    assert summary.assembly_cost == pytest.approx(expected_assembly, abs=1e-3)


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
    assert summary.assembly_cost == pytest.approx(expected_assembly, abs=1e-4)


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
