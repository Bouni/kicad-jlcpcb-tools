"""Tests for selector keyword generation."""

from search_keywords import build_search_keywords


class TestBuildSearchKeywords:
    """Verify value normalization and category suggestions."""

    def test_preserves_capacitor_value_and_voltage_segments(self):
        """Capacitor ratings separated by a slash remain searchable."""
        assert build_search_keywords('C1', '100nF/16V', 'Capacitor_SMD:C_0603_1608Metric') == (
            '100nF 16V',
            'Capacitors',
            '0603',
        )

    def test_normalizes_resistor_value_and_preserves_ratings(self):
        """Resistor value and slash-separated power and voltage remain searchable."""
        assert build_search_keywords('R1', '200R/20W/250V', 'Resistor_SMD:R_0603_1608Metric') == (
            '200Ω 20W 250V',
            'Resistors',
            '0603',
        )

    def test_splits_underscores_and_maps_reference_category(self):
        """Underscore-separated value terms are searchable for ICs."""
        assert build_search_keywords('U3', 'STM32F103_C8T6', 'Package_QFP:LQFP-48') == (
            'STM32F103 C8T6',
            'Integrated Circuits (ICs)',
            '',
        )
