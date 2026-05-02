"""Pricing models and cost-calculation engine for BOM estimation.

This module is intentionally pure and UI-agnostic so pricing behavior can be
reviewed and tested independently from enrichment transport and presentation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import contextlib
from dataclasses import dataclass, field
import json


@dataclass
class AssemblyPricing:
    """JLC assembly fee schedule in USD.

    Each field represents one pricing rule input to the estimator.
    Update this dataclass when JLC pricing changes; estimator tests should then
    be updated to document the new expected totals.
    """

    # THT assembly
    tht_setup_fee: float = 3.55
    """One-time THT setup charge per order."""
    tht_per_joint_fee: float = 0.0163
    """Per-pad fee for wave/THT soldering."""

    # SMT assembly (joint-level)
    smt_per_joint_fee: float = 0.0016
    """Per-pad fee for SMT placement."""

    # Extended part surcharge (Economic mode only)
    extended_part_fee: float = 3.04
    """Per distinct extended-part LCSC code fee in Economic mode."""

    # Standard mode fixed fees
    standard_setup_fee: float = 25.4
    """Per-populated-SMT-side setup fee in Standard mode."""
    standard_part_fee: float = 1.52
    """Per distinct SMT LCSC code fee in Standard mode."""
    standard_stencil_fee: float = 7.8
    """Per-populated-SMT-side stencil fee in Standard mode."""

    # Economic mode fixed fees
    economic_setup_fee: float = 8.12
    """One-time setup fee in Economic mode."""
    economic_stencil_fee: float = 1.52
    """One-time stencil fee in Economic mode."""


@dataclass
class BomEstimateSummary:
    """Typed result payload for BOM estimate totals and diagnostics.

    Buckets are designed to let UI show non-overlapping categories while still
    preserving fields needed for policy troubleshooting and tests.
    """

    component_cost: float = 0.0
    fixed_cost: float = 0.0
    tht_setup_cost: float = 0.0
    economic_setup_cost: float = 0.0
    standard_setup_cost: float = 0.0
    stencil_cost: float = 0.0
    policy_cost: float = 0.0
    extended_cost: float = 0.0
    standard_part_surcharge_cost: float = 0.0
    variable_assembly_cost: float = 0.0
    assembly_cost: float = 0.0
    total_cost: float = 0.0
    cost_per_board: float = 0.0
    missing_prices: int = 0
    bom_part_count: int = 0
    standard_part_count: int = 0
    smt_joint_count: int = 0
    tht_joint_count: int = 0

    def __getitem__(self, key: str):
        """Temporary mapping-style access during migration from dict summaries."""
        return getattr(self, key)


DEFAULT_PRICING = AssemblyPricing()


@dataclass
class _AssemblyScan:
    """Intermediate state gathered while scanning BOM rows."""

    tht_present: bool = False
    standard_present: bool = False
    populated_part_present: bool = False
    tht_joints: int = 0
    smt_joints: int = 0
    standard_part_count: int = 0
    extended_lcsc: set[str] = field(default_factory=set)
    smt_lcsc: set[str] = field(default_factory=set)


def get_unit_price(quantity: int, prices: str) -> float:
    """Resolve quantity-tiered unit price from encoded price bands.

    The expected encoding is ``"min-max:price"`` comma-separated, e.g.
    ``"1-9:0.12,10-99:0.08,100-:0.05"`` where ``100-`` means open-ended.

    Returns ``-1.0`` when no valid tier can be resolved.
    """
    if not prices:
        return -1.0

    bands = []
    for token in prices.split(","):
        if ":" not in token or "-" not in token:
            continue
        quantity_range, price = token.split(":", maxsplit=1)
        lower_s, upper_s = quantity_range.split("-", maxsplit=1)
        try:
            lower = int(lower_s)
            upper = int(upper_s) if upper_s else None
            unit_price = float(price)
        except ValueError:
            continue
        bands.append((lower, upper, unit_price))

    if not bands:
        return -1.0

    bands.sort(key=lambda x: x[0])
    if quantity <= bands[0][0]:
        return bands[0][2]

    for lower, upper, unit_price in bands:
        if upper is None and quantity >= lower:
            return unit_price
        if upper is not None and lower <= quantity < upper:
            return unit_price

    return -1.0


def is_tht_part(part: Mapping[str, object]) -> bool:
    """Return True if metadata indicates a through-hole part."""
    if part.get("has_tht") is not None:
        return bool(part.get("has_tht"))

    assembly_process = str(part.get("assembly_process") or "").lower()
    return (
        "through" in assembly_process
        or "wave" in assembly_process
        or "tht" in assembly_process
    )


def get_assembly_flags(part: Mapping[str, object]) -> dict:
    """Parse assembly flags from persisted JSON."""
    try:
        return json.loads(str(part.get("assembly_flags") or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _safe_int(value: object, default: int = 0) -> int:
    """Best-effort integer conversion for persisted metadata fields."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        with contextlib.suppress(ValueError, TypeError):
            return int(value)
    return default


