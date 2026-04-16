"""BOM estimator logic extracted from UI for testability."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Mapping, Optional, Protocol


@dataclass
class AssemblyPricing:
    """JLC assembly fee schedule.

    All values are in USD.  Update this class when JLC changes their prices —
    the rest of the estimator and its tests derive from these constants.
    """

    # THT assembly
    tht_setup_fee: float = 3.50
    """One-time THT setup charge per order."""
    tht_per_joint_fee: float = 0.0157
    """Per-pad fee for wave/THT soldering."""

    # SMT assembly (joint-level)
    smt_per_joint_fee: float = 0.0016
    """Per-pad fee for SMT placement."""

    # Extended part surcharge
    extended_part_fee: float = 3.00
    """Per distinct extended-part LCSC code fee."""

    # Standard mode fixed fees
    standard_setup_fee: float = 25.0
    """Per-populated-SMT-side setup fee in Standard mode."""
    standard_part_fee: float = 1.5
    """Per distinct SMT LCSC code fee in Standard mode."""
    standard_stencil_fee: float = 7.8
    """Per-populated-SMT-side stencil fee in Standard mode."""

    # Economic mode fixed fees
    economic_setup_fee: float = 8.0
    """One-time setup fee in Economic mode."""
    economic_stencil_fee: float = 1.5
    """One-time stencil fee in Economic mode."""


@dataclass
class BomEstimateSummary:
    """Typed BOM estimate result payload."""

    component_cost: float = 0.0
    fixed_cost: float = 0.0
    tht_setup_cost: float = 0.0
    economic_setup_cost: float = 0.0
    standard_setup_cost: float = 0.0
    stencil_cost: float = 0.0
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

try:
    from .lcsc_api import LCSC_API
except ImportError:  # pragma: no cover - test import fallback
    from lcsc_api import LCSC_API


class _AssemblyApi(Protocol):
    """Protocol for API clients used by assembly enrichment logic."""

    def get_part_data(self, lcsc: str) -> dict:
        """Fetch part details for an LCSC number."""


@dataclass
class _AssemblyScan:
    """Intermediate assembly scan state for BOM estimate aggregation."""

    tht_present: bool = False
    standard_present: bool = False
    populated_part_present: bool = False
    tht_joints: int = 0
    smt_joints: int = 0
    standard_part_count: int = 0
    extended_lcsc: set[str] = field(default_factory=set)
    smt_lcsc: set[str] = field(default_factory=set)


def get_unit_price(quantity: int, prices: str) -> float:
    """Resolve quantity-tiered unit price from encoded price bands."""
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
        return json.loads(part.get("assembly_flags") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def fetch_assembly_processes(
    lcsc_codes: Iterable[str],
    api: Optional[_AssemblyApi] = None,
) -> Dict[str, Dict[str, object]]:
    """Fetch assembly metadata values from LCSC API for the given part numbers."""
    client = api or LCSC_API()
    results = {}
    for lcsc in lcsc_codes:
        assembly_process = ""
        component_product_type = None
        try:
            part_data = client.get_part_data(lcsc)
            if part_data.get("success"):
                payload = part_data.get("data", {}).get("data", {})
                assembly_process = payload.get("assemblyProcess", "")
                component_product_type = payload.get("componentProductType")
        except Exception:  # pylint: disable=broad-exception-caught
            assembly_process = ""
            component_product_type = None

        is_standard = False
        with contextlib.suppress(ValueError, TypeError):
            is_standard = int(component_product_type) != 0

        results[lcsc] = {
            "assembly_process": assembly_process,
            "component_product_type": component_product_type,
            "is_standard_assembly": is_standard,
        }
    return results


def _create_empty_summary() -> BomEstimateSummary:
    """Create an initialized BOM estimate summary payload."""
    return BomEstimateSummary()


def _collect_billable_bom_parts(parts: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
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

        with contextlib.suppress(ValueError, TypeError):
            if int(part.get("component_product_type")) != 0:
                scan.standard_present = True
                scan.standard_part_count += 1

        flags = get_assembly_flags(part)
        exclude_from_pos = bool(flags.get("exclude_from_pos", False))
        tht = is_tht_part(part)

        if not exclude_from_pos:
            scan.populated_part_present = True
            joints = max(0, int(part.get("pad_count") or 0)) * board_count
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
    pricing: Optional[AssemblyPricing] = None,
    board_standard: Optional[bool] = None,
    smt_populated_sides: int = 0,
) -> BomEstimateSummary:
    """Calculate BOM and assembly estimate totals.

    Pass a custom ``pricing`` instance to override the fee schedule.

    Returns a summary dict with totals and diagnostics.
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

    bom_parts = _collect_billable_bom_parts(parts)
    summary.bom_part_count = len(bom_parts)
    if not bom_parts:
        return summary

    lcsc_quantities = _build_lcsc_quantities(bom_parts, board_count)

    details_cache: dict[str, dict] = {}

    component_cost, missing_prices = _calculate_component_costs(
        lcsc_quantities,
        details_cache=details_cache,
        get_part_details=get_part_details,
    )
    summary.component_cost = component_cost
    summary.missing_prices = missing_prices

    scan = _scan_assembly_state(
        bom_parts,
        board_count,
        details_cache=details_cache,
        get_part_details=get_part_details,
    )
    summary.standard_part_count = scan.standard_part_count

    if scan.tht_present:
        summary.tht_setup_cost += _tht_setup_fee

    board_is_standard = scan.standard_present if board_standard is None else board_standard
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
    )

    summary.smt_joint_count = scan.smt_joints
    summary.tht_joint_count = scan.tht_joints
    summary.variable_assembly_cost += scan.tht_joints * _tht_per_joint_fee
    summary.variable_assembly_cost += scan.smt_joints * _smt_per_joint_fee
    summary.extended_cost += len(scan.extended_lcsc) * _extended_part_fee
    if board_is_standard:
        summary.standard_part_surcharge_cost += len(scan.smt_lcsc) * _standard_part_fee
        summary.variable_assembly_cost += summary.standard_part_surcharge_cost

    summary.assembly_cost = (
        summary.fixed_cost
        + summary.extended_cost
        + summary.variable_assembly_cost
    )
    summary.total_cost = summary.component_cost + summary.assembly_cost
    if board_count > 0:
        summary.cost_per_board = summary.total_cost / board_count
    return summary


