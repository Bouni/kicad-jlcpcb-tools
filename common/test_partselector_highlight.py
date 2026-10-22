"""Tests for reusable dataview highlighting helpers."""

import pytest

import dataview_highlight
from dataview_highlight import (
    HighlightQueryCache,
    decode_highlighted_value,
    encode_highlighted_value,
    expand_footprint,
    expand_value,
    filtered_highlight_terms,
    find_highlight_spans,
    normalize_highlight_terms,
    simplify_footprint_name,
)
from tests.native_wx_support import run_native


def test_normalize_highlight_terms_deduplicates_casefolded_terms():
    """Normalization folds case and removes duplicate query terms."""
    assert normalize_highlight_terms(" 10K 0603 10k ") == ["10k", "0603"]


def test_filtered_highlight_terms_skips_short_terms():
    """Single-character terms are ignored to avoid noisy broad highlights."""
    assert filtered_highlight_terms("1 12 1206") == ["12", "1206"]


def test_find_highlight_spans_merges_overlaps():
    """Highlight spans are returned in display order without overlap duplication."""
    assert find_highlight_spans("0603 10kΩ", ["0603", "10k"]) == [(0, 4), (5, 8)]


def test_find_highlight_spans_prefers_longest_overlap_when_merged():
    """Overlapping matches merge to the longest highlighted span."""
    terms = normalize_highlight_terms("10K 10KΩ")
    assert find_highlight_spans("10KΩ", terms) == [(0, 4)]


def test_highlight_query_cache_stores_negative_results():
    """Cache stores empty span lookups so misses are reused efficiently."""
    cache = HighlightQueryCache()
    cache.prepare("zzzz")

    assert cache.get_spans("Murata Electronics") == []


def test_highlight_query_cache_reuses_cached_negative_results():
    """Repeated misses return the cached list instance for the same text."""
    cache = HighlightQueryCache()
    cache.prepare("zzzz")

    first = cache.get_spans("Murata Electronics")
    second = cache.get_spans("Murata Electronics")

    assert first == []
    assert second == []
    assert first is second


def test_highlight_query_cache_prepare_changes_terms_and_resets_spans():
    """Changing query text resets cached spans and prepared terms."""
    cache = HighlightQueryCache()
    cache.prepare("murata")
    spans_before = cache.get_spans("Murata Electronics")

    cache.prepare("1206")
    spans_after = cache.get_spans("Murata Electronics")

    assert spans_before != []
    assert cache.get_terms() == ["1206"]
    assert spans_after == []


def test_highlight_query_cache_clear_resets_all_state():
    """Clearing the cache drops both prepared terms and computed spans."""
    cache = HighlightQueryCache()
    cache.prepare("murata 1206")
    _ = cache.get_spans("Murata Electronics")

    cache.clear()

    assert cache.get_terms() == []


def test_encode_decode_highlighted_value_round_trip():
    """Packed cell values preserve display text and normalized highlight terms."""
    encoded = encode_highlighted_value("10kΩ 1% 0603", ["10kΩ", "0603"])

    assert decode_highlighted_value(encoded) == (
        "10kΩ 1% 0603",
        ["10kω", "0603"],
    )


def test_decode_highlighted_value_without_metadata_returns_plain_text():
    """Plain strings decode to themselves with no highlight metadata."""
    assert decode_highlighted_value("plain text") == ("plain text", [])


def test_simplify_footprint_name_extracts_metric_size():
    """Metric KiCad footprints are reduced to the expected short package name."""
    assert simplify_footprint_name("Resistor_SMD:R_0603_1608Metric") == "0603"


def test_simplify_footprint_name_reads_the_package_designator():
    """Non-metric footprint names resolve to the package the catalog lists."""
    assert simplify_footprint_name("Package_TO_SOT_SMD:SOT-23") == "SOT-23"


def test_expand_footprint_highlights_a_bare_chip_size():
    """A KiCad 4 chip footprint still lends its size to the parameter highlight."""
    assert "0603" in expand_footprint("R1", "Resistor_SMD:R_0603")


