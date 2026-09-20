"""The part selector's prefill must carry the canonicalized value.

`canonicalize` is covered directly in `tests/test_value_normalize.py`, but
those tests re-compose the call in the test body, so they would still pass if
`select_part` stopped using the result or assembled the footprint token wrongly.
This exercises the glue itself.
"""

from unittest.mock import MagicMock

import pytest

from dataview_highlight import simplify_footprint_name

from .wx_harness import load_mainwindow, wx_stubs

# The harness stubs simplify_footprint_name as identity; use the real one so the
# assembled keyword is what the selector would actually receive.
mainwindow = load_mainwindow(
    "mainwindow_prefill_canonicalization_tests",
    wx=wx_stubs(
        Dialog=type("Dialog", (), {}),
        Frame=type("Frame", (), {}),
        NewIdRef=object,
        PostEvent=lambda *_a: None,
    ),
    dataview_highlight={
        "HighlightedTextRenderer": object,
        "decode_highlighted_value": lambda value: (value, []),
        "simplify_footprint_name": simplify_footprint_name,
    },
)
JLCPCBTools = mainwindow.JLCPCBTools


def _window(rows):
    """Build a bare main window whose selection is ``rows`` of (ref, value, footprint).

    ``_part_selector`` is pre-set so ``select_part`` re-targets the open dialog
    instead of constructing one, which is the branch that needs no GUI.
    """
    window = JLCPCBTools.__new__(JLCPCBTools)
    items = list(range(len(rows)))
    window.footprint_list = MagicMock()
    window.footprint_list.GetSelections.return_value = items
    model = MagicMock()
    model.get_reference.side_effect = lambda i: rows[i][0]
    model.get_value.side_effect = lambda i: rows[i][1]
    model.get_footprint.side_effect = lambda i: rows[i][2]
    window.partlist_data_model = model
    window._part_selector = MagicMock()
    return window


def _prefill(rows):
    window = _window(rows)
    window.select_part()
    window._part_selector.update_for.assert_called_once()
    return window._part_selector.update_for.call_args[0][0]


@pytest.mark.parametrize(
    ("reference", "value", "footprint", "expected"),
    [
        ("C1", "0.1uF", "Capacitor_SMD:C_0603_1608Metric", "100nF 0603"),
        ("C2", "100p", "Capacitor_SMD:C_0402_1005Metric", "100pF 0402"),
        ("R1", "4k7", "Resistor_SMD:R_0805_2012Metric", "4.7kΩ 0805"),
        ("R2", "0", "Resistor_SMD:R_0603_1608Metric", "0Ω 0603"),
        ("R3", "2200", "Resistor_SMD:R_0402_1005Metric", "2.2kΩ 0402"),
        ("L1", "2u2", "Inductor_SMD:L_0805_2012Metric", "2.2uH 0805"),
    ],
)
def test_prefill_is_the_canonical_value_plus_the_footprint(
    reference, value, footprint, expected
):
    """Each quantity's canonical spelling reaches the selector, footprint appended."""
    assert _prefill([(reference, value, footprint)]) == {reference: expected}


@pytest.mark.parametrize(
    ("reference", "value"),
    [
        ("D1", "1N4148"),
        ("U1", "LM358"),
        ("RL1", "NO/NC"),
        ("C3", "X7R"),
        ("R4", "0603"),
    ],
)
def test_a_value_with_no_canonical_form_reaches_the_selector_untouched(
    reference, value
):
    """Nothing the module declines to read may be rewritten on the way through."""
    assert _prefill([(reference, value, "")]) == {reference: value}


def test_the_reference_decides_which_quantity_a_bare_prefix_means():
    """The same board value canonicalizes differently for a capacitor and an inductor."""
    assert _prefill([("C1", "2u2", ""), ("L1", "2u2", "")]) == {
        "C1": "2.2uF",
        "L1": "2.2uH",
    }


def test_a_footprint_that_does_not_simplify_adds_no_token():
    """An unrecognised footprint must not append a stray separator."""
    assert _prefill([("R1", "10k", "")]) == {"R1": "10kΩ"}