def format_bom_estimate_summary(
    summary: BomEstimateSummary, board_count: int, mode: str, reason_text: str
) -> tuple[str, str]:
    """Format BOM estimate summary into display lines.

    Args:
        summary: BOM estimate from calculate_bom_estimate()
        board_count: Number of boards
        mode: "Standard" or "Economic" mode string
        reason_text: Comma-joined reason text for triggers (or "none")

    Returns:
        (overview_line, details_line) tuple for two-line display
    """
    overview_line = (
        f"BOM Estimate ({board_count} boards): Mode {mode} | "
        f"Total ${summary.total_cost:.2f} | "
        f"Per board ${summary.cost_per_board:.2f} | "
        f"Triggers {reason_text} | "
        f"Missing prices {summary.missing_prices}"
    )

    displayed_fixed_cost = summary.fixed_cost + summary.extended_cost
    displayed_setup_cost = summary.economic_setup_cost + summary.standard_setup_cost

    # Assembly cost includes variable joint fees and surcharges (extended + standard)
    # We show them separately in the breakdown for clarity
    surcharge_breakdown = f"extended: ${summary.extended_cost:.2f}"
    if summary.standard_part_surcharge_cost > 0:
        surcharge_breakdown += f", standard: ${summary.standard_part_surcharge_cost:.2f}"

    details_line = (
        f"Direct BOM Cost: ${summary.component_cost:.2f} | "
        f"Fixed ${displayed_fixed_cost:.2f} "
        f"({surcharge_breakdown}, setup: ${displayed_setup_cost:.2f}, "
        f"stencil: ${summary.stencil_cost:.2f}, tht: ${summary.tht_setup_cost:.2f}) | "
        f"Assembly ${summary.variable_assembly_cost:.2f} "
        f"(smt: {summary.smt_joint_count} joints, tht: {summary.tht_joint_count} joints)"
    )

    return overview_line, details_line


def standard_signal_reasons(signals: Mapping[str, object]) -> list[str]:
    """Build user-facing reason labels for active Standard-mode triggers."""
    reason_map = [
        ("manual_enabled", "manual"),
        ("qty_50_plus", "qty≥50"),
        ("v_cut_drawings", "V-cut layer"),
        ("standard_part_present", "standard part"),
        ("multi_side_populated", "both sides populated"),
    ]
    return [label for key, label in reason_map if signals.get(key)]