def test_expand_value_for_resistor_adds_ohm_symbol_variants():
    """Resistor values expand with Ω-compatible alternatives for matching."""
    assert expand_value("R1", "390R") == ["390R", "390Ω"]
    assert expand_value("R2", "10K") == ["10K", "10KΩ"]
    assert expand_value("R3", "10KΩ") == ["10KΩ", "10K"]


def test_expand_value_for_non_resistor_keeps_original_only():
    """Non-resistor references do not get resistor-specific value expansion."""
    assert expand_value("C1", "10K") == ["10K"]


def test_expand_value_for_capacitor_adds_micro_variants():
    """Capacitor values add interchangeable `u` and `µ` variants."""
    assert expand_value("C10", "10µF") == ["10µF", "10uF", "10µ", "10u"]
    assert expand_value("C11", "10uF") == ["10uF", "10µF", "10u", "10µ"]


def test_expand_value_for_capacitor_without_suffix_adds_f_variants():
    """Capacitor values without `F` also get `F`-suffixed variants."""
    assert expand_value("C12", "1u") == ["1u", "1µ", "1uF", "1µF"]
    assert expand_value("C13", "1µ") == ["1µ", "1u", "1µF", "1uF"]


def test_expand_value_adds_the_catalog_spelling():
    """A board value also highlights the spelling the catalog writes for it."""
    assert "2.2nF" in expand_value("C1", "2n2")
    assert "100nF" in expand_value("C2", "0.1uF")
    assert "4.7kΩ" in expand_value("R1", "4k7")
    assert "2.2uH" in expand_value("L1", "2u2")


def test_expand_value_catalog_spelling_highlights_the_params_text():
    """The catalog spelling reaches the rendered params through the cell codec."""
    packed = encode_highlighted_value("2.2nF 50V", expand_value("C1", "2n2"))
    text, terms = decode_highlighted_value(packed)
    assert find_highlight_spans(text, terms, whole_values=True) == [(0, 5)]


def test_expand_value_gives_a_bare_resistance_its_ohm_sign():
    """A bare number lights its own value, not the start of a larger one."""
    assert expand_value("R1", "100") == ["100Ω"]
    assert expand_value("R2", ".47") == ["0.47Ω", "470mΩ"]
    terms = normalize_highlight_terms(" ".join(expand_value("R1", "100")))
    assert find_highlight_spans("100kΩ ±1%", terms, whole_values=True) == []
    assert find_highlight_spans("100Ω ±1%", terms, whole_values=True) == [(0, 4)]
    assert _params_spans("R3", "0.1", "0.1Ω ±1% 0805") == ["0.1Ω"]
    assert _params_spans("R3", "0.1", "100mΩ (0.1Ω) ±1%") == ["100mΩ", "0.1Ω"]


def test_whole_values_refuse_a_digit_or_point_before_a_value():
    """A board value does not light the end of a different value."""
    terms = normalize_highlight_terms(" ".join(expand_value("R1", "4k7")))
    assert find_highlight_spans("14.7kΩ ±1%", terms) == [(1, 6)]
    assert find_highlight_spans("14.7kΩ ±1%", terms, whole_values=True) == []
    assert find_highlight_spans("4.7kΩ ±1%", terms, whole_values=True) == [(0, 5)]
    terms = normalize_highlight_terms(" ".join(expand_value("R2", "0R2")))
    assert find_highlight_spans("200mΩ (0.2Ω)", terms, whole_values=True) == [(0, 5)]


def test_whole_values_bound_a_value_written_with_a_leading_point():
    """A board .1uF does not light the end of 10.1uF."""
    assert find_highlight_spans("10.1uF 50V", [".1uf"], whole_values=True) == []
    assert _params_spans("C1", ".1uF", "10.1uF 50V") == []
    assert _params_spans("C1", ".1uF", "100nF 50V") == ["100nF"]


