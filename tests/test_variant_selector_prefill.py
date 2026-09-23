"""A board with design variants opens the selector on the catalog's spelling too.

With variants defined, the main window hands selection to the variant matrix,
which assembles its own prefill from the focused variant's value.  Both paths
must produce the same search, or a board gains variants and its searches
quietly stop finding parts.
"""

from typing import Any

import pytest

from .native_window_support import focus, window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("variant", ["", "A"], ids=["default", "named"])
@pytest.mark.parametrize(
    ("reference", "footprint", "value", "expected"),
    [
        pytest.param(
            "C1",
            "Capacitor_SMD:C_0603_1608Metric",
            "0.1uf",
            "100nF 0603",
            id="capacitor",
        ),
        pytest.param(
            "R1",
            "Resistor_SMD:R_0603_1608Metric",
            "4k7",
            "4.7kΩ 0603",
            id="rkm-resistor",
        ),
        pytest.param(
            "R1",
            "Resistor_SMD:R_0603_1608Metric",
            "100Ω",
            "100Ω 0603",
            id="unit-already-spelled",
        ),
    ],
)
def test_variant_selector_prefills_the_catalog_spelling(
    window_ui: Any,
    variant: str,
    reference: str,
    footprint: str,
    value: str,
    expected: str,
) -> None:
    def check(ui: Any) -> None:
        """Open the selector from the main window on the focused variant's value."""
        part = ui.board.parts[0]
        part.fields["Reference"] = reference
        part.GetFPIDAsString = lambda: footprint
        if variant:
            # The base keeps the fixture's own value, so reading Default by
            # mistake would search for it and fail loudly.
            part.AddVariant(variant).SetFieldValue("Value", value)
        else:
            part.fields["Value"] = value
        ui.controller.refresh()
        focus(ui, variant)

        ui.dialog.select_part()

        selector = ui.dialog._part_selector
        assert selector.parts == {reference: expected}
        assert selector.keyword.GetValue() == expected
        assert not ui.messages

    window_ui.run(check)
