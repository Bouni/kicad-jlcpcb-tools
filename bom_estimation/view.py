"""View/presentation helpers for BOM estimation.

This module contains formatting and UI-oriented derivations and stays free of
transport concerns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from math import isfinite
import re
from typing import Optional, cast

from .assembly_mode import (
    AssemblyModeDecision,
    ComponentProductType,
    classify_component_product_type,
)
from .pricing import (
    BomEstimateSummary,
    _build_lcsc_quantities,
    _safe_int,
    calculate_bom_estimate,
    calculate_part_bom_cost,
    get_assembly_flags,
    get_unit_price,
)

ASSEMBLY_CAPABILITIES_SOURCE = (
    "JLCPCB assembly capabilities",
    "https://jlcpcb.com/capabilities/pcb-assembly-capabilities",
)
STANDARD_ONLY_SOURCE = (
    "JLCPCB BOM and CPL matching guidance",
    "https://jlcpcb.com/help/article/common-bom-and-cpl-matching-issues-and-explanations",
)
COMPONENT_MATCHING_SOURCE = (
    "JLCPCB component matching guidelines",
    "https://jlcpcb.com/help/article/component-matching-guidelines-for-pcba-orders",
)


def format_assembly_mode_status(decision: AssemblyModeDecision) -> str:
    """Return the terse user-facing assembly-mode status."""
    conflict_count = len(decision.economic_only_conflict_refs)
    missing_count = len(decision.classification_missing_refs)

    if conflict_count:
        noun = "part is" if conflict_count == 1 else "parts are"
        return f"Mode conflict · {conflict_count} selected {noun} Economic Only"
    if decision.board_standard is True:
        status = "Estimated pricing mode: Standard"
        if missing_count:
            noun = (
                "part classification" if missing_count == 1 else "part classifications"
            )
            status += f" · {missing_count} {noun} missing"
        return status
    if decision.board_standard is False:
        return "Estimated pricing mode: Economic"
    if missing_count:
        noun = "part classification" if missing_count == 1 else "part classifications"
        return f"Assembly mode unknown · {missing_count} {noun} missing"
    return "Assembly mode: N/A"


def assembly_mode_details_button_label(
    decision: AssemblyModeDecision,
) -> Optional[str]:
    """Return the details action label, or None when no action is useful."""
    if decision.economic_only_conflict_refs:
        return "Review conflict…"
    if decision.board_standard is True:
        return "Why Standard…"
    if decision.classification_missing_refs:
        return "Review parts…"
    return None


def format_bom_estimate_summary(
    summary: BomEstimateSummary,
    board_count: int,
    mode: object,
    reason_text: str = "",
) -> tuple[str, str]:
    """Format BOM estimate summary into two compact UI lines.

    ``mode`` accepts an ``AssemblyModeDecision`` for the current UI and the
    former ``"Standard"``/``"Economic"`` strings for API compatibility.
    ``reason_text`` remains accepted for compatibility but is intentionally no
    longer repeated in the compact summary.
    """
    if isinstance(mode, AssemblyModeDecision):
        board_standard = mode.board_standard
        applicable = mode.applicable
        status = format_assembly_mode_status(mode)
    else:
        board_standard = str(mode).strip().lower() == "standard"
        applicable = True
        status = (
            f"Estimated pricing mode: {'Standard' if board_standard else 'Economic'}"
        )
    overview_line = (
        f"BOM Estimate ({board_count} boards): {status} | "
        f"Missing prices {summary.missing_prices}"
    )

    if board_standard is None or (
        isinstance(mode, AssemblyModeDecision) and mode.economic_only_conflict_refs
    ):
        availability = (
            "Mode-dependent assembly estimate unavailable"
            if applicable
            else "No selected parts for assembly"
        )
        return (
            overview_line,
            f"Direct BOM Cost: ${summary.component_cost:.2f} | {availability}",
        )

    overview_line = (
        f"BOM Estimate ({board_count} boards): {status} | "
        f"Total ${summary.total_cost:.2f} | "
        f"Per board ${summary.cost_per_board:.2f} | "
        f"Missing prices {summary.missing_prices}"
    )

    mode_is_standard = board_standard is True
    mode_surcharge_cost = summary.standard_part_surcharge_cost
    surcharge_breakdown = f"standard service part fees: ${mode_surcharge_cost:.2f}"
    if not mode_is_standard:
        mode_surcharge_cost = summary.extended_cost
        surcharge_breakdown = f"extended: ${mode_surcharge_cost:.2f}"
    if mode_surcharge_cost <= 0:
        surcharge_breakdown = "surcharges: $0.00"
    displayed_fixed_cost = summary.fixed_cost + mode_surcharge_cost
    displayed_setup_cost = summary.economic_setup_cost + summary.standard_setup_cost
    displayed_joint_assembly_cost = max(
        0.0,
        summary.variable_assembly_cost - summary.standard_part_surcharge_cost,
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
    """Build legacy reason labels for compatibility callers."""
    reason_map = [
        ("manual_enabled", "manual"),
        ("quantity_over_50", "qty>50"),
        ("standard_part_present", "standard part"),
        ("multi_side_populated", "both sides populated"),
    ]
    return [label for key, label in reason_map if signals.get(key)]


def _counted_heading(count: int, singular: str, plural: str) -> str:
    """Return a terse count plus correctly pluralized reason label."""
    return f"{count} {singular if count == 1 else plural}"


def build_assembly_mode_reasons(
    decision: AssemblyModeDecision,
) -> list[dict[str, object]]:
    """Build ordered reason copy and source links for the details dialog."""
    reasons = []

    def add(heading, body, remedy="", sources=()):
        reasons.append(
            {
                "heading": heading,
                "body": body,
                "remedy": remedy,
                "sources": list(sources),
            }
        )

    if decision.quantity_over_50:
        add(
            "Quantity above 50",
            f"Board quantity {decision.board_count} exceeds the Economic maximum of 50.",
            "Set Boards to 50 or fewer.",
            (ASSEMBLY_CAPABILITIES_SOURCE,),
        )

    standard_count = len(decision.standard_only_refs)
    if standard_count:
        listed_standard = "part" if standard_count == 1 else "parts"
        add(
            _counted_heading(
                standard_count,
                "Standard-only part",
                "Standard-only parts",
            ),
            f"JLCPCB classifies the listed {listed_standard} as Standard Only.",
            "Choose Economic-compatible parts or leave these positions unplaced.",
            (STANDARD_ONLY_SOURCE, COMPONENT_MATCHING_SOURCE),
        )

    if decision.both_sides_populated:
        top_count = len(decision.top_refs)
        bottom_count = len(decision.bottom_refs)
        top_noun = "part is" if top_count == 1 else "parts are"
        bottom_noun = "part is" if bottom_count == 1 else "parts are"
        add(
            "Components on TOP and BOT",
            f"{top_count} selected {top_noun} on TOP; "
            f"{bottom_count} selected {bottom_noun} on BOT. "
            "Economic PCBA supports placement on one side.",
            "Move or leave unplaced every selected part on one side.",
            (ASSEMBLY_CAPABILITIES_SOURCE, COMPONENT_MATCHING_SOURCE),
        )

    if decision.manual_enabled and decision.applicable:
        add(
            "Force Standard",
            "The local Force Standard option is enabled.",
            "Clear Force Standard to allow automatic mode selection.",
        )

    conflict_count = len(decision.economic_only_conflict_refs)
    if conflict_count:
        listed_conflict = "part is" if conflict_count == 1 else "parts are"
        add(
            "Compatibility conflict — "
            + _counted_heading(conflict_count, "part", "parts"),
            f"Standard is required, but the listed {listed_conflict} Economic Only.",
            "Replace or leave the conflicting parts unplaced, or remove every "
            "Standard reason.",
        )

    missing_count = len(decision.classification_missing_refs)
    if missing_count:
        add(
            "Assembly classification missing — "
            + _counted_heading(missing_count, "part", "parts"),
            "No Economic/Standard classification was returned for these LCSC parts.",
            "Check the LCSC assignments and hover over the part's Std cell for assembly information.",
        )

    if not reasons:
        add(
            "No active Standard reasons",
            "No modeled reason currently selects Standard.",
        )
    return reasons


def _natural_reference_key(reference: str):
    """Return a key that sorts R2 before R10 without type comparisons."""
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.split(r"(\d+)", reference)
    )


def build_affected_part_rows(
    decision: AssemblyModeDecision,
    parts: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    """Join decision references to current part rows for the details table."""
    related_refs = set().union(
        decision.standard_only_refs,
        decision.economic_only_conflict_refs,
        decision.classification_missing_refs,
    )
    if decision.both_sides_populated:
        related_refs.update(decision.top_refs)
        related_refs.update(decision.bottom_refs)

    parts_by_ref = {
        str(part.get("reference") or ""): part
        for part in parts
        if part.get("reference")
    }
    rows = []
    for reference in sorted(related_refs, key=_natural_reference_key):
        relationships = []
        if reference in decision.standard_only_refs:
            relationships.append("Standard Only")
        if decision.both_sides_populated:
            relationships.append("Both-side context")
        if reference in decision.economic_only_conflict_refs:
            relationships.append("Economic Only conflict")
        if reference in decision.classification_missing_refs:
            relationships.append("Classification missing")

        part = parts_by_ref.get(reference, {})
        side = "TOP" if reference in decision.top_refs else "BOT"
        rows.append(
            {
                "relationship": "; ".join(relationships),
                "reference": reference,
                "value": str(part.get("value") or ""),
                "lcsc": str(part.get("lcsc") or ""),
                "side": side,
            }
        )
    return rows


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
    """Build the legacy pure BOM estimate view-model mapping.

    The main UI now consumes ``AssemblyModeDecision`` directly. This helper is
    retained for callers of the previously exported presentation API.
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
    """Build the legacy Standard/Economic policy context mapping."""
    populated_refs = set(populated_refs)
    populated_sides = set(populated_sides)
    smt_populated_sides = set(smt_populated_sides)
    standard_part_refs = set(standard_part_refs)

    signals = {
        "manual_enabled": bool(manual_enabled),
        "quantity_over_50": board_count > 50,
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


@dataclass(frozen=True)
class BomEstimateResult:
    """One calculation shared by the ordinary dialog and variant comparison."""

    decision: AssemblyModeDecision
    prices: dict[str, Optional[float]]
    price_labels: dict[str, str]
    summary: Optional[BomEstimateSummary]
    summary_text: str
    details_button_label: Optional[str]


def selected_assembly_parts(
    parts: Iterable[Mapping[str, object]],
) -> Iterable[Mapping[str, object]]:
    """Yield assigned BOM parts whose physical placements enter assembly policy."""
    return (
        part
        for part in parts
        if part.get("reference")
        and part.get("lcsc")
        and not part.get("exclude_from_bom")
        and not part.get("exclude_from_pos")
        and not get_assembly_flags(part).get("is_dnp")
    )


def _assembly_decision(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    sides: Mapping[str, str],
    force_standard: bool,
) -> AssemblyModeDecision:
    """Classify selected placements from supplied physical and supplier facts."""
    refs_by_side: dict[str, set[str]] = {"top": set(), "bottom": set()}
    classified: dict[Optional[ComponentProductType], set[str]] = {
        product_type: set() for product_type in [*ComponentProductType, None]
    }
    smt_sides: set[str] = set()
    for part in selected_assembly_parts(parts):
        reference = str(part["reference"])
        side = sides.get(reference)
        if side not in refs_by_side:
            continue
        refs_by_side[side].add(reference)
        if not _safe_int(part.get("has_tht")):
            smt_sides.add(side)
        product_type = classify_component_product_type(
            part.get("component_product_type")
        )
        classified[product_type].add(reference)

    return AssemblyModeDecision(
        board_count=board_count,
        manual_enabled=force_standard,
        top_refs=refs_by_side["top"],
        bottom_refs=refs_by_side["bottom"],
        smt_populated_side_count=len(smt_sides),
        standard_only_refs=classified[ComponentProductType.STANDARD_ONLY],
        economic_only_refs=classified[ComponentProductType.ECONOMIC_ONLY],
        classification_missing_refs=classified[None],
    )


def _prepare_bom_prices(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    get_part_details: Callable[[str], dict],
) -> tuple[dict[str, Optional[float]], dict[str, str]]:
    """Calculate each assigned part's unrounded cost and its display label.

    Price-column tiers include assigned DNP parts, preserving ordinary mode's
    assignment-pricing policy. BOM totals separately omit unpopulated parts.
    """
    rows = [part for part in parts if part.get("reference")]
    quantities = _build_lcsc_quantities(
        [
            part
            for part in rows
            if not part.get("exclude_from_bom") and part.get("lcsc")
        ],
        board_count,
    )
    unit_prices = {
        lcsc: get_unit_price(quantity, str(get_part_details(lcsc).get("price") or ""))
        for lcsc, quantity in quantities.items()
    }
    values: dict[str, Optional[float]] = {}
    labels: dict[str, str] = {}
    for part in rows:
        reference = str(part["reference"])
        lcsc = str(part.get("lcsc") or "")
        unit_price = None if part.get("exclude_from_bom") else unit_prices.get(lcsc)
        cost = None if unit_price is None else unit_price * board_count
        values[reference] = (
            cost
            if unit_price is not None
            and unit_price >= 0
            and cost is not None
            and cost >= 0
            and isfinite(cost)
            else None
        )
        labels[reference] = (
            "" if unit_price is None else "N/A" if unit_price < 0 else f"${cost:.4f}"
        )
    return values, labels


def prepare_bom_price_labels(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    get_part_details: Callable[[str], dict],
) -> dict[str, str]:
    """Return display costs using aggregated quantity tiers for each LCSC."""
    return _prepare_bom_prices(parts, board_count, get_part_details)[1]


def evaluate_bom_estimate(
    parts: Iterable[Mapping[str, object]],
    board_count: int,
    get_part_details: Callable[[str], dict],
    *,
    sides: Mapping[str, str],
    force_standard: bool = False,
) -> BomEstimateResult:
    """Prepare the complete estimate without reading a board or updating controls.

    ``sides`` maps references to ``top`` or ``bottom``; absent footprints do not
    participate in assembly mode selection. All prices in one evaluation use
    the same catalog response for each LCSC number.
    """
    parts = list(parts)
    decision = _assembly_decision(parts, board_count, sides, force_standard)
    if not any(not part.get("exclude_from_bom") and part.get("lcsc") for part in parts):
        reason = "no parts" if not parts else "no assigned BOM parts"
        return BomEstimateResult(
            decision,
            {},
            {},
            None,
            f"BOM Estimate ({board_count} boards): {reason}",
            None,
        )

    details = cache(get_part_details)
    summary = calculate_bom_estimate(
        parts,
        board_count,
        details,
        board_standard=decision.board_standard,
        smt_populated_sides=decision.smt_populated_side_count,
    )
    prices, labels = _prepare_bom_prices(parts, board_count, details)
    return BomEstimateResult(
        decision,
        prices,
        labels,
        summary,
        "\n".join(format_bom_estimate_summary(summary, board_count, decision)),
        assembly_mode_details_button_label(decision),
    )
