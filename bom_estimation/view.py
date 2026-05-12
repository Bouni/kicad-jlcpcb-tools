"""View/presentation helpers for BOM estimation.

This module contains formatting and UI-oriented derivations and stays free of
transport concerns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import cast

from .pricing import (
    BomEstimateSummary,
    _build_lcsc_quantities,
    _safe_int,
    calculate_bom_estimate,
    calculate_part_bom_cost,
    get_unit_price,
)


def format_bom_estimate_summary(
    summary: BomEstimateSummary, board_count: int, mode: str, reason_text: str
) -> tuple[str, str]:
    """Format BOM estimate summary into two compact UI lines."""
    overview_line = (
        f"BOM Estimate ({board_count} boards): Mode {mode} | "
        f"Total ${summary.total_cost:.2f} | "
        f"Per board ${summary.cost_per_board:.2f} | "
        f"Triggers {reason_text} | "
        f"Missing prices {summary.missing_prices}"
    )

    mode_is_standard = str(mode).strip().lower() == "standard"
    mode_surcharge_cost = 0.0
    if mode_is_standard:
        mode_surcharge_cost += summary.standard_part_surcharge_cost
    else:
        mode_surcharge_cost += summary.extended_cost

    displayed_fixed_cost = summary.fixed_cost + mode_surcharge_cost
    displayed_setup_cost = summary.economic_setup_cost + summary.standard_setup_cost
    displayed_joint_assembly_cost = (
        summary.variable_assembly_cost - summary.standard_part_surcharge_cost
    )
    if displayed_joint_assembly_cost < 0:
        displayed_joint_assembly_cost = 0.0

    surcharge_labels = []
    if not mode_is_standard and summary.extended_cost > 0:
        surcharge_labels.append(f"extended: ${summary.extended_cost:.2f}")
    if mode_is_standard and summary.standard_part_surcharge_cost > 0:
        surcharge_labels.append(
            f"std-parts: ${summary.standard_part_surcharge_cost:.2f}"
        )
    surcharge_breakdown = (
        ", ".join(surcharge_labels) if surcharge_labels else "surcharges: $0.00"
    )

    details_line = (
        f"Direct BOM Cost: ${summary.component_cost:.2f} | "
        f"Fixed ${displayed_fixed_cost:.2f} "
        f"({surcharge_breakdown}, setup: ${displayed_setup_cost:.2f}, "
        f"stencil: ${summary.stencil_cost:.2f}, tht: ${summary.tht_setup_cost:.2f}) | "
        f"Assembly ${displayed_joint_assembly_cost:.2f} "
        f"(smt: {summary.smt_joint_count} joints, tht: {summary.tht_joint_count} joints)"
    )

    return overview_line, details_line


def standard_signal_reasons(signals: Mapping[str, object]) -> list[str]:
    """Build user-facing reason labels for active Standard-mode triggers."""
    reason_map = [
        ("manual_enabled", "manual"),
        ("qty_50_plus", "qty≥50"),
        ("standard_part_present", "standard part"),
        ("multi_side_populated", "both sides populated"),
    ]
    return [label for key, label in reason_map if signals.get(key)]


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
    """Build a pure BOM estimate view model for UI consumption.

    Returned mapping keys:
    - ``summary``: ``BomEstimateSummary`` or ``None`` when insufficient data
    - ``mode``: ``"Standard"`` / ``"Economic"`` / ``None``
    - ``reason_text``: comma-separated trigger labels
    - ``highlight_refs``: references to emphasize for Standard triggers
    - ``summary_label``: two-line user-facing summary text
    """
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
    smt_side_count = _safe_int(standard_context.get("smt_populated_sides"))
    signals = cast(Mapping[str, object], standard_context.get("signals", {}))
    trigger_references = cast(
        Iterable[str], standard_context.get("trigger_references", set())
    )
    summary = calculate_bom_estimate(
        parts=parts,
        board_count=board_count,
        get_part_details=get_part_details,
        board_standard=board_standard,
        smt_populated_sides=smt_side_count,
    )

    mode = "Standard" if board_standard else "Economic"
    reason_text = ", ".join(standard_signal_reasons(signals)) or "none"
    highlight_refs = set(trigger_references) if board_standard else set()
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
    populated_refs: Iterable[str],
    populated_sides: Iterable[str],
    smt_populated_sides: Iterable[str],
    standard_part_refs: Iterable[str],
) -> dict:
    """Build Standard/Economic policy context from normalized board facts.

    This function is intentionally side-effect free so policy decisions can be
    unit-tested without UI or board-object dependencies.
    """
    populated_refs = set(populated_refs)
    populated_sides = set(populated_sides)
    smt_populated_sides = set(smt_populated_sides)
    standard_part_refs = set(standard_part_refs)

    signals = {
        "manual_enabled": bool(manual_enabled),
        "qty_50_plus": board_count >= 50,
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
    """Return ``{reference: label}`` mapping for BOM price column population.

    Labels are per-reference display values, while quantity-tier pricing is
    resolved per unique LCSC code using aggregated board quantity.
    """
    part_rows = [part for part in parts if part.get("reference")]
    billable_rows = [
        part
        for part in part_rows
        if not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
    ]
    lcsc_quantities = _build_lcsc_quantities(billable_rows, board_count)

    details_cache: dict = {}
    result: dict = {}
    for part in part_rows:
        reference = part.get("reference")
        lcsc = str(part.get("lcsc") or "")
        details: dict = {}
        if lcsc:
            if lcsc not in details_cache:
                details_cache[lcsc] = get_part_details(lcsc)
            details = details_cache[lcsc]

        if not part.get("exclude_from_bom") and lcsc:
            quantity = lcsc_quantities.get(lcsc, board_count)
            unit_price = get_unit_price(quantity, str(details.get("price") or ""))
            if unit_price < 0:
                result[reference] = "N/A"
            else:
                result[reference] = f"${unit_price * board_count:.4f}"
            continue

        result[reference] = format_part_bom_price_label(part, details, board_count)

    return result
