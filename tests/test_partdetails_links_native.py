"""The real Part Details buttons wait for the part record's links."""

import sys
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import button, window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

DATASHEET = "https://www.lcsc.com/datasheet/lcsc_datasheet_C25804.pdf"


def test_open_buttons_are_enabled_by_the_links_the_record_has(
    window_ui: Any,
) -> None:
    """Both start disabled; the record enables the one it has a link for."""

    def check(ui: Any) -> None:
        dialog_class = ui.selector_module.PartDetailsDialog
        details = sys.modules[dialog_class.__module__]
        with patch.object(dialog_class, "_fetch_part_data", lambda _self: None):
            dialog = dialog_class(ui.dialog, "C25804")
        try:
            assert not dialog.openpdf_button.IsEnabled()
            assert not dialog.openpage_button.IsEnabled()

            dialog._apply_part_data(
                {"success": True, "data": {"data": {"dataManualUrl": DATASHEET}}},
                None,
            )

            assert dialog.openpdf_button.IsEnabled()
            assert not dialog.openpage_button.IsEnabled()
            with patch.object(
                details.wx, "LaunchDefaultBrowser", return_value=True
            ) as launch:
                button(ui.wx, dialog.openpdf_button)
            launch.assert_called_once_with(DATASHEET)
        finally:
            dialog.Destroy()

    window_ui.run(check)
