"""Check stock reserves through real controller refresh and edit workflows."""

from typing import Any

import pytest

from .native_window_support import _SelectableFootprint, focus, window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def test_stock_updates_for_board_count_assignment_and_population_changes(
    window_ui: Any,
) -> None:
    """The selected count and all matching variant parts determine the reserve."""

    def check(ui: Any) -> None:
        ui.catalog.stock = 61
        ui.dialog.bom_estimator_board_count = 5
        ui.controller.refresh()

        def symbol(variant: str) -> str:
            model = ui.controller.model
            return model.get_display(
                model.row_for_component("component-1"),
                model.column_for(variant, "stock"),
            )

        assert symbol("A") == "✓"
        ui.dialog.bom_estimator_board_count = 7
        ui.controller.render()
        assert symbol("A") == "⚠"
        ui.dialog.bom_estimator_board_count = 6
        ui.board.parts.append(_SelectableFootprint(ui.board, "component-2", "R2"))
        ui.controller.refresh()
        assert symbol("A") == symbol("B") == "⚠"
        ui.controller._on_edit(focus(ui, "A", "pop", row=1), False)
        assert symbol("A") == "✓" and symbol("B") == "⚠"
        ui.controller._on_edit(focus(ui, "A", "pop", row=1), True)
        assert symbol("A") == "⚠"
        ui.controller._on_edit(focus(ui, "A", "lcsc", row=1), "C2")
        assert symbol("A") == "✓" and symbol("B") == "⚠"
        assert ui.messages == []

    window_ui.run(check)
