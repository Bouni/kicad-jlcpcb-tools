"""The real Download Datasheet button waits for the part record's datasheet."""

from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def test_download_is_enabled_once_the_record_names_a_datasheet(
    window_ui: Any,
) -> None:
    """The button starts disabled; a record with a datasheet file id enables it."""

    def check(ui: Any) -> None:
        dialog_class = ui.selector_module.PartDetailsDialog
        with patch.object(dialog_class, "_fetch_part_data", lambda _self: None):
            dialog = dialog_class(ui.dialog, "C25804")
        try:
            assert not dialog.savepdf_button.IsEnabled()

            dialog._apply_part_data(
                {
                    "success": True,
                    "data": {"data": {"dataManualFileAccessId": "8579706381320323072"}},
                },
                None,
            )

            assert dialog.savepdf_button.IsEnabled()
        finally:
            dialog.Destroy()

    window_ui.run(check)
