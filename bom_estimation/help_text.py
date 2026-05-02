"""Shared help text for BOM estimator UI surfaces.

Keep this text centralized so main-window and settings help dialogs stay in
sync and reviewers can validate wording in one place.
"""

BOM_ESTIMATOR_HELP_TITLE = "BOM estimator help"

BOM_ESTIMATOR_HELP_TEXT = (
    "BOM estimator notes:\n\n"
    "• Uses live network requests to look up some part metadata in real time.\n"
    "• Values shown are rough estimates for planning and comparison only.\n"
    "• Final pricing is always provided by JLC at order time.\n"
    "• If you see serious inconsistencies, please report an issue with details."
)


def get_bom_estimator_help_text() -> str:
    """Return BOM estimator help text used by all UI help popups."""
    return BOM_ESTIMATOR_HELP_TEXT
