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


def show_bom_estimator_help(parent) -> None:
    """Display the BOM estimator help dialog with shared text and title.

    `wx` is imported lazily so this module stays importable from non-wx test
    environments (per the bom_estimation package's wx-free contract).
    """
    import wx  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,import-error

    wx.MessageBox(
        BOM_ESTIMATOR_HELP_TEXT,
        BOM_ESTIMATOR_HELP_TITLE,
        style=wx.OK | wx.ICON_INFORMATION,
        parent=parent,
    )
