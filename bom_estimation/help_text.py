"""Shared help text for BOM estimator UI surfaces."""

BOM_ESTIMATOR_HELP_TITLE = "BOM estimator help"

BOM_ESTIMATOR_HELP_TEXT = (
    "BOM estimator notes:\n\n"
    "• Uses live network requests to look up some part metadata in real time.\n"
    "• Values shown are rough estimates for planning and comparison only.\n"
    "• Final pricing is always provided by JLC at order time.\n"
    "• If you see serious inconsistencies, please report an issue with details."
)


def get_bom_estimator_help_text() -> str:
    """Return the shared explanatory text for BOM estimator help popups."""
    return BOM_ESTIMATOR_HELP_TEXT
