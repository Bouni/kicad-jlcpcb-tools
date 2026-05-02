"""BOM estimation domain modules (pricing and presentation)."""

from .help_text import (
    BOM_ESTIMATOR_HELP_TEXT,
    BOM_ESTIMATOR_HELP_TITLE,
    get_bom_estimator_help_text,
)
from .pricing import (
    DEFAULT_PRICING,
    AssemblyPricing,
    BomEstimateSummary,
    _collect_billable_bom_parts,
    _scan_assembly_state,
    calculate_bom_estimate,
    calculate_part_bom_cost,
    get_assembly_flags,
    get_unit_price,
    is_tht_part,
)
from .view import (
    build_bom_estimate_view_model,
    build_standard_mode_context,
    format_bom_estimate_summary,
    format_part_bom_price_label,
    prepare_bom_price_labels,
    standard_signal_reasons,
)

__all__ = [
    "BOM_ESTIMATOR_HELP_TEXT",
    "BOM_ESTIMATOR_HELP_TITLE",
    "AssemblyPricing",
    "BomEstimateSummary",
    "DEFAULT_PRICING",
    "get_bom_estimator_help_text",
    "get_unit_price",
    "is_tht_part",
    "get_assembly_flags",
    "calculate_bom_estimate",
    "calculate_part_bom_cost",
    "format_bom_estimate_summary",
    "standard_signal_reasons",
    "format_part_bom_price_label",
    "build_bom_estimate_view_model",
    "build_standard_mode_context",
    "prepare_bom_price_labels",
    "_collect_billable_bom_parts",
    "_scan_assembly_state",
]