def _create_empty_summary() -> BomEstimateSummary:
    """Create an initialized BOM estimate summary payload."""
    return BomEstimateSummary()


def _collect_billable_bom_parts(
    parts: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Filter parts down to BOM-assigned, non-DNP rows."""
    bom_parts: list[Mapping[str, object]] = []
    for part in parts:
        if part.get("exclude_from_bom") or not str(part.get("lcsc") or ""):
            continue
        flags = get_assembly_flags(part)
        if bool(flags.get("is_dnp", False)):
            continue
        bom_parts.append(part)
    return bom_parts


def _get_cached_part_details(
    lcsc: str,
    details_cache: dict[str, dict],
    get_part_details: Callable[[str], dict],
) -> dict:
    """Fetch and cache part details by LCSC code."""
    details = details_cache.get(lcsc)
    if details is None:
        details = get_part_details(lcsc)
        details_cache[lcsc] = details
    return details


def _build_lcsc_quantities(
    bom_parts: Iterable[Mapping[str, object]], board_count: int
) -> dict[str, int]:
    """Aggregate quantity per LCSC code for component pricing lookup."""
    lcsc_quantities: dict[str, int] = {}
    for part in bom_parts:
        lcsc = str(part.get("lcsc") or "")
        lcsc_quantities[lcsc] = lcsc_quantities.get(lcsc, 0) + board_count
    return lcsc_quantities


def _calculate_component_costs(
    lcsc_quantities: Mapping[str, int],
    *,
    details_cache: dict[str, dict],
    get_part_details: Callable[[str], dict],
) -> tuple[float, int]:
    """Calculate direct component cost and missing-price count."""
    component_cost = 0.0
    missing_prices = 0
    for lcsc, quantity in lcsc_quantities.items():
        details = _get_cached_part_details(lcsc, details_cache, get_part_details)
        unit_price = get_unit_price(quantity, str(details.get("price") or ""))
        if unit_price < 0:
            missing_prices += 1
            continue
        component_cost += unit_price * quantity
    return component_cost, missing_prices


def _scan_assembly_state(
    bom_parts: Iterable[Mapping[str, object]],
    board_count: int,
    *,
    details_cache: dict[str, dict],
    get_part_details: Callable[[str], dict],
) -> _AssemblyScan:
    """Scan BOM rows to collect assembly-mode and surcharge metrics."""
    scan = _AssemblyScan()

    for part in bom_parts:
        lcsc = str(part.get("lcsc") or "")
        details = _get_cached_part_details(lcsc, details_cache, get_part_details)

        if _safe_int(part.get("component_product_type")) != 0:
            scan.standard_present = True
            scan.standard_part_count += 1

        flags = get_assembly_flags(part)
        exclude_from_pos = bool(flags.get("exclude_from_pos", False))
        tht = is_tht_part(part)

        if not exclude_from_pos:
            scan.populated_part_present = True
            joints = max(0, _safe_int(part.get("pad_count"))) * board_count
            if tht:
                scan.tht_present = True
                scan.tht_joints += joints
            else:
                scan.smt_joints += joints
                scan.smt_lcsc.add(lcsc)

        if str(details.get("type") or "") == "Extended" and not tht:
            scan.extended_lcsc.add(lcsc)

    return scan


def calculate_bom_estimate(
    parts: Iterable[Mapping],
    board_count: int,
    get_part_details: Callable[[str], dict],
    *,
    pricing: AssemblyPricing | None = None,
    board_standard: bool | None = None,
    smt_populated_sides: int = 0,
    order_handling_fee: float = 0.0,
    panelization_per_board_fee: float = 0.0,
    panelization_threshold_boards: int = 1,
) -> BomEstimateSummary:
    """Calculate BOM and assembly estimate totals.

    The calculation proceeds in phases:
    1) sanitize policy knobs (order/panelization),
    2) collect billable BOM rows,
    3) compute direct component costs,
    4) scan assembly metadata/joint counts,
    5) apply mode-specific setup/surcharge rules,
    6) roll up totals and per-board value.
    """
    p = pricing if pricing is not None else DEFAULT_PRICING
    _tht_setup_fee = p.tht_setup_fee
    _tht_per_joint_fee = p.tht_per_joint_fee
    _smt_per_joint_fee = p.smt_per_joint_fee
    _extended_part_fee = p.extended_part_fee
    _standard_setup_fee = p.standard_setup_fee
    _standard_part_fee = p.standard_part_fee
    _economic_setup_fee = p.economic_setup_fee
    _economic_stencil_fee = p.economic_stencil_fee
    _standard_stencil_fee = p.standard_stencil_fee
    summary = _create_empty_summary()

    # Phase 1: normalize policy fees and threshold values.
    try:
        order_fee = max(0.0, float(order_handling_fee))
    except (TypeError, ValueError):
        order_fee = 0.0
    try:
        panel_fee = max(0.0, float(panelization_per_board_fee))
    except (TypeError, ValueError):
        panel_fee = 0.0
    try:
        panel_threshold = max(1, int(panelization_threshold_boards))
    except (TypeError, ValueError):
        panel_threshold = 1

    summary.policy_cost += order_fee
    if board_count >= panel_threshold:
        summary.policy_cost += panel_fee * board_count

    # Phase 2: keep only BOM-eligible rows with assigned LCSC code.
    bom_parts = _collect_billable_bom_parts(parts)
    summary.bom_part_count = len(bom_parts)
    if not bom_parts:
        return summary

    lcsc_quantities = _build_lcsc_quantities(bom_parts, board_count)

    details_cache: dict[str, dict] = {}

    # Phase 3: direct component cost from quantity-tier prices.
    component_cost, missing_prices = _calculate_component_costs(
        lcsc_quantities,
        details_cache=details_cache,
        get_part_details=get_part_details,
    )
    summary.component_cost = component_cost
    summary.missing_prices = missing_prices

    # Phase 4: assembly mode signals, joints, and surcharge sets.
    scan = _scan_assembly_state(
        bom_parts,
        board_count,
        details_cache=details_cache,
        get_part_details=get_part_details,
    )
    summary.standard_part_count = scan.standard_part_count

    # Phase 5: fixed fees and stencil/setup rules by mode.
    if scan.tht_present:
        summary.tht_setup_cost += _tht_setup_fee

    board_is_standard = (
        scan.standard_present if board_standard is None else board_standard
    )
    if not board_is_standard and scan.populated_part_present:
        summary.economic_setup_cost += _economic_setup_fee

    if board_is_standard:
        side_count = max(0, int(smt_populated_sides or 0))
        if side_count > 0:
            summary.standard_setup_cost += _standard_setup_fee * side_count
            summary.stencil_cost += _standard_stencil_fee * side_count
    elif scan.smt_joints > 0:
        summary.stencil_cost += _economic_stencil_fee

    summary.fixed_cost = (
        summary.tht_setup_cost
        + summary.economic_setup_cost
        + summary.standard_setup_cost
        + summary.stencil_cost
        + summary.policy_cost
    )

    summary.smt_joint_count = scan.smt_joints
    summary.tht_joint_count = scan.tht_joints
    summary.variable_assembly_cost += scan.tht_joints * _tht_per_joint_fee
    summary.variable_assembly_cost += scan.smt_joints * _smt_per_joint_fee

    # In Standard mode, JLC applies per-distinct-SMT-part surcharge regardless of
    # Extended/Basic classification. The Extended surcharge is Economic-only.
    if board_is_standard:
        summary.standard_part_surcharge_cost += len(scan.smt_lcsc) * _standard_part_fee
        summary.variable_assembly_cost += summary.standard_part_surcharge_cost
    else:
        summary.extended_cost += len(scan.extended_lcsc) * _extended_part_fee

    # Phase 6: final totals.
    summary.assembly_cost = (
        summary.fixed_cost + summary.extended_cost + summary.variable_assembly_cost
    )
    summary.total_cost = summary.component_cost + summary.assembly_cost
    if board_count > 0:
        summary.cost_per_board = summary.total_cost / board_count
    return summary


def calculate_part_bom_cost(
    part: Mapping[str, object], details: Mapping[str, object], board_count: int
) -> float | None:
    """Return the raw BOM contribution for a part, excluding fixed fees."""
    if part.get("exclude_from_bom"):
        return None

    lcsc = str(part.get("lcsc") or "")
    if not lcsc:
        return None

    unit_price = get_unit_price(board_count, str(details.get("price") or ""))
    if unit_price < 0:
        return None

    return unit_price * board_count
