"""BOM estimator logic extracted from UI for testability."""

from __future__ import annotations

import contextlib
import json
from typing import Callable, Dict, Iterable, Mapping, Optional, Protocol

try:
    from .lcsc_api import LCSC_API
except ImportError:  # pragma: no cover - test import fallback
    from lcsc_api import LCSC_API


class _AssemblyApi(Protocol):
    """Protocol for API clients used by assembly enrichment logic."""

    def get_part_data(self, lcsc: str) -> dict:
        """Fetch part details for an LCSC number."""


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


def is_tht_part(part: dict) -> bool:
    """Return True if metadata indicates a through-hole part."""
    if part.get("has_tht") is not None:
        return bool(part.get("has_tht"))

    assembly_process = str(part.get("assembly_process") or "").lower()
    return (
        "through" in assembly_process
        or "wave" in assembly_process
        or "tht" in assembly_process
    )


def get_assembly_flags(part: dict) -> dict:
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


def calculate_bom_estimate(
    parts: Iterable[Mapping],
    board_count: int,
    get_part_details: Callable[[str], dict],
    *,
    tht_setup_fee: float = 3.50,
    tht_per_joint_fee: float = 0.0157,
    smt_per_joint_fee: float = 0.0016,
    extended_part_fee: float = 3.00,
    standard_setup_fee: float = 25.0,
    standard_part_fee: float = 1.5,
    economic_setup_fee: float = 8.0,
    economic_stencil_fee: float = 1.5,
    standard_stencil_fee: float = 7.8,
    order_handling_fee: float = 0.0,
    panelization_per_board_fee: float = 0.0,
    panelization_threshold_boards: int = 1,
    board_standard: Optional[bool] = None,
    smt_populated_sides: int = 0,
) -> dict:
    """Calculate BOM and assembly estimate totals.

    Returns a summary dict with totals and diagnostics.
    """
    summary = {
        "component_cost": 0.0,
        "fixed_cost": 0.0,
        "tht_setup_cost": 0.0,
        "economic_setup_cost": 0.0,
        "standard_setup_cost": 0.0,
        "stencil_cost": 0.0,
        "policy_cost": 0.0,
        "extended_cost": 0.0,
        "variable_assembly_cost": 0.0,
        "assembly_cost": 0.0,
        "total_cost": 0.0,
        "cost_per_board": 0.0,
        "missing_prices": 0,
        "bom_part_count": 0,
        "standard_part_count": 0,
    }

    bom_parts = [
        part
        for part in parts
        if not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
    ]
    summary["bom_part_count"] = len(bom_parts)
    if not bom_parts:
        return summary

    lcsc_quantities = {}
    for part in bom_parts:
        lcsc = str(part.get("lcsc") or "")
        lcsc_quantities[lcsc] = lcsc_quantities.get(lcsc, 0) + board_count

    details_cache = {}

    for lcsc, quantity in lcsc_quantities.items():
        details = details_cache.get(lcsc)
        if details is None:
            details = get_part_details(lcsc)
            details_cache[lcsc] = details

        unit_price = get_unit_price(quantity, str(details.get("price") or ""))
        if unit_price < 0:
            summary["missing_prices"] += 1
            continue
        summary["component_cost"] += unit_price * quantity

    tht_present = False
    standard_present = False
    populated_part_present = False
    tht_joints = 0
    smt_joints = 0
    extended_lcsc = set()
    smt_lcsc = set()

    for part in bom_parts:
        lcsc = str(part.get("lcsc") or "")
        details = details_cache.get(lcsc)
        if details is None:
            details = get_part_details(lcsc)
            details_cache[lcsc] = details

        with contextlib.suppress(ValueError, TypeError):
            if int(part.get("component_product_type")) != 0:
                standard_present = True
                summary["standard_part_count"] += 1

        flags = get_assembly_flags(part)
        is_dnp = bool(flags.get("is_dnp", False))
        exclude_from_pos = bool(flags.get("exclude_from_pos", False))
        tht = is_tht_part(part)

        if not is_dnp and not exclude_from_pos:
            populated_part_present = True
            joints = max(0, int(part.get("pad_count") or 0)) * board_count
            if tht:
                tht_present = True
                tht_joints += joints
            else:
                smt_joints += joints
                smt_lcsc.add(lcsc)

        if str(details.get("type") or "") == "Extended" and not tht:
            extended_lcsc.add(lcsc)

    if tht_present:
        summary["tht_setup_cost"] += tht_setup_fee

    board_is_standard = standard_present if board_standard is None else board_standard
    if not board_is_standard and populated_part_present:
        summary["economic_setup_cost"] += economic_setup_fee

    if board_is_standard:
        side_count = max(0, int(smt_populated_sides or 0))
        if side_count > 0:
            summary["standard_setup_cost"] += standard_setup_fee * side_count
            summary["stencil_cost"] += standard_stencil_fee * side_count
    elif smt_joints > 0:
        summary["stencil_cost"] += economic_stencil_fee

    if populated_part_present and order_handling_fee > 0:
        summary["policy_cost"] += order_handling_fee
    if (
        populated_part_present
        and panelization_per_board_fee > 0
        and board_count >= max(1, int(panelization_threshold_boards))
    ):
        summary["policy_cost"] += panelization_per_board_fee * board_count

    summary["fixed_cost"] = (
        summary["tht_setup_cost"]
        + summary["economic_setup_cost"]
        + summary["standard_setup_cost"]
        + summary["stencil_cost"]
        + summary["policy_cost"]
    )

    summary["variable_assembly_cost"] += tht_joints * tht_per_joint_fee
    summary["variable_assembly_cost"] += smt_joints * smt_per_joint_fee
    summary["extended_cost"] += len(extended_lcsc) * extended_part_fee
    if board_is_standard:
        summary["variable_assembly_cost"] += len(smt_lcsc) * standard_part_fee

    summary["assembly_cost"] = (
        summary["fixed_cost"]
        + summary["extended_cost"]
        + summary["variable_assembly_cost"]
    )
    summary["total_cost"] = summary["component_cost"] + summary["assembly_cost"]
    if board_count > 0:
        summary["cost_per_board"] = summary["total_cost"] / board_count
    return summary