def _params_spans(reference, value, params):
    """Return the highlighted text of params the way the footprint list finds it."""
    packed = encode_highlighted_value(params, expand_value(reference, value))
    text, terms = decode_highlighted_value(packed)
    return [text[a:b] for a, b in find_highlight_spans(text, terms, whole_values=True)]


def test_thermistor_keeps_its_bare_code():
    """A thermistor's params show its part number, where its code is the match."""
    assert "103" in expand_value("TH1", "103")
    assert _params_spans("TH1", "103", "NCP18XH103F03RB 0603") == ["103"]


@pytest.mark.native_wx
def test_params_renderer_lights_the_board_value_and_not_its_neighbours():
    """The footprint list's params cell holds its board value to a whole value."""
    pytest.importorskip("wx")
    drawn = []

    class RecordingDC:
        """Record each painted run and its colour, with fixed text metrics."""

        colour = None

        def SetTextForeground(self, colour):
            self.colour = colour

        def SetBackgroundMode(self, _mode):
            pass

        def GetTextExtent(self, text):
            return 7 * len(text), 12

        def SetClippingRegion(self, _rect):
            pass

        def DestroyClippingRegion(self):
            pass

        def DrawText(self, text, _x, _y):
            drawn.append((text, self.colour))

    def exercise(_frame, wx):
        plain = wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOXTEXT)
        renderer = dataview_highlight.HighlightedTextRenderer(
            value_decoder=decode_highlighted_value
        )
        for value, params, lit in (
            ("4k7", "4.7kΩ ±1% 0603", ["4.7kΩ"]),
            ("4k7", "14.7kΩ ±1% 0603", []),
            ("100", "100kΩ ±1% 0603", []),
        ):
            drawn.clear()
            renderer.SetValue(
                encode_highlighted_value(params, expand_value("R1", value))
            )
            renderer.Render(wx.Rect(0, 0, 400, 20), RecordingDC(), 0)
            assert "".join(text for text, _ in drawn) == params
            assert [text for text, colour in drawn if colour != plain] == lit

    run_native(exercise)


def test_whole_values_keep_package_terms_and_later_matches():
    """Only number-led terms are bounded, and a refused match does not end the scan."""
    assert find_highlight_spans("10kΩ 0603", ["0603"], whole_values=True) == [(5, 9)]
    assert find_highlight_spans("SOT-23-5", ["sot-23"], whole_values=True) == [(0, 6)]
    assert find_highlight_spans("21k1k1", ["1k1"], whole_values=True) == [(3, 6)]


def test_expand_value_leaves_values_without_a_quantity_alone():
    """No catalog spelling is guessed for a reference that names no quantity."""
    assert expand_value("D1", "1N4148") == ["1N4148"]
    assert expand_value("U1", "2n2") == ["2n2"]
    assert expand_value("U2", "555") == ["555"]


def test_expand_footprint_adds_known_aliases():
    """Footprint aliases include known compatible package naming alternatives."""
    assert "SO-8" in expand_footprint("U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    assert "TO-236" in expand_footprint("Q1", "Package_TO_SOT_SMD:SOT-23")


def test_expand_footprint_adds_reverse_aliases_generated_from_forward_map():
    """Reverse aliases are auto-generated from the canonical alias map.

    Only the SO-8 direction is asserted: no installed KiCad library names a
    footprint TO-236, so the reverse of that entry cannot fire on a real board.
    """
    assert "SOIC-8" in expand_footprint("U2", "Package_SO:SO-8_3.9x4.9mm_P1.27mm")
    assert "SOIC-8" in expand_footprint("U3", "Package_SO:Diodes_SO-8EP")


def test_expand_footprint_maps_electrolytic_diameter():
    """Electrolytic footprints emit the SMD diameter term the catalog writes.

    The reference designator no longer gates this: the footprint itself says the
    part is an electrolytic can, whatever the schematic called it.
    """
    assert "SMD,D6.3x" in expand_footprint("C5", "Capacitor_SMD:CP_Elec_6.3x7.7")
    assert "SMD,D6.3x" in expand_footprint("U5", "Capacitor_SMD:CP_Elec_6.3x7.7")