def calculate_part_bom_cost(
    part: Mapping[str, object], details: Mapping[str, object], board_count: int
) -> Optional[float]:
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


def format_part_bom_price_label(
    part: Mapping[str, object], details: Mapping[str, object], board_count: int
) -> str:
    """Build per-part BOM contribution label for UI display."""
    if part.get("exclude_from_bom"):
        return ""

    lcsc = str(part.get("lcsc") or "")
    if not lcsc:
        return ""

    contribution = calculate_part_bom_cost(part, details, board_count)
    if contribution is None:
        return "N/A"

    return f"${contribution:.4f}"


def build_bom_estimate_view_model(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    get_part_details: Callable[[str], dict],
    standard_context: Mapping[str, object],
) -> dict:
    """Build a pure BOM estimate view model for UI consumption."""
    parts = list(parts)
    if not parts:
        return {
            "summary": None,
            "mode": None,
            "reason_text": "none",
            "highlight_refs": set(),
            "summary_label": f"BOM Estimate ({board_count} boards): no parts",
        }

    bom_parts = [
        part
        for part in parts
        if not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
    ]
    if not bom_parts:
        return {
            "summary": None,
            "mode": None,
            "reason_text": "none",
            "highlight_refs": set(),
            "summary_label": f"BOM Estimate ({board_count} boards): no assigned BOM parts",
        }

    board_standard = bool(standard_context.get("board_standard"))
    summary = calculate_bom_estimate(
        parts=parts,
        board_count=board_count,
        get_part_details=get_part_details,
        board_standard=board_standard,
        smt_populated_sides=int(standard_context.get("smt_populated_sides") or 0),
    )

    mode = "Standard" if board_standard else "Economic"
    reason_text = ", ".join(
        standard_signal_reasons(standard_context.get("signals", {}))
    ) or "none"
    highlight_refs = (
        set(standard_context.get("trigger_references", set())) if board_standard else set()
    )
    overview_line, details_line = format_bom_estimate_summary(
        summary,
        board_count,
        mode,
        reason_text,
    )
    return {
        "summary": summary,
        "mode": mode,
        "reason_text": reason_text,
        "highlight_refs": highlight_refs,
        "summary_label": f"{overview_line}\n{details_line}",
    }


def build_standard_mode_context(
    *,
    manual_enabled: bool,
    board_count: int,
    has_v_cut_drawings: bool,
    populated_refs: Iterable[str],
    populated_sides: Iterable[str],
    smt_populated_sides: Iterable[str],
    standard_part_refs: Iterable[str],
) -> dict:
    """Build pure Standard/Economic policy context from normalized board facts."""
    populated_refs = set(populated_refs)
    populated_sides = set(populated_sides)
    smt_populated_sides = set(smt_populated_sides)
    standard_part_refs = set(standard_part_refs)

    signals = {
        "manual_enabled": bool(manual_enabled),
        "qty_50_plus": board_count >= 50,
        "v_cut_drawings": bool(has_v_cut_drawings),
        "standard_part_present": bool(standard_part_refs),
        "multi_side_populated": len(populated_sides) > 1,
    }
    trigger_references = set(standard_part_refs)
    if signals["multi_side_populated"]:
        trigger_references.update(populated_refs)

    return {
        "signals": signals,
        "board_standard": any(signals.values()),
        "smt_populated_sides": len(smt_populated_sides),
        "trigger_references": trigger_references,
    }


def prepare_bom_price_labels(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    get_part_details: Callable[[str], dict],
) -> dict:
    """Return a {reference: label} mapping for BOM price column population.

    Pure function — no UI or wx dependencies.  Callers are responsible for
    applying the resulting labels to the data model.
    """
    details_cache: dict = {}
    result: dict = {}
    for part in parts:
        reference = part.get("reference")
        if not reference:
            continue

        lcsc = str(part.get("lcsc") or "")
        details: dict = {}
        if lcsc:
            if lcsc not in details_cache:
                details_cache[lcsc] = get_part_details(lcsc)
            details = details_cache[lcsc]

        result[reference] = format_part_bom_price_label(part, details, board_count)

    return result