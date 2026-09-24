"""The Params column's resistance text, and its sub-ohm label (issue #849)."""

from __future__ import annotations

import pytest

import derive_params
from derive_params import params_for_part


def _resistor(description: str) -> str:
    return params_for_part(
        {"description": description, "category": "Resistors", "package": "2512"}
    )


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("7W ±75ppm/℃ ±1% 200mΩ", "200mΩ (0.2Ω) ±1% 2512"),
        ("1mΩ ±1% 1W Current Sense Resistor", "1mΩ (0.001Ω) ±1% 2512"),
        (
            "0.5mΩ 20ppm/℃~+50ppm/℃ 5W Current Sense Resistor SMD ±1%",
            "0.5mΩ (0.0005Ω) ±1% 2512",
        ),
        ("1.5mΩ ±1% 2W", "1.5mΩ (0.0015Ω) ±1% 2512"),
        ("100mΩ ±1% 1W", "100mΩ (0.1Ω) ±1% 2512"),
    ],
)
def test_a_milliohm_resistance_is_also_given_in_ohms(description, expected):
    """1mΩ and 1MΩ differ by one letter's case; 0.001Ω cannot be misread."""
    assert _resistor(description) == expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("100mW 1MΩ 75V Thick Film Resistor ±1%", "1MΩ ±1% 2512"),
        ("10kΩ ±1%", "10kΩ ±1% 2512"),
        ("100Ω ±1%", "100Ω ±1% 2512"),
    ],
)
def test_other_resistances_are_unchanged(description, expected):
    """Only the milli prefix gets the label."""
    assert _resistor(description) == expected


def test_the_module_table_of_real_descriptions_still_holds():
    """derive_params keeps its own table of LCSC descriptions; run it here."""
    derive_params.test_params_for_part()